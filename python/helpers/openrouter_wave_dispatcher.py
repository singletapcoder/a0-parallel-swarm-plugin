"""Bounded dispatcher planning for OpenRouter swarm waves.

The dispatcher turns worker payloads into a deterministic launch plan while
respecting worker-count, concurrency, lane, token, and cost bounds. It is
report-only by default: it does not call OpenRouter, create scheduler tasks, or
mutate Trading V4.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from plugins.parallel_swarm.python.helpers.openrouter_wave_controller import create_wave_manifest, save_manifest, utc_stamp


DEFAULT_MAX_WORKERS_TOTAL = 35
DEFAULT_MAX_CONCURRENCY = 10


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def validate_dispatch_inputs(
    worker_payloads: list[dict[str, Any]],
    *,
    max_workers_total: int = DEFAULT_MAX_WORKERS_TOTAL,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> dict[str, Any]:
    """Validate worker dispatch bounds and return deterministic diagnostics."""
    errors: list[str] = []
    warnings: list[str] = []
    if max_workers_total < 1:
        errors.append("max_workers_total_must_be_positive")
    if max_workers_total > DEFAULT_MAX_WORKERS_TOTAL:
        errors.append("max_workers_total_exceeds_hard_cap_35")
    if max_concurrency < 1:
        errors.append("max_concurrency_must_be_positive")
    if max_concurrency > max_workers_total:
        warnings.append("max_concurrency_reduced_to_worker_total")
    if len(worker_payloads) > max_workers_total:
        errors.append("worker_payload_count_exceeds_max_workers_total")
    seen: set[str] = set()
    duplicate_ids: list[str] = []
    for idx, payload in enumerate(worker_payloads, start=1):
        task_id = str(payload.get("id") or f"worker_{idx:03d}")
        if task_id in seen and task_id not in duplicate_ids:
            duplicate_ids.append(task_id)
        seen.add(task_id)
        if str(payload.get("backend") or "openrouter").lower() != "openrouter":
            errors.append(f"worker_{task_id}_backend_not_openrouter")
    if duplicate_ids:
        errors.append("duplicate_worker_ids")
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "duplicate_worker_ids": duplicate_ids,
        "worker_payload_count": len(worker_payloads),
        "max_workers_total": max_workers_total,
        "max_concurrency": min(max_concurrency, max_workers_total) if max_workers_total > 0 else max_concurrency,
    }


def build_dispatch_plan(
    *,
    run_id: str,
    output_dir: str | Path,
    worker_payloads: list[dict[str, Any]],
    max_workers_total: int = DEFAULT_MAX_WORKERS_TOTAL,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    token_budget: int | None = None,
    cost_budget_usd: float | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Create a durable manifest and launch plan without running workers."""
    validation = validate_dispatch_inputs(
        worker_payloads,
        max_workers_total=max_workers_total,
        max_concurrency=max_concurrency,
    )
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    plan_path = out / "dispatch_plan.json"
    if not validation["valid"]:
        plan = {
            "schema": "parallel_swarm.openrouter_dispatch_plan.v1",
            "run_id": run_id,
            "created_at_utc": utc_stamp(),
            "status": "invalid",
            "dry_run": dry_run,
            "validation": validation,
            "launch_batches": [],
            "trading_v4_mutation_performed": False,
            "openrouter_calls_launched": False,
        }
        _write_json(plan_path, plan)
        return plan

    effective_concurrency = int(validation["max_concurrency"])
    manifest = create_wave_manifest(
        run_id=run_id,
        output_dir=out,
        max_workers_total=max_workers_total,
        max_concurrency=effective_concurrency,
        token_budget=token_budget,
        cost_budget_usd=cost_budget_usd,
        worker_payloads=worker_payloads,
    )
    batches: list[dict[str, Any]] = []
    for batch_index, start in enumerate(range(0, len(worker_payloads), effective_concurrency), start=1):
        payload_batch = worker_payloads[start : start + effective_concurrency]
        batches.append(
            {
                "batch_index": batch_index,
                "worker_ids": [str(payload.get("id") or f"worker_{start + idx + 1:03d}") for idx, payload in enumerate(payload_batch)],
                "size": len(payload_batch),
                "status": "planned",
            }
        )
    plan = {
        "schema": "parallel_swarm.openrouter_dispatch_plan.v1",
        "run_id": run_id,
        "created_at_utc": utc_stamp(),
        "status": "planned_dry_run" if dry_run else "planned_requires_explicit_launch",
        "dry_run": dry_run,
        "validation": validation,
        "manifest_path": str(out / "wave_manifest.json"),
        "launch_batches": batches,
        "worker_payload_count": len(worker_payloads),
        "max_workers_total": max_workers_total,
        "max_concurrency": effective_concurrency,
        "token_budget": token_budget,
        "cost_budget_usd": cost_budget_usd,
        "trading_v4_mutation_performed": False,
        "openrouter_calls_launched": False,
        "notes": [
            "Dispatcher plan only; workers are not launched by this helper.",
            "A future explicit launch step must consume this plan and update the wave manifest from worker result artifacts.",
        ],
    }
    manifest["dispatcher_plan_path"] = str(plan_path)
    manifest["dispatch_status"] = plan["status"]
    save_manifest(manifest, out / "wave_manifest.json")
    _write_json(plan_path, plan)
    return plan


def write_dispatch_markdown(plan: dict[str, Any], *, output_dir: str | Path) -> str:
    """Write a concise dispatch plan Markdown report."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "dispatch_plan.md"
    validation = plan.get("validation") if isinstance(plan.get("validation"), dict) else {}
    lines = [
        "# OpenRouter Wave Dispatch Plan",
        "",
        f"- Run ID: `{plan.get('run_id')}`",
        f"- Status: `{plan.get('status')}`",
        f"- Dry run: `{plan.get('dry_run')}`",
        f"- Worker payloads: `{plan.get('worker_payload_count', validation.get('worker_payload_count', 0))}`",
        f"- Max workers total: `{plan.get('max_workers_total', validation.get('max_workers_total'))}`",
        f"- Max concurrency: `{plan.get('max_concurrency', validation.get('max_concurrency'))}`",
        f"- OpenRouter calls launched: `{plan.get('openrouter_calls_launched')}`",
        f"- Trading V4 mutation performed: `{plan.get('trading_v4_mutation_performed')}`",
        "",
        "## Validation",
        "",
        f"- Valid: `{validation.get('valid')}`",
        f"- Errors: `{', '.join(validation.get('errors') or []) or 'none'}`",
        f"- Warnings: `{', '.join(validation.get('warnings') or []) or 'none'}`",
        "",
        "## Launch Batches",
        "",
    ]
    for batch in plan.get("launch_batches") or []:
        lines.append(f"- Batch {batch.get('batch_index')}: `{batch.get('size')}` workers — {', '.join(batch.get('worker_ids') or [])}")
    if not plan.get("launch_batches"):
        lines.append("- none")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)
