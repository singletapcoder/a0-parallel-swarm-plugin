"""Tests for deterministic one-shot OpenRouter pilot launcher."""

import json
from pathlib import Path

import pytest

from plugins.parallel_swarm.python.helpers.model_router import TaskComplexity
from plugins.parallel_swarm.python.helpers.pilot_launcher import run_one_openrouter_payload, task_from_payload


def test_task_from_payload_preserves_openrouter_fields(tmp_path):
    task = task_from_payload(
        {
            "id": "M5_003",
            "description": "desc",
            "message": "msg",
            "backend": "openrouter",
            "model": "deepseek/deepseek-chat",
            "complexity": "simple",
            "allowed_files": ["tests/test_example.py"],
            "unknown": "ignored",
        },
        output_dir=str(tmp_path / "task"),
    )
    assert task.id == "M5_003"
    assert task.backend == "openrouter"
    assert task.model == "deepseek/deepseek-chat"
    assert task.complexity == TaskComplexity.SIMPLE
    assert task.output_dir == str(tmp_path / "task")
    assert task.allowed_files == ["tests/test_example.py"]


@pytest.mark.asyncio
async def test_run_one_openrouter_payload_writes_started_and_completed_records(tmp_path, monkeypatch):
    import plugins.parallel_swarm.python.helpers.pilot_launcher as launcher

    async def fake_run_openrouter_task(task):
        Path(task.output_dir).mkdir(parents=True, exist_ok=True)
        Path(task.output_dir, "metadata.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        return "worker result", {"total_tokens": 3, "usage_confidence": "exact"}

    monkeypatch.setattr(launcher, "run_openrouter_task", fake_run_openrouter_task)
    record = await run_one_openrouter_payload(
        {
            "id": "M5_003",
            "description": "desc",
            "message": "msg",
            "backend": "openrouter",
            "model": "deepseek/deepseek-chat",
            "fallback_policy": "stop_not_direct_code",
        },
        run_out=tmp_path / "run",
    )
    result_path = tmp_path / "run" / "pilot_result.json"
    assert result_path.exists()
    saved = json.loads(result_path.read_text())
    assert record["status"] == "completed"
    assert saved["auditable_worker_result"] is True
    assert saved["real_openrouter_call_attempted"] is True
    assert saved["usage"]["total_tokens"] == 3


@pytest.mark.asyncio
async def test_run_one_openrouter_payload_blocks_non_openrouter_backend(tmp_path):
    record = await run_one_openrouter_payload(
        {
            "id": "bad_backend",
            "description": "desc",
            "message": "msg",
            "backend": "agent_zero",
            "model": "deepseek/deepseek-chat",
        },
        run_out=tmp_path / "run",
    )
    saved = json.loads((tmp_path / "run" / "pilot_result.json").read_text())
    assert record["status"] == "blocked"
    assert saved["status"] == "blocked"
    assert "backend must be openrouter" in saved["error"]


@pytest.mark.asyncio
async def test_run_one_openrouter_payload_records_blocked_adapter_error(tmp_path, monkeypatch):
    import plugins.parallel_swarm.python.helpers.pilot_launcher as launcher
    from plugins.parallel_swarm.python.helpers.openrouter_worker import OpenRouterUnavailable

    async def fake_run_openrouter_task(task):
        raise OpenRouterUnavailable("BLOCKED_OPENROUTER_UNAVAILABLE: fake")

    monkeypatch.setattr(launcher, "run_openrouter_task", fake_run_openrouter_task)
    record = await run_one_openrouter_payload(
        {
            "id": "blocked",
            "description": "desc",
            "message": "msg",
            "backend": "openrouter",
            "model": "deepseek/deepseek-chat",
        },
        run_out=tmp_path / "run",
    )
    assert record["status"] == "blocked"
    assert "BLOCKED_OPENROUTER_UNAVAILABLE" in record["error"]



def test_task_from_payload_preserves_context_and_strict_diff_fields(tmp_path):
    task = task_from_payload(
        {
            "id": "M4_001",
            "description": "desc",
            "message": "msg",
            "backend": "openrouter",
            "model": "deepseek/deepseek-chat",
            "context_repo_path": str(tmp_path),
            "include_allowed_file_context": True,
            "strict_diff": True,
            "validate_git_apply": True,
        },
        output_dir=str(tmp_path / "task"),
    )
    assert task.context_repo_path == str(tmp_path)
    assert task.include_allowed_file_context is True
    assert task.strict_diff is True
    assert task.validate_git_apply is True


@pytest.mark.asyncio
async def test_run_one_openrouter_payload_records_patch_validation(tmp_path, monkeypatch):
    import plugins.parallel_swarm.python.helpers.pilot_launcher as launcher

    async def fake_run_openrouter_task(task):
        Path(task.output_dir).mkdir(parents=True, exist_ok=True)
        Path(task.output_dir, "metadata.json").write_text(
            json.dumps({"patch_validation": {"status": "valid_basic", "touched_files": ["tests/test_example.py"]}}),
            encoding="utf-8",
        )
        return "worker result", {"total_tokens": 3, "usage_confidence": "exact"}

    monkeypatch.setattr(launcher, "run_openrouter_task", fake_run_openrouter_task)
    record = await run_one_openrouter_payload(
        {
            "id": "strict",
            "description": "desc",
            "message": "msg",
            "backend": "openrouter",
            "model": "deepseek/deepseek-chat",
            "strict_diff": True,
            "validate_git_apply": True,
        },
        run_out=tmp_path / "run",
    )
    assert record["patch_validation"]["status"] == "valid_basic"
    saved = json.loads((tmp_path / "run" / "pilot_result.json").read_text())
    assert saved["strict_diff"] is True
    assert saved["validate_git_apply"] is True
    assert saved["patch_validation"]["touched_files"] == ["tests/test_example.py"]
