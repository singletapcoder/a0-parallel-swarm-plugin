"""Tests for OpenRouter-backed Parallel Swarm worker mode."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.parallel_swarm.python.helpers.artifacts import classify_candidate_response, extract_diff_block, validate_candidate_patch
from plugins.parallel_swarm.python.helpers.openrouter_worker import OpenRouterUnavailable, build_openrouter_system_message, normalize_usage, run_openrouter_task


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

    def fake_call(model, prompt, *, api_key, system_message=None):
        assert model == "qwen/qwen-2.5-coder-32b-instruct"
        assert "Forbidden actions" in prompt
        assert system_message
        assert "Produce candidate patches only" in system_message
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



def test_validate_candidate_patch_rejects_empty_patch():
    result = validate_candidate_patch("", ["tests/test_example.py"])
    assert result["status"] == "invalid"
    assert "empty_patch" in result["reasons"]


def test_validate_candidate_patch_accepts_allowed_complete_diff():
    diff = """diff --git a/tests/test_example.py b/tests/test_example.py
--- a/tests/test_example.py
+++ b/tests/test_example.py
@@ -1,1 +1,2 @@
 old
+new
"""
    result = validate_candidate_patch(diff, ["tests/test_example.py"])
    assert result["status"] == "valid_basic"
    assert result["touched_files"] == ["tests/test_example.py"]
    assert result["allowed_files_violated"] == []


def test_validate_candidate_patch_flags_disallowed_file():
    diff = """diff --git a/src/unsafe.py b/src/unsafe.py
--- a/src/unsafe.py
+++ b/src/unsafe.py
@@ -1,1 +1,2 @@
 old
+new
"""
    result = validate_candidate_patch(diff, ["tests/test_example.py"])
    assert result["status"] == "invalid"
    assert result["allowed_files_violated"] == ["src/unsafe.py"]
    assert "allowed_files_violated" in result["reasons"]


def test_normalize_usage_marks_missing_provider_fields():
    usage = normalize_usage({"prompt_tokens": None, "completion_tokens": 5}, "abc")
    assert usage["usage_confidence"] == "provider_missing"
    assert usage["response_character_count"] == 3
    assert "prompt_tokens" in usage["usage_missing_fields"]
    assert "total_tokens" in usage["usage_missing_fields"]



def test_build_openrouter_system_message_strict_diff_mode(tmp_path):
    task = _task(tmp_path, strict_diff=True)
    message = build_openrouter_system_message(task)
    assert "STRICT OUTPUT MODE" in message
    assert "raw unified diff only" in message
    assert "NO_PATCH" in message
    assert "BLOCKED_FOR_SAFETY_BOUNDARY" in message
    assert "Do not use markdown fences" in message


@pytest.mark.asyncio
async def test_openrouter_task_strict_diff_uses_strict_system_message(tmp_path, monkeypatch):
    import plugins.parallel_swarm.python.helpers.openrouter_worker as worker

    captured = {}

    def fake_call(model, prompt, *, api_key, system_message=None):
        captured["system_message"] = system_message
        return "NO_PATCH: already covered", {"total_tokens": 4}

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-real")
    monkeypatch.setattr(worker, "_call_openrouter_sync", fake_call)
    task = _task(tmp_path, strict_diff=True)
    result, usage = await run_openrouter_task(task)
    assert usage["total_tokens"] == 4
    assert "NO_PATCH" in result
    assert "STRICT OUTPUT MODE" in captured["system_message"]
    metadata = json.loads((Path(task.output_dir) / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["strict_diff"] is True
    assert metadata["system_message_mode"] == "strict_diff"


def test_classify_candidate_response_rejects_forbidden_glob():
    diff = "diff --git a/src/unsafe.py b/src/unsafe.py\n--- a/src/unsafe.py\n+++ b/src/unsafe.py\n@@ -1 +1,2 @@\n x = 1\n+y = 2\n"
    result = classify_candidate_response(diff, [], ["src/*.py"], ["src/unsafe.py"])
    assert result["classification"] == "unsafe_or_out_of_scope"
    assert result["patch_validation"]["forbidden_file_globs_violated"] == ["src/unsafe.py"]
