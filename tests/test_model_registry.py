"""Tests for Parallel Swarm role/model registry."""

from plugins.parallel_swarm.python.helpers.model_registry import DEFAULT_ROLE_MODEL_REGISTRY, resolve_model_for_role
from plugins.parallel_swarm.python.helpers.pilot_launcher import task_from_payload


def test_explicit_model_wins_over_role():
    resolved = resolve_model_for_role("cheap_coder", "qwen/qwen-2.5-coder-32b-instruct")
    assert resolved.model == "qwen/qwen-2.5-coder-32b-instruct"
    assert resolved.source == "explicit"


def test_known_role_resolves_to_pinned_model():
    resolved = resolve_model_for_role("cheap_coder", "")
    assert resolved.model == DEFAULT_ROLE_MODEL_REGISTRY["cheap_coder"]
    assert resolved.source == "role_registry"
    assert resolved.known_role is True


def test_unknown_role_fails_unresolved():
    resolved = resolve_model_for_role("made_up_role", "")
    assert resolved.model == ""
    assert resolved.source == "unresolved"
    assert resolved.known_role is False


def test_task_from_payload_resolves_role_when_model_missing(tmp_path):
    task = task_from_payload(
        {
            "id": "role_task",
            "description": "desc",
            "message": "msg",
            "backend": "openrouter",
            "role": "cheap_coder",
        },
        output_dir=str(tmp_path / "task"),
    )
    assert task.model == DEFAULT_ROLE_MODEL_REGISTRY["cheap_coder"]
