"""Tests for OpenRouter candidate classification and normalization."""

import json
import subprocess
from pathlib import Path

from plugins.parallel_swarm.python.helpers.artifacts import classify_candidate_response, write_openrouter_artifacts
from plugins.parallel_swarm.python.helpers.model_router import TaskComplexity
from plugins.parallel_swarm.python.helpers.swarm import SwarmTask


def _diff(path="tests/test_example.py"):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1,2 @@\n old\n+new\n"


def _task(tmp_path, **kwargs):
    data = {
        "id": "classify",
        "description": "desc",
        "message": "msg",
        "complexity": TaskComplexity.SIMPLE,
        "backend": "openrouter",
        "model": "deepseek/deepseek-chat",
        "output_dir": str(tmp_path / "out"),
        "allowed_files": ["tests/test_example.py"],
        "context_repo_path": str(tmp_path / "repo"),
        "validate_git_apply": True,
    }
    data.update(kwargs)
    return SwarmTask(**data)


def test_classifies_raw_diff_as_usable():
    result = classify_candidate_response(_diff(), ["tests/test_example.py"])
    assert result["classification"] == "usable_raw_diff"
    assert result["has_raw_diff"] is True
    assert result["patch_validation"]["status"] == "valid_basic"


def test_classifies_fenced_diff_as_repairable():
    response = "Here is the change:\n```diff\n" + _diff().strip() + "\n```\n"
    result = classify_candidate_response(response, ["tests/test_example.py"])
    assert result["classification"] == "repairable_fenced_diff"
    assert result["has_fenced_diff"] is True
    assert result["normalized_patch"] == _diff()


def test_classifies_no_patch_and_safety_blocks():
    assert classify_candidate_response("NO_PATCH: already covered")["classification"] == "no_patch"
    assert classify_candidate_response("BLOCKED_FOR_SAFETY_BOUNDARY: broker call needed")["classification"] == "blocked_for_safety"


def test_classifies_prose_without_patch_as_idea_only():
    result = classify_candidate_response("This test could check a rounding edge.")
    assert result["classification"] == "idea_only"
    assert result["recommended_action"] == "manual_review_or_repair"


def test_rejects_out_of_scope_diff_even_when_repairable():
    response = "```diff\n" + _diff("src/live_broker.py").strip() + "\n```"
    result = classify_candidate_response(response, ["tests/test_example.py"])
    assert result["classification"] == "unsafe_or_out_of_scope"
    assert result["recommended_action"] == "reject"


def test_write_artifacts_preserves_normalized_fenced_diff_and_apply_check(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "test_example.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    task = _task(tmp_path)
    response = "Helpful explanation.\n```diff\n" + _diff().strip() + "\n```\n"
    paths = write_openrouter_artifacts(task, "prompt", response, {"status": "completed"})
    metadata = json.loads(Path(paths["metadata_path"]).read_text(encoding="utf-8"))
    assert Path(paths["candidate_patch_path"]).read_text(encoding="utf-8") == _diff()
    assert Path(paths["normalized_candidate_patch_path"]).read_text(encoding="utf-8") == _diff()
    assert metadata["candidate_classification"]["classification"] == "repairable_fenced_diff"
    assert metadata["candidate_classification"]["normalized_patch_validation"]["git_apply_check"]["status"] == "ok"
