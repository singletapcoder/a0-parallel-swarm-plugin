"""Wave controller manifest helpers for OpenRouter swarm runs.

This module provides the durable state-machine layer above individual worker
artifacts and lane-lead aggregation. It is intentionally deterministic and
report-only: helpers write manifests/reports but do not call OpenRouter, create
scheduler tasks, or mutate Trading V4.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TERMINAL_WORKER_STATUSES = {"completed", "blocked", "failed", "cancelled", "timeout", "timed_out"}
ACTIVE_WORKER_STATUSES = {"starting", "preflight_ok", "started_http_request", "running", "pending"}
CONTROLLER_STATES = {
    "planned",
    "workers_running",
    "workers_done",
    "ready_for_lane_lead",
    "lane_leads_running",
    "lane_leads_done",
    "repair_running",
    "ready_for_jarvis",
    "closed",
    "blocked",
}


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_json(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"_json_error": "decode_failed", "path": str(p)}
    return data if isinstance(data, dict) else {"_json_error": "not_object", "path": str(p)}


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def create_wave_manifest(
    *,
    run_id: str,
    output_dir: str | Path,
    max_workers_total: int,
    max_concurrency: int,
    token_budget: int | None = None,
    cost_budget_usd: float | None = None,
    worker_payloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a durable wave manifest without launching any workers."""
    if max_workers_total < 1:
        raise ValueError("max_workers_total must be >= 1")
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be >= 1")
    workers = []
    for idx, payload in enumerate(worker_payloads or [], start=1):
        task_id = str(payload.get("id") or f"worker_{idx:03d}")
        workers.append(
            {
                "task_id": task_id,
                "status": "pending",
                "model": str(payload.get("model") or ""),
                "lane": str(payload.get("lane") or ""),
                "role": str(payload.get("role") or ""),
                "artifact_dir": "",
                "result_path": "",
                "scheduler_uuid": "",
                "token_usage": {},
                "cost_usd": None,
            }
        )
    manifest = {
        "schema": "parallel_swarm.openrouter_wave_manifest.v1",
        "run_id": run_id,
        "created_at_utc": utc_stamp(),
        "updated_at_utc": utc_stamp(),
        "state": "planned",
        "output_dir": str(output_dir),
        "max_workers_total": max_workers_total,
        "max_concurrency": max_concurrency,
        "token_budget": token_budget,
        "cost_budget_usd": cost_budget_usd,
        "workers": workers,
        "scheduler_tasks": [],
        "lane_lead_reports": [],
        "repair_test_plans": [],
        "budget_guard": {
            "tokens_used": 0,
            "cost_usd": 0.0,
            "token_budget_exceeded": False,
            "cost_budget_exceeded": False,
        },
        "trading_v4_mutation_performed": False,
        "openrouter_calls_launched_by_controller": False,
        "notes": [],
    }
    save_manifest(manifest, Path(output_dir) / "wave_manifest.json")
    return manifest


def load_manifest(path: str | Path) -> dict[str, Any]:
    manifest = _read_json(path)
    if manifest.get("schema") != "parallel_swarm.openrouter_wave_manifest.v1":
        manifest.setdefault("schema_warning", "unexpected_or_missing_schema")
    return manifest


def save_manifest(manifest: dict[str, Any], path: str | Path) -> None:
    payload = dict(manifest)
    payload["updated_at_utc"] = utc_stamp()
    _write_json(path, payload)


def register_scheduler_task(manifest: dict[str, Any], *, uuid: str, name: str, stage: str, prompt_path: str = "") -> dict[str, Any]:
    """Record a scheduler task UUID in the manifest; does not create the task."""
    tasks = list(manifest.get("scheduler_tasks") or [])
    record = {"uuid": uuid, "name": name, "stage": stage, "prompt_path": prompt_path, "status": "registered"}
    if not any(item.get("uuid") == uuid for item in tasks):
        tasks.append(record)
    manifest["scheduler_tasks"] = tasks
    return record


