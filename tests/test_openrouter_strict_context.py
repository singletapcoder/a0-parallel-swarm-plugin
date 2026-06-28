"""Tests for context-enriched strict-diff OpenRouter workflow."""

import json
import subprocess
from pathlib import Path

from plugins.parallel_swarm.python.helpers.artifacts import extract_diff_block, git_apply_check, validate_candidate_patch, write_openrouter_artifacts
from plugins.parallel_swarm.python.helpers.model_router import TaskComplexity
from plugins.parallel_swarm.python.helpers.swarm import SwarmTask
from plugins.parallel_swarm.python.helpers.trading_v4_policy import build_trading_v4_worker_prompt


def _task(tmp_path, **kwargs):
    data = {
        "id": "ctx",
        "description": "desc",
        "message": "change the allowed file",
        "complexity": TaskComplexity.SIMPLE,
        "backend": "openrouter",
        "model": "deepseek/deepseek-chat",
        "output_dir": str(tmp_path / "out"),
        "allowed_files": ["tests/test_example.py"],
        "context_repo_path": str(tmp_path / "repo"),
        "include_allowed_file_context": True,
        "strict_diff": True,
        "validate_git_apply": True,
    }
    data.update(kwargs)
    return SwarmTask(**data)


def test_prompt_includes_allowed_file_context_and_strict_contract(tmp_path):
    repo = tmp_path / "repo"
    target = repo / "tests" / "test_example.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_existing():\n    assert True\n", encoding="utf-8")
    prompt = build_trading_v4_worker_prompt(_task(tmp_path))
    assert "--- BEGIN FILE: tests/test_example.py ---" in prompt
    assert "def test_existing" in prompt
    assert "Return exactly one of the following" in prompt
    assert "Do not wrap the diff in markdown fences" in prompt


def test_strict_diff_extracts_raw_diff_and_rejects_prose():
    diff = "diff --git a/tests/test_example.py b/tests/test_example.py\n--- a/tests/test_example.py\n+++ b/tests/test_example.py\n@@ -1 +1,2 @@\n def test_existing():\n+    assert True\n"
    assert extract_diff_block(diff, strict_diff=True) == diff
    assert extract_diff_block("Here is a patch\n" + diff, strict_diff=True) == ""
    assert extract_diff_block("NO_PATCH: nothing safe", strict_diff=True) == ""


def test_validate_candidate_patch_accepts_file_headers_and_rejects_forbidden_files():
    diff = "--- a/tests/test_example.py\n+++ b/tests/test_example.py\n@@ -1 +1,2 @@\n def test_existing():\n+    assert True\n"
    validation = validate_candidate_patch(diff, ["tests/test_example.py"])
    assert validation["status"] == "valid_basic"
    assert validation["touched_files"] == ["tests/test_example.py"]

    bad = diff.replace("tests/test_example.py", "src/live_broker.py")
    validation = validate_candidate_patch(bad, ["tests/test_example.py"])
    assert validation["status"] == "invalid"
    assert validation["allowed_files_violated"] == ["src/live_broker.py"]


def test_git_apply_check_is_non_mutating(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    target = repo / "example.txt"
    target.write_text("old\n", encoding="utf-8")
    diff = "diff --git a/example.txt b/example.txt\n--- a/example.txt\n+++ b/example.txt\n@@ -1 +1 @@\n-old\n+new\n"
    result = git_apply_check(diff, str(repo))
    assert result["status"] == "ok"
    assert target.read_text(encoding="utf-8") == "old\n"


def test_write_openrouter_artifacts_records_git_apply_check(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "test_example.py").write_text("def test_existing():\n    assert True\n", encoding="utf-8")
    task = _task(tmp_path)
    diff = "diff --git a/tests/test_example.py b/tests/test_example.py\n--- a/tests/test_example.py\n+++ b/tests/test_example.py\n@@ -1,2 +1,3 @@\n def test_existing():\n     assert True\n+    assert 1 == 1\n"
    paths = write_openrouter_artifacts(task, "prompt", diff, {"status": "completed"})
    metadata = json.loads(Path(paths["metadata_path"]).read_text(encoding="utf-8"))
    assert metadata["strict_diff"] is True
    assert metadata["include_allowed_file_context"] is True
    assert metadata["patch_validation"]["status"] == "valid_basic"
    assert metadata["patch_validation"]["git_apply_check"]["status"] == "ok"
