"""Tests for OpenRouter-backed Parallel Swarm worker mode."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.parallel_swarm.python.helpers.artifacts import extract_diff_block
from plugins.parallel_swarm.python.helpers.openrouter_worker import OpenRouterUnavailable, run_openrouter_task


def _task(tmp_path, **overrides):
    data = dict(
        id="M5_001",
        description="test task",
        message="Produce a candidate patch.",
        backend="openrouter",
        model="qwen/qwen-2.5-coder-32b-instruct",
        role="cheap_coder",
        lane="M5",
        fallback_policy="stop_not_direct_code",
        output_dir=str(tmp_path / "task"),
        allowed_files=["tests/test_example.py"],
        forbidden_actions=["broker_calls", "credential_resolution"],
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def test_extract_diff_block():
    text = "before\n```diff\n+hello\n```\nafter"
    assert extract_diff_block(text) == "+hello\n"


@pytest.mark.asyncio
async def test_openrouter_task_requires_model(tmp_path):
    task = _task(tmp_path, model="")
    with pytest.raises(OpenRouterUnavailable) as exc:
        await run_openrouter_task(task)
    assert "missing exact model" in str(exc.value)


@pytest.mark.asyncio
async def test_openrouter_task_blocks_without_api_key_and_writes_metadata(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    task = _task(tmp_path)
    with pytest.raises(OpenRouterUnavailable) as exc:
        await run_openrouter_task(task)
    assert "BLOCKED_OPENROUTER_UNAVAILABLE" in str(exc.value)
    meta = Path(task.output_dir) / "metadata.json"
    assert meta.exists()
    payload = json.loads(meta.read_text())
    assert payload["backend"] == "openrouter"
    assert payload["model"] == task.model
    assert payload["fallback_used"] is False
    assert payload["status"] == "blocked"


@pytest.mark.asyncio
async def test_openrouter_task_success_writes_artifacts(tmp_path, monkeypatch):
    import plugins.parallel_swarm.python.helpers.openrouter_worker as worker

    def fake_call(model, prompt, *, api_key):
        assert model == "qwen/qwen-2.5-coder-32b-instruct"
        assert "Forbidden actions" in prompt
        return "# Worker Result\n\n## Candidate Patch\n```diff\n+ok\n```", {"total_tokens": 12}

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-real")
    monkeypatch.setattr(worker, "_call_openrouter_sync", fake_call)
    task = _task(tmp_path)
    result, usage = await run_openrouter_task(task)
    assert usage["total_tokens"] == 12
    assert "OpenRouter artifact metadata" in result
    assert (Path(task.output_dir) / "candidate_patch.diff").read_text() == "+ok\n"


@pytest.mark.asyncio
async def test_orchestrator_openrouter_backend_uses_adapter_not_agent_monologue(monkeypatch, tmp_path):
    """backend=openrouter must use the OpenRouter adapter, not normal Agent.monologue."""
    from plugins.parallel_swarm.python.helpers.swarm import SwarmOrchestrator, SwarmTask, TaskStatus
    import plugins.parallel_swarm.python.helpers.openrouter_worker as openrouter_worker

    task = SwarmTask(
        id="M8_001",
        description="OpenRouter route test",
        message="Produce a candidate patch.",
        backend="openrouter",
        model="qwen/qwen-2.5-coder-32b-instruct",
        role="cheap_coder",
        lane="M8",
        fallback_policy="stop_not_direct_code",
        output_dir=str(tmp_path / "M8_001"),
    )

    calls = {"openrouter": 0, "agent_init": 0}

    async def fake_run_openrouter_task(received_task):
        calls["openrouter"] += 1
        assert received_task is task
        return "openrouter result", {"total_tokens": 7}

    def fail_initialize_agent():
        calls["agent_init"] += 1
        raise AssertionError("initialize_agent/normal Agent monologue path must not run for backend=openrouter")

    class ParentAgent:
        number = 0
        context = object()

        async def call_extensions(self, *args, **kwargs):
            return None

    monkeypatch.setattr(openrouter_worker, "run_openrouter_task", fake_run_openrouter_task)

    # If the normal Agent Zero path is accidentally used, initialize_agent is imported
    # from the initialize module inside _execute_task. Make that path fail loudly.
    import initialize

    monkeypatch.setattr(initialize, "initialize_agent", fail_initialize_agent)

    orchestrator = SwarmOrchestrator(parent_agent=ParentAgent(), max_concurrency=1, token_budget=1000, per_task_budget=100)
    result = await orchestrator._execute_task(task)

    assert result == "openrouter result"
    assert task.status == TaskStatus.COMPLETED
    assert task.result == "openrouter result"
    assert task.tokens_used == 7
    assert calls == {"openrouter": 1, "agent_init": 0}
