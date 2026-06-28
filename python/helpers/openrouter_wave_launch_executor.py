"""Safe launch executor adapter for OpenRouter wave dispatch plans.

The executor consumes a dispatch plan and updates launch/manifest evidence. It
is dry-run/report-only by default. Actual worker launch remains an explicit,
separately-approved step and is not performed by the default path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from plugins.parallel_swarm.python.helpers.openrouter_wave_controller import load_manifest, save_manifest, utc_stamp


LAUNCH_PLAN_SCHEMA = "parallel_swarm.openrouter_launch_execution.v1"


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


def _dispatch_plan_path(path: str | Path) -> Path:
    p = Path(path)
    if p.is_dir():
        return p / "dispatch_plan.json"
    return p


def _next_planned_batch(plan: dict[str, Any]) -> dict[str, Any] | None:
    for batch in plan.get("launch_batches") or []:
        if str(batch.get("status") or "planned") == "planned":
            return batch
    return None


def execute_next_launch_batch(
    dispatch_plan_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    dry_run: bool = True,
    allow_openrouter_launch: bool = False,
) -> dict[str, Any]:
    """Execute or dry-run the next planned launch batch.

    Default behavior is dry-run. If dry_run is False but allow_openrouter_launch
    is not explicitly True, the executor blocks fail-closed and records why.
    Even when allowed, this adapter currently only records launch readiness; the
    actual network launch should be implemented in a later, separately-gated
    runner.
    """
    plan_path = _dispatch_plan_path(dispatch_plan_path)
    plan = _read_json(plan_path)
    out = Path(output_dir) if output_dir is not None else plan_path.parent
    out.mkdir(parents=True, exist_ok=True)
    execution_path = out / "launch_execution.json"

    record: dict[str, Any] = {
        "schema": LAUNCH_PLAN_SCHEMA,
        "created_at_utc": utc_stamp(),
        "dispatch_plan_path": str(plan_path),
        "run_id": str(plan.get("run_id") or ""),
        "dry_run": dry_run,
        "allow_openrouter_launch": allow_openrouter_launch,
        "status": "starting",
        "selected_batch": None,
        "openrouter_calls_launched": False,
        "trading_v4_mutation_performed": False,
        "errors": [],
        "notes": [],
    }

    if not plan:
        record.update({"status": "blocked", "errors": ["dispatch_plan_missing_or_empty"]})
        _write_json(execution_path, record)
        return record
    if plan.get("schema") != "parallel_swarm.openrouter_dispatch_plan.v1":
        record.update({"status": "blocked", "errors": ["unexpected_dispatch_plan_schema"]})
        _write_json(execution_path, record)
        return record
    if str(plan.get("status") or "") == "invalid":
        record.update({"status": "blocked", "errors": ["dispatch_plan_invalid"]})
        _write_json(execution_path, record)
        return record

    batch = _next_planned_batch(plan)
    if not batch:
        record.update({"status": "no_batches_remaining", "notes": ["No planned launch batches remain."]})
        _write_json(execution_path, record)
        return record

    record["selected_batch"] = dict(batch)
    if dry_run:
        batch["status"] = "dry_run_ready"
        batch["dry_run_checked_at_utc"] = utc_stamp()
        record.update(
            {
                "status": "dry_run_ready",
                "notes": ["Dry-run only; no workers were launched and no OpenRouter calls were made."],
            }
        )
    elif not allow_openrouter_launch:
        record.update(
            {
                "status": "blocked",
                "errors": ["explicit_openrouter_launch_approval_required"],
                "notes": ["Set dry_run=false and allow_openrouter_launch=true only after explicit approval."],
            }
        )
    else:
        batch["status"] = "launch_ready_requires_runner"
        batch["launch_ready_at_utc"] = utc_stamp()
        record.update(
            {
                "status": "launch_ready_requires_runner",
                "notes": [
                    "Explicit launch approval flag was provided, but this adapter does not perform network calls.",
                    "A separately-gated runner must consume this batch and write pilot_result.json artifacts.",
                ],
            }
        )

    _write_json(plan_path, plan)
    manifest_path = plan.get("manifest_path")
    if manifest_path:
        manifest = load_manifest(manifest_path)
        manifest["last_launch_execution_path"] = str(execution_path)
        manifest["last_launch_status"] = record["status"]
        manifest["openrouter_calls_launched_by_controller"] = bool(
            manifest.get("openrouter_calls_launched_by_controller", False) or record["openrouter_calls_launched"]
        )
        manifest.setdefault("launch_executions", [])
        if str(execution_path) not in manifest["launch_executions"]:
            manifest["launch_executions"].append(str(execution_path))
        save_manifest(manifest, manifest_path)
        record["manifest_path"] = str(manifest_path)
    _write_json(execution_path, record)
    return record


def write_launch_execution_markdown(record: dict[str, Any], *, output_dir: str | Path) -> str:
    """Write a concise Markdown report for a launch execution record."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "launch_execution.md"
    selected = record.get("selected_batch") if isinstance(record.get("selected_batch"), dict) else {}
    lines = [
        "# OpenRouter Wave Launch Execution Report",
        "",
        f"- Run ID: `{record.get('run_id')}`",
        f"- Status: `{record.get('status')}`",
        f"- Dry run: `{record.get('dry_run')}`",
        f"- Allow OpenRouter launch: `{record.get('allow_openrouter_launch')}`",
        f"- OpenRouter calls launched: `{record.get('openrouter_calls_launched')}`",
        f"- Trading V4 mutation performed: `{record.get('trading_v4_mutation_performed')}`",
        f"- Selected batch: `{selected.get('batch_index', 'none')}`",
        f"- Worker IDs: `{', '.join(selected.get('worker_ids') or [])}`",
        "",
        "## Errors",
        "",
        f"- {', '.join(record.get('errors') or []) or 'none'}",
        "",
        "## Notes",
        "",
    ]
    for note in record.get("notes") or []:
        lines.append(f"- {note}")
    if not record.get("notes"):
        lines.append("- none")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)