def update_worker_from_result(
    manifest: dict[str, Any],
    *,
    task_id: str,
    result_path: str | Path,
    manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    """Update one worker record from a pilot_result.json-style artifact.

    If manifest_path is provided, persist the updated manifest immediately so
    later controller stages can safely reload terminal worker state.
    """
    result = _read_json(result_path)
    workers = list(manifest.get("workers") or [])
    worker = next((item for item in workers if item.get("task_id") == task_id), None)
    if worker is None:
        worker = {"task_id": task_id}
        workers.append(worker)
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    raw_usage = usage.get("raw_usage") if isinstance(usage.get("raw_usage"), dict) else {}
    worker.update(
        {
            "status": str(result.get("status") or worker.get("status") or "unknown"),
            "artifact_dir": str(result.get("output_dir") or worker.get("artifact_dir") or ""),
            "result_path": str(result_path),
            "model": str(result.get("model") or worker.get("model") or ""),
            "token_usage": usage,
            "cost_usd": raw_usage.get("cost", worker.get("cost_usd")),
            "auditable_worker_result": bool(result.get("auditable_worker_result", False)),
        }
    )
    manifest["workers"] = workers
    if manifest_path is not None:
        save_manifest(manifest, manifest_path)
    return worker


def summarize_workers(manifest: dict[str, Any]) -> dict[str, Any]:
    workers = list(manifest.get("workers") or [])
    counts = {
        "total": len(workers),
        "completed": 0,
        "failed": 0,
        "blocked": 0,
        "cancelled": 0,
        "timeout": 0,
        "running": 0,
        "pending": 0,
        "unknown": 0,
        "terminal": 0,
    }
    tokens_used = 0
    cost_usd = 0.0
    for worker in workers:
        status = str(worker.get("status") or "unknown")
        if status == "timed_out":
            status = "timeout"
        if status in counts:
            counts[status] += 1
        elif status in ACTIVE_WORKER_STATUSES:
            counts["running"] += 1
        else:
            counts["unknown"] += 1
        if status in TERMINAL_WORKER_STATUSES:
            counts["terminal"] += 1
        usage = worker.get("token_usage") if isinstance(worker.get("token_usage"), dict) else {}
        try:
            tokens_used += int(usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            pass
        try:
            if worker.get("cost_usd") is not None:
                cost_usd += float(worker.get("cost_usd") or 0)
        except (TypeError, ValueError):
            pass
    return {"counts": counts, "tokens_used": tokens_used, "cost_usd": round(cost_usd, 8)}


def apply_budget_guard(manifest: dict[str, Any]) -> dict[str, Any]:
    summary = summarize_workers(manifest)
    token_budget = manifest.get("token_budget")
    cost_budget = manifest.get("cost_budget_usd")
    guard = {
        "tokens_used": summary["tokens_used"],
        "cost_usd": summary["cost_usd"],
        "token_budget": token_budget,
        "cost_budget_usd": cost_budget,
        "token_budget_exceeded": bool(token_budget is not None and summary["tokens_used"] > int(token_budget)),
        "cost_budget_exceeded": bool(cost_budget is not None and summary["cost_usd"] > float(cost_budget)),
    }
    manifest["budget_guard"] = guard
    return guard


def compute_next_state(manifest: dict[str, Any], *, min_terminal_fraction: float = 1.0) -> str:
    """Compute next controller state from worker/lane/repair evidence."""
    guard = apply_budget_guard(manifest)
    if guard["token_budget_exceeded"] or guard["cost_budget_exceeded"]:
        return "blocked"
    workers = summarize_workers(manifest)["counts"]
    total = workers["total"]
    if total == 0:
        return "planned"
    terminal_fraction = workers["terminal"] / total if total else 0.0
    if workers["terminal"] < total and terminal_fraction < min_terminal_fraction:
        return "workers_running"
    if not manifest.get("lane_lead_reports"):
        return "ready_for_lane_lead"
    if manifest.get("repair_test_plans"):
        return "ready_for_jarvis"
    return "lane_leads_done"


def refresh_manifest_state(manifest: dict[str, Any], *, min_terminal_fraction: float = 1.0) -> dict[str, Any]:
    manifest["worker_summary"] = summarize_workers(manifest)
    manifest["state"] = compute_next_state(manifest, min_terminal_fraction=min_terminal_fraction)
    manifest["updated_at_utc"] = utc_stamp()
    return manifest


def write_monitor_report(manifest: dict[str, Any], *, output_dir: str | Path) -> dict[str, Any]:
    """Write deterministic controller monitor JSON/Markdown reports."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest = refresh_manifest_state(dict(manifest))
    json_path = out / "wave_controller_monitor_report.json"
    md_path = out / "wave_controller_monitor_report.md"
    _write_json(json_path, manifest)
    counts = manifest.get("worker_summary", {}).get("counts", {})
    guard = manifest.get("budget_guard", {})
    lines = [
        "# OpenRouter Wave Controller Monitor Report",
        "",
        f"- Run ID: `{manifest.get('run_id')}`",
        f"- State: `{manifest.get('state')}`",
        f"- Workers total: `{counts.get('total', 0)}`",
        f"- Workers terminal: `{counts.get('terminal', 0)}`",
        f"- Completed: `{counts.get('completed', 0)}`",
        f"- Failed: `{counts.get('failed', 0)}`",
        f"- Blocked: `{counts.get('blocked', 0)}`",
        f"- Running/Pending: `{counts.get('running', 0) + counts.get('pending', 0)}`",
        f"- Tokens used: `{guard.get('tokens_used', 0)}`",
        f"- Cost USD: `{guard.get('cost_usd', 0.0)}`",
        f"- Trading V4 mutation performed: `{manifest.get('trading_v4_mutation_performed', False)}`",
        "",
        "## Recommended Next Action",
        "",
        _recommended_next_action(manifest),
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    manifest["monitor_json_report_path"] = str(json_path)
    manifest["monitor_markdown_report_path"] = str(md_path)
    return manifest


def _recommended_next_action(manifest: dict[str, Any]) -> str:
    state = str(manifest.get("state") or "")
    if state == "planned":
        return "launch_bounded_workers"
    if state == "workers_running":
        return "wait_or_monitor_workers"
    if state == "ready_for_lane_lead":
        return "run_lane_lead_aggregation"
    if state == "lane_leads_done":
        return "select_top_candidates_for_repair_test_planning"
    if state == "ready_for_jarvis":
        return "jarvis_review_top_validated_candidates"
    if state == "blocked":
        return "stop_and_notify_principal"
    return "record_and_close"
