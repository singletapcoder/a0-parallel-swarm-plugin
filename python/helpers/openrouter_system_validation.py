"""No-network end-to-end validation for the OpenRouter parallel-swarm path.

This module validates the orchestration mechanics without spending tokens or
mutating Trading V4:

1. dispatch plan creation;
2. launch-executor dry-run;
3. synthetic OpenRouter worker artifact writing through the real worker path;
4. lane-lead aggregation over those artifacts;
5. monitor reporting for registered scheduler tasks, including overdue running
   task detection against a fixture tasks file.

It deliberately monkeypatches the OpenRouter HTTP call inside the process. It
must never read real OpenRouter credentials or perform network calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from plugins.parallel_swarm.python.helpers.openrouter_lane_lead import build_lane_lead_report
from plugins.parallel_swarm.python.helpers.openrouter_wave_controller import (
    load_manifest,
    register_scheduler_task,
    save_manifest,
    write_monitor_report,
)
from plugins.parallel_swarm.python.helpers.openrouter_wave_dispatcher import build_dispatch_plan, write_dispatch_markdown
from plugins.parallel_swarm.python.helpers.openrouter_wave_launch_executor import execute_next_launch_batch, write_launch_execution_markdown
from plugins.parallel_swarm.python.helpers.pilot_launcher import run_one_openrouter_payload
import plugins.parallel_swarm.python.helpers.openrouter_worker as openrouter_worker


VALIDATION_SCHEMA = "parallel_swarm.openrouter_system_validation.v1"


def _payload(task_id: str, lane: str, allowed: list[str], forbidden: list[str]) -> dict[str, Any]:
    return {
        "id": task_id,
        "backend": "openrouter",
        "model": "z-ai/glm-5.2",
        "role": "coder",
        "lane": lane,
        "description": f"Synthetic {lane} worker validation",
        "message": "Return NO_PATCH for synthetic validation. Do not mutate files.",
        "context_repo_path": "/a0/usr/workdir/TradingV4_phase0_work",
        "include_allowed_file_context": True,
        "allowed_file_globs": allowed,
        "read_only_context_files": ["docs/API_CONTROL_PLANE.md", "docs/BROKER_ADAPTER_POSTURE.md"],
        "forbidden_file_globs": forbidden,
        "strict_diff": True,
        "validate_git_apply": True,
        "fallback_policy": "stop_not_direct_code",
    }


def _fake_openrouter_call(model: str, prompt: str, *, api_key: str, system_message: str | None = None) -> tuple[str, dict[str, Any]]:
    content = "NO_PATCH: synthetic validation worker intentionally produced no patch."
    return content, {
        "prompt_tokens": 10,
        "completion_tokens": 7,
        "total_tokens": 17,
        "raw_usage": {"synthetic": True},
        "usage_confidence": "exact",
        "usage_missing_fields": [],
        "response_character_count": len(content),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


async def _run_synthetic_workers(payloads: list[dict[str, Any]], workers_dir: Path) -> list[dict[str, Any]]:
    original_call = openrouter_worker._call_openrouter_sync
    old_key = os.environ.get("OPENROUTER_API_KEY")
    openrouter_worker._call_openrouter_sync = _fake_openrouter_call
    os.environ["OPENROUTER_API_KEY"] = "synthetic-validation-key-not-real"
    try:
        records: list[dict[str, Any]] = []
        for payload in payloads:
            record = await run_one_openrouter_payload(payload, run_out=workers_dir / str(payload["id"]))
            records.append(record)
        return records
    finally:
        openrouter_worker._call_openrouter_sync = original_call
        if old_key is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = old_key


def run_no_network_validation(output_dir: str | Path, *, clean: bool = True) -> dict[str, Any]:
    """Run deterministic no-network validation and return a result record."""
    out = Path(output_dir)
    if clean and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    payloads = [
        _payload("VAL_M5_001", "M5", ["tests/test_position_accounting.py"], ["src/trading_v4/api/security.py"]),
        _payload("VAL_M7_001", "M7", ["tests/test_broker_adapters.py", "src/trading_v4/broker/**"], ["tests/test_position_accounting.py"]),
    ]

    result: dict[str, Any] = {
        "schema": VALIDATION_SCHEMA,
        "status": "started",
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "output_dir": str(out),
        "real_openrouter_network_calls": False,
        "real_openrouter_credentials_read": False,
        "trading_v4_mutation_performed": False,
        "scheduler_mutation_performed": False,
        "steps": {},
    }
    _write_json(out / "validation_result.json", result)

    plan = build_dispatch_plan(
        run_id="parallel_system_validation_no_network",
        output_dir=out / "dispatch",
        worker_payloads=payloads,
        max_workers_total=4,
        max_concurrency=2,
        token_budget=1000,
        cost_budget_usd=0.01,
        dry_run=True,
    )
    write_dispatch_markdown(plan, output_dir=out / "dispatch")
    assert plan["status"] == "planned_dry_run"
    assert plan["openrouter_calls_launched"] is False
    result["steps"]["dispatch"] = {
        "status": "pass",
        "dispatch_plan": str(out / "dispatch" / "dispatch_plan.json"),
        "manifest_path": plan["manifest_path"],
    }
    _write_json(out / "validation_result.json", result)

    launch = execute_next_launch_batch(out / "dispatch" / "dispatch_plan.json", output_dir=out / "launch", dry_run=True)
    write_launch_execution_markdown(launch, output_dir=out / "launch")
    assert launch["status"] == "dry_run_ready"
    assert launch["openrouter_calls_launched"] is False
    result["steps"]["launch_dry_run"] = {"status": "pass", "launch_execution": str(out / "launch" / "launch_execution.json")}
    _write_json(out / "validation_result.json", result)

    records = asyncio.run(_run_synthetic_workers(payloads, out / "workers"))
    assert all(record["status"] == "completed" for record in records)
    assert all(record["auditable_worker_result"] is True for record in records)
    result["steps"]["synthetic_workers"] = {
        "status": "pass",
        "worker_count": len(records),
        "worker_records": [str(out / "workers" / str(payload["id"]) / "pilot_result.json") for payload in payloads],
    }
    _write_json(out / "validation_result.json", result)

    candidate_dirs = [out / "workers" / str(payload["id"]) / "tasks" / str(payload["id"]) for payload in payloads]
    lane_report = build_lane_lead_report(candidate_dirs, output_dir=out / "lane_lead")
    assert lane_report["schema"] == "parallel_swarm.openrouter_lane_lead_report.v1"
    assert lane_report.get("candidate_count") == len(payloads)
    result["steps"]["lane_lead"] = {
        "status": "pass",
        "candidate_count": lane_report.get("candidate_count"),
        "lane_lead_report": str(out / "lane_lead" / "lane_lead_report.json"),
    }
    _write_json(out / "validation_result.json", result)

    manifest_path = Path(plan["manifest_path"])
    manifest = load_manifest(manifest_path)
    register_scheduler_task(manifest, uuid="synthetic_done", name="Synthetic done worker", stage="worker", prompt_path="done.md")
    register_scheduler_task(manifest, uuid="synthetic_stuck", name="Synthetic stuck worker", stage="worker", prompt_path="stuck.md")
    save_manifest(manifest, manifest_path)
    now = datetime.now(timezone.utc)
    tasks_fixture = out / "tasks_fixture.json"
    _write_json(
        tasks_fixture,
        {
            "tasks": [
                {
                    "uuid": "synthetic_done",
                    "name": "Synthetic done worker",
                    "state": "idle",
                    "type": "planned",
                    "updated_at": now.isoformat().replace("+00:00", "Z"),
                    "last_run": now.isoformat().replace("+00:00", "Z"),
                    "next_run": None,
                    "last_result": "done",
                },
                {
                    "uuid": "synthetic_stuck",
                    "name": "Synthetic stuck worker",
                    "state": "running",
                    "type": "planned",
                    "updated_at": (now - timedelta(minutes=90)).isoformat().replace("+00:00", "Z"),
                    "last_run": None,
                    "next_run": None,
                    "last_result": None,
                },
            ]
        },
    )
    monitor = write_monitor_report(load_manifest(manifest_path), output_dir=out / "monitor", tasks_path=tasks_fixture, scheduler_timeout_minutes=30)
    scheduler_summary = monitor["scheduler_task_summary"]
    assert scheduler_summary["registered_count"] == 2
    assert scheduler_summary["found_count"] == 2
    assert scheduler_summary["running_count"] == 1
    assert scheduler_summary["overdue_count"] == 1
    assert scheduler_summary["mutates_scheduler"] is False
    assert "overdue_registered_scheduler_tasks" in monitor.get("monitor_alerts", [])
    result["steps"]["monitor"] = {
        "status": "pass",
        "monitor_report": str(out / "monitor" / "wave_controller_monitor_report.json"),
        "registered_count": scheduler_summary["registered_count"],
        "overdue_count": scheduler_summary["overdue_count"],
        "mutates_scheduler": scheduler_summary["mutates_scheduler"],
    }

    result["status"] = "pass"
    result["completed_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_json(out / "validation_result.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run no-network OpenRouter parallel-swarm system validation.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--no-clean", action="store_true")
    args = parser.parse_args(argv)
    result = run_no_network_validation(args.output_dir, clean=not args.no_clean)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
