"""Tests for context-enriched strict-diff OpenRouter workflow."""

import json
import subprocess
from pathlib import Path

from plugins.parallel_swarm.python.helpers.artifacts import extract_diff_block, git_apply_check, recompute_hunk_counts, repair_candidate_diff, validate_candidate_patch, write_openrouter_artifacts
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
    assert "--- BEGIN ALLOWED MUTATION FILE: tests/test_example.py" in prompt
    assert "def test_existing" in prompt
    assert "Return exactly one of the following" in prompt
    assert "Do not wrap the diff in markdown fences" in prompt


def test_prompt_includes_read_only_context_without_granting_mutation_authority(tmp_path):
    repo = tmp_path / "repo"
    allowed = repo / "docs" / "contract.md"
    readonly = repo / "src" / "trading_v4" / "api" / "runtime.py"
    allowed.parent.mkdir(parents=True)
    readonly.parent.mkdir(parents=True)
    allowed.write_text("old vocabulary\n", encoding="utf-8")
    readonly.write_text("def runtime_contract():\n    return 'read-only evidence'\n", encoding="utf-8")

    prompt = build_trading_v4_worker_prompt(
        _task(
            tmp_path,
            allowed_files=["docs/contract.md"],
            read_only_context_files=["src/trading_v4/api/runtime.py"],
            context_file_max_bytes=2000,
            context_total_max_bytes=5000,
        )
    )

    assert "--- BEGIN ALLOWED MUTATION FILE: docs/contract.md" in prompt
    assert "old vocabulary" in prompt
    assert "--- BEGIN READ-ONLY CONTEXT FILE: src/trading_v4/api/runtime.py" in prompt
    assert "read-only evidence" in prompt
    assert "Treat Read-only context files as non-mutable evidence" in prompt
    assert "Only touch files listed in Allowed files" in prompt


def test_prompt_expands_context_globs_with_limits_and_diagnostics(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)
    (repo / "tests" / "test_contract.py").write_text("assert 'contract'\n", encoding="utf-8")
    (repo / "docs" / "big.md").write_text("x" * 200, encoding="utf-8")

    prompt = build_trading_v4_worker_prompt(
        _task(
            tmp_path,
            allowed_files=[],
            allowed_file_globs=["tests/test_*.py"],
            read_only_context_globs=["docs/*.md"],
            context_file_max_bytes=50,
            context_total_max_bytes=120,
        )
    )

    assert "glob: tests/test_*.py" in prompt
    assert "--- BEGIN ALLOWED MUTATION FILE: tests/test_contract.py" in prompt
    assert "--- BEGIN READ-ONLY CONTEXT FILE: docs/big.md" in prompt
    assert "TRUNCATED_CONTEXT_FILE" in prompt
    assert "Context warnings:" in prompt


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
    assert metadata["context_manifest"]["include_requested"] is True
    assert metadata["context_manifest"]["files"][0]["path"] == "tests/test_example.py"
    assert metadata["patch_validation"]["status"] == "valid_basic"
    assert metadata["patch_validation"]["git_apply_check"]["status"] == "ok"


def test_validate_candidate_patch_honors_allowed_and_forbidden_globs():
    diff = "diff --git a/tests/test_contract.py b/tests/test_contract.py\n--- a/tests/test_contract.py\n+++ b/tests/test_contract.py\n@@ -1 +1,2 @@\n assert True\n+assert 1 == 1\n"
    validation = validate_candidate_patch(diff, [], ["tests/test_*.py"], [])
    assert validation["status"] == "valid_basic"
    assert validation["allowed_files_violated"] == []

    forbidden = validate_candidate_patch(diff, [], ["tests/test_*.py"], ["tests/test_contract.py"])
    assert forbidden["status"] == "invalid"
    assert forbidden["forbidden_file_globs_violated"] == ["tests/test_contract.py"]
    assert "forbidden_file_globs_violated" in forbidden["reasons"]


