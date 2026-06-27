"""Deterministic one-shot OpenRouter worker launcher for Parallel Swarm.

This helper productizes the Stage B project-side launcher into plugin-owned
code. It writes a result record before network activity starts, fails closed,
and does not fall back to Agent Zero subordinate monologue.
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from plugins.parallel_swarm.python.helpers.model_router import TaskComplexity
from plugins.parallel_swarm.python.helpers.openrouter_worker import OpenRouterUnavailable, run_openrouter_task
from plugins.parallel_swarm.python.helpers.swarm import SwarmTask


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_result(path: str | Path, record: dict[str, Any]) -> None:
    result_path = Path(path)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(record)
    payload["updated_at_utc"] = utc_stamp()
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _complexity(value: Any) -> TaskComplexity:
    raw = str(value or "moderate").lower()
    if raw == "simple":
        return TaskComplexity.SIMPLE
    if raw == "complex":
        return TaskComplexity.COMPLEX
    return TaskComplexity.MODERATE


def task_from_payload(payload: dict[str, Any], *, output_dir: str = "") -> SwarmTask:
    """Build a SwarmTask from JSON-ish payload while ignoring unknown keys."""
    allowed = {f.name for f in fields(SwarmTask)}
    data = {k: v for k, v in payload.items() if k in allowed}
    data.setdefault("id", str(payload.get("id") or "openrouter_task"))
    data.setdefault("description", str(payload.get("description") or data["id"]))
    data.setdefault("message", str(payload.get("message") or data["description"]))
    data["complexity"] = _complexity(payload.get("complexity", data.get("complexity")))
    data.setdefault("backend", "openrouter")
    data.setdefault("fallback_policy", "stop_not_direct_code")
    if output_dir:
        data["output_dir"] = output_dir
    return SwarmTask(**data)


async def run_one_openrouter_payload(payload: dict[str, Any], *, run_out: str | Path) -> dict[str, Any]:
    """Run exactly one OpenRouter task and persist a pilot_result.json record."""
    run_path = Path(run_out)
    result_path = run_path / "pilot_result.json"
    record: dict[str, Any] = {
        "status": "starting",
        "real_openrouter_call_attempted": False,
        "auditable_worker_result": False,
        "run_out": str(run_path),
    }
    write_result(result_path, record)

    task = task_from_payload(payload, output_dir=str(run_path / "tasks" / str(payload.get("id") or "openrouter_task")))
    record.update(
        {
            "status": "preflight_ok",
            "task_id": task.id,
            "backend": task.backend,
            "model": task.model,
            "fallback_policy": task.fallback_policy,
            "output_dir": task.output_dir,
        }
    )
    write_result(result_path, record)

    if task.backend.lower() != "openrouter":
        record.update({"status": "blocked", "error": "BLOCKED_OPENROUTER_UNAVAILABLE: backend must be openrouter"})
        write_result(result_path, record)
        return record

    record.update({"status": "started_http_request", "real_openrouter_call_attempted": True})
    write_result(result_path, record)

    try:
        result, usage = await run_openrouter_task(task)
        record.update(
            {
                "status": "completed",
                "auditable_worker_result": True,
                "usage": usage,
                "result_preview": result[:1200],
            }
        )
    except OpenRouterUnavailable as exc:
        record.update({"status": "blocked", "error": str(exc)})
    except Exception as exc:  # pragma: no cover - defensive evidence path
        record.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    write_result(result_path, record)
    return record
