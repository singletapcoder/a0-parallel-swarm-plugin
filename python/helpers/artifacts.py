"""Artifact helpers for Parallel Swarm task evidence."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_task_output_dir(task) -> Path:
    if not getattr(task, "output_dir", ""):
        base = Path("/a0/usr/workdir/parallel_swarm_outputs") / utc_stamp() / str(task.id)
    else:
        base = Path(task.output_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base


def extract_diff_block(text: str, *, strict_diff: bool = False) -> str:
    """Return candidate diff text from a worker response.

    Normal mode preserves the original contract: first fenced diff/patch block.
    Strict mode accepts raw unified diff only, because workers are instructed not
    to wrap output in markdown. Safety/NO_PATCH sentinel responses produce an
    empty candidate patch.
    """
    raw = (text or "").strip()
    if strict_diff:
        if raw.startswith(("NO_PATCH", "BLOCKED_FOR_SAFETY_BOUNDARY")):
            return ""
        if raw.startswith("diff --git ") or raw.startswith("--- "):
            return raw + "\n"
        return ""
    pattern = r"```(?:diff|patch)\n(.*?)```"
    match = re.search(pattern, text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip() + "\n"
    return ""


def _diff_touched_files(diff_text: str) -> list[str]:
    files: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3]
                if path.startswith("b/"):
                    path = path[2:]
                if path != "/dev/null" and path not in files:
                    files.append(path)
        elif line.startswith("+++ "):
            path = line[len("+++ "):]
            if path.startswith("b/"):
                path = path[2:]
            if path != "/dev/null" and path not in files:
                files.append(path)
    return files


def git_apply_check(diff_text: str, repo_path: str) -> dict[str, Any]:
    """Run non-mutating git apply --check for candidate diff evidence."""
    if not diff_text.strip():
        return {"status": "skipped", "reason": "empty_patch"}
    if not repo_path:
        return {"status": "skipped", "reason": "repo_path_not_configured"}
    repo = Path(repo_path)
    if not repo.exists():
        return {"status": "skipped", "reason": "repo_path_missing", "repo_path": str(repo)}
    proc = subprocess.run(
        ["git", "apply", "--check", "-"],
        input=diff_text,
        text=True,
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return {
        "status": "ok" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "repo_path": str(repo),
    }


def validate_candidate_patch(diff_text: str, allowed_files: list[str] | None = None) -> dict[str, Any]:
    """Validate basic candidate patch shape and allowed-file scope.

    This intentionally avoids applying patches. Repo-specific git apply checks
    remain a gatekeeper responsibility unless a future plugin tool is given an
    explicit repo path and sandbox policy.
    """
    allowed = set(allowed_files or [])
    touched = _diff_touched_files(diff_text)
    violations = [path for path in touched if allowed and path not in allowed]
    has_diff_header = "diff --git " in diff_text
    has_file_header = "--- " in diff_text and "+++ " in diff_text
    has_hunk = "@@" in diff_text
    non_empty = bool(diff_text.strip())
    status = "valid_basic" if non_empty and (has_diff_header or has_file_header) and has_hunk and not violations else "invalid"
    reasons: list[str] = []
    if not non_empty:
        reasons.append("empty_patch")
    if non_empty and not (has_diff_header or has_file_header):
        reasons.append("missing_file_header")
    if non_empty and not has_hunk:
        reasons.append("missing_hunk_header")
    if violations:
        reasons.append("allowed_files_violated")
    return {
        "status": status,
        "non_empty": non_empty,
        "has_diff_git_header": has_diff_header,
        "has_file_header": has_file_header,
        "has_hunk_header": has_hunk,
        "touched_files": touched,
        "allowed_files": sorted(allowed),
        "allowed_files_violated": violations,
        "reasons": reasons,
    }


def write_openrouter_artifacts(task, prompt: str, raw_response: str, metadata: dict[str, Any]) -> dict[str, str]:
    out = safe_task_output_dir(task)
    prompt_path = out / "prompt.md"
    raw_path = out / "raw_response.md"
    patch_path = out / "candidate_patch.diff"
    meta_path = out / "metadata.json"

    prompt_path.write_text(prompt, encoding="utf-8")
    raw_path.write_text(raw_response, encoding="utf-8")
    candidate_patch = extract_diff_block(raw_response, strict_diff=bool(getattr(task, "strict_diff", False)))
    patch_path.write_text(candidate_patch, encoding="utf-8")

    metadata = dict(metadata)
    patch_validation = validate_candidate_patch(candidate_patch, getattr(task, "allowed_files", []) or [])
    if bool(getattr(task, "validate_git_apply", False)):
        patch_validation["git_apply_check"] = git_apply_check(candidate_patch, getattr(task, "context_repo_path", "") or "")
    metadata.update({
        "prompt_path": str(prompt_path),
        "raw_response_path": str(raw_path),
        "candidate_patch_path": str(patch_path),
        "output_dir": str(out),
        "strict_diff": bool(getattr(task, "strict_diff", False)),
        "include_allowed_file_context": bool(getattr(task, "include_allowed_file_context", False)),
        "context_repo_path": getattr(task, "context_repo_path", "") or "",
        "patch_validation": patch_validation,
    })
    with meta_path.open("w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, sort_keys=True)

    return {
        "output_dir": str(out),
        "prompt_path": str(prompt_path),
        "raw_response_path": str(raw_path),
        "candidate_patch_path": str(patch_path),
        "metadata_path": str(meta_path),
    }