def test_context_glob_expansion_skips_cache_and_binary_files(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src" / "pkg" / "__pycache__").mkdir(parents=True)
    (repo / "src" / "pkg" / "module.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "src" / "pkg" / "__pycache__" / "module.pyc").write_bytes(b"\x00bad")

    prompt = build_trading_v4_worker_prompt(
        _task(
            tmp_path,
            allowed_files=[],
            allowed_file_globs=["src/pkg/**"],
            read_only_context_files=[],
        )
    )

    assert "--- BEGIN ALLOWED MUTATION FILE: src/pkg/module.py" in prompt
    assert "module.pyc" not in prompt
    assert "CONTEXT_FILE_NOT_UTF8" not in prompt


def test_repair_candidate_diff_fixes_stray_marker_before_diff_git_header():
    malformed = "--- diff --git a/tests/test_example.py b/tests/test_example.py\nindex 111..222 100644\n--- a/tests/test_example.py\n+++ b/tests/test_example.py\n@@ -1 +1,2 @@\n def test_existing():\n+    assert True\n"
    result = repair_candidate_diff(malformed)
    assert result["repaired"] is True
    assert result["repaired_text"].startswith("diff --git a/tests/test_example.py b/tests/test_example.py\n")
    assert "stripped_stray_marker_before_diff_git_header" in result["repairs_applied"]


def test_repair_candidate_diff_strips_leading_prose():
    malformed = "Here is the patch you asked for:\n\ndiff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n"
    result = repair_candidate_diff(malformed)
    assert result["repaired"] is True
    assert result["repaired_text"].startswith("diff --git a/a.txt b/a.txt\n")
    assert "stripped_leading_non_diff_lines" in result["repairs_applied"]


def test_repair_candidate_diff_is_noop_for_clean_diff():
    clean = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n"
    result = repair_candidate_diff(clean)
    assert result["repaired"] is False
    assert result["repaired_text"].strip() == clean.strip()


def test_repair_candidate_diff_reports_no_diff_header():
    result = repair_candidate_diff("NO_PATCH: nothing safe")
    assert result["repaired"] is False
    assert result["reason"] == "no_diff_header_found"


def test_write_artifacts_repairs_stray_marker_diff_and_git_applies(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    (repo / "tests").mkdir()
    target = repo / "tests" / "test_example.py"
    target.write_text("def test_existing():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)

    good = "diff --git a/tests/test_example.py b/tests/test_example.py\n--- a/tests/test_example.py\n+++ b/tests/test_example.py\n@@ -1,2 +1,3 @@\n def test_existing():\n     assert True\n+    assert 1 == 1\n"
    malformed = "--- " + good
    task = _task(tmp_path, context_repo_path=str(repo), allowed_files=["tests/test_example.py"])
    paths = write_openrouter_artifacts(task, "prompt", malformed, {"status": "completed"})
    metadata = json.loads(Path(paths["metadata_path"]).read_text(encoding="utf-8"))
    classification = metadata["candidate_classification"]
    assert classification["repaired_patch_applied_repairs"]
    assert classification["repaired_patch_validation"]["git_apply_check"]["status"] == "ok"
    assert Path(paths["repaired_candidate_patch_path"]).read_text(encoding="utf-8").startswith("diff --git ")


def test_recompute_hunk_counts_corrects_inaccurate_header_counts():
    # Header claims 7,7 but body has 5 context + 1 del + 1 add = 6 old / 6 new.
    diff = (
        "diff --git a/d.md b/d.md\n"
        "--- a/d.md\n"
        "+++ b/d.md\n"
        "@@ -4,7 +4,7 @@\n"
        " \n"
        " para line stays\n"
        " \n"
        "-old sentence.\n"
        "+old sentence and more.\n"
        " \n"
        " ## next\n"
    )
    repaired, changed = recompute_hunk_counts(diff)
    assert changed is True
    assert "@@ -4,6 +4,6 @@" in repaired


def test_recompute_hunk_counts_is_noop_for_accurate_header():
    diff = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    repaired, changed = recompute_hunk_counts(diff)
    assert changed is False
    assert repaired.strip() == diff.strip()


def test_repair_recomputes_counts_and_makes_patch_git_applyable(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    (repo / "docs").mkdir()
    target = repo / "docs" / "d.md"
    target.write_text("top\n\nparagraph one stays here.\n\nold sentence.\n\n## next\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)

    # Worker emits a structurally clean diff but with WRONG hunk counts (claims 7,7).
    candidate = (
        "diff --git a/docs/d.md b/docs/d.md\n"
        "--- a/docs/d.md\n"
        "+++ b/docs/d.md\n"
        "@@ -3,7 +3,7 @@\n"
        " paragraph one stays here.\n"
        " \n"
        "-old sentence.\n"
        "+old sentence and more.\n"
        " \n"
        " ## next\n"
    )
    # Raw candidate should fail git apply (count mismatch).
    assert git_apply_check(candidate, str(repo))["status"] == "failed"

    task = _task(tmp_path, context_repo_path=str(repo), allowed_files=["docs/d.md"])
    paths = write_openrouter_artifacts(task, "prompt", candidate, {"status": "completed"})
    metadata = json.loads(Path(paths["metadata_path"]).read_text(encoding="utf-8"))
    classification = metadata["candidate_classification"]
    assert "recomputed_hunk_counts" in classification["repaired_patch_applied_repairs"]
    assert classification["repaired_patch_validation"]["git_apply_check"]["status"] == "ok"
