"""Artifact helpers for Parallel Swarm task evidence."""

from __future__ import annotations

import fnmatch
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


def extract_fenced_diff(text: str) -> str:
    """Return the first fenced diff/patch block, or an empty string."""
    pattern = r"```(?:diff|patch)\n(.*?)```"
    match = re.search(pattern, text or "", flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip() + "\n"
    return ""


def extract_raw_diff(text: str) -> str:
    """Return raw unified diff text only when the response starts with a diff."""
    raw = (text or "").strip()
    if raw.startswith("diff --git ") or raw.startswith("--- "):
        return raw + "\n"
    return ""


def extract_diff_block(text: str, *, strict_diff: bool = False) -> str:
    """Return candidate diff text from a worker response.

    Normal mode preserves the original contract: first fenced diff/patch block.
    Strict mode accepts raw unified diff only. More nuanced classification is
    handled by classify_candidate_response().
    """
    if strict_diff:
        return extract_raw_diff(text)
    return extract_fenced_diff(text)


def classify_candidate_response(
    text: str,
    allowed_files: list[str] | None = None,
    allowed_file_globs: list[str] | None = None,
    forbidden_file_globs: list[str] | None = None,
) -> dict[str, Any]:
    """Classify a worker response without mutating any repository.

    This supports a scalable swarm workflow: workers may return raw diffs,
    fenced diffs, idea-only prose, safety blocks, or unusable output. The
    classifier preserves useful repairable candidates without weakening the
    apply/test gate.
    """
    raw = (text or "").strip()
    raw_diff = extract_raw_diff(raw)
    fenced_diff = extract_fenced_diff(raw)
    normalized_patch = raw_diff or fenced_diff

    if raw.startswith("BLOCKED_FOR_SAFETY_BOUNDARY"):
        classification = "blocked_for_safety"
        recommended_action = "record_and_skip"
    elif raw.startswith("NO_PATCH"):
        classification = "no_patch"
        recommended_action = "record_and_skip"
    elif raw_diff:
        classification = "usable_raw_diff"
        recommended_action = "validate_then_review"
    elif fenced_diff:
        classification = "repairable_fenced_diff"
        recommended_action = "validate_normalized_patch_then_review"
    elif raw:
        classification = "idea_only"
        recommended_action = "manual_review_or_repair"
    else:
        classification = "nonsense"
        recommended_action = "reject"

    validation = validate_candidate_patch(normalized_patch, allowed_files or [], allowed_file_globs or [], forbidden_file_globs or [])
    if normalized_patch and (validation.get("allowed_files_violated") or validation.get("forbidden_file_globs_violated")):
        classification = "unsafe_or_out_of_scope"
        recommended_action = "reject"

    return {
        "classification": classification,
        "recommended_action": recommended_action,
        "has_raw_diff": bool(raw_diff),
        "has_fenced_diff": bool(fenced_diff),
        "normalized_patch": normalized_patch,
        "normalized_patch_bytes": len(normalized_patch.encode("utf-8")),
        "patch_validation": validation,
    }


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


def _matches_any_glob(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def validate_candidate_patch(
    diff_text: str,
    allowed_files: list[str] | None = None,
    allowed_file_globs: list[str] | None = None,
    forbidden_file_globs: list[str] | None = None,
) -> dict[str, Any]:
    """Validate basic candidate patch shape and allowed/forbidden file scope.

    This intentionally avoids applying patches. Repo-specific git apply checks
    remain a gatekeeper responsibility unless a future plugin tool is given an
    explicit repo path and sandbox policy.
    """
    allowed = set(allowed_files or [])
    allowed_globs = list(allowed_file_globs or [])
    forbidden_globs = list(forbidden_file_globs or [])
    touched = _diff_touched_files(diff_text)
    has_allowed_scope = bool(allowed or allowed_globs)
    violations = [
        path
        for path in touched
        if has_allowed_scope and path not in allowed and not _matches_any_glob(path, allowed_globs)
    ]
    forbidden_violations = [path for path in touched if _matches_any_glob(path, forbidden_globs)]
    has_diff_header = "diff --git " in diff_text
    has_file_header = "--- " in diff_text and "+++ " in diff_text
    has_hunk = "@@" in diff_text
    non_empty = bool(diff_text.strip())
    status = (
        "valid_basic"
        if non_empty and (has_diff_header or has_file_header) and has_hunk and not violations and not forbidden_violations
        else "invalid"
    )
    reasons: list[str] = []
    if not non_empty:
        reasons.append("empty_patch")
    if non_empty and not (has_diff_header or has_file_header):
        reasons.append("missing_file_header")
    if non_empty and not has_hunk:
        reasons.append("missing_hunk_header")
    if violations:
        reasons.append("allowed_files_violated")
    if forbidden_violations:
        reasons.append("forbidden_file_globs_violated")
    return {
        "status": status,
        "non_empty": non_empty,
        "has_diff_git_header": has_diff_header,
        "has_file_header": has_file_header,
        "has_hunk_header": has_hunk,
        "touched_files": touched,
        "allowed_files": sorted(allowed),
        "allowed_file_globs": allowed_globs,
        "forbidden_file_globs": forbidden_globs,
        "allowed_files_violated": violations,
        "forbidden_file_globs_violated": forbidden_violations,
        "reasons": reasons,
    }


def repair_candidate_diff(text: str) -> dict[str, Any]:
    """Attempt a conservative, non-semantic repair of a malformed unified diff.

    This fixes only common structural malformations observed from LLM workers
    without altering hunk bodies or recomputing line counts:

    - strips leading prose/blank lines before the first diff header;
    - repairs a ``diff --git`` header that was prefixed with a stray ``--- ``
      or ``+++ `` marker (for example ``--- diff --git a/x b/x``);
    - normalizes CRLF/CR line endings to LF;
    - ensures a single trailing newline.

    It never applies the patch and never recomputes hunk ranges; semantic
    correctness still depends on git apply validation downstream.
    """
    raw = text or ""
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    repairs: list[str] = []
    if normalized != raw:
        repairs.append("normalized_line_endings")

    lines = normalized.split("\n")

    # Repair a diff --git header glued behind a stray file-marker prefix.
    fixed_lines: list[str] = []
    for line in lines:
        stripped = line
        for prefix in ("--- diff --git ", "+++ diff --git "):
            if stripped.startswith(prefix):
                stripped = "diff --git " + stripped[len(prefix):]
                repairs.append("stripped_stray_marker_before_diff_git_header")
                break
        fixed_lines.append(stripped)
    lines = fixed_lines

    # Drop leading lines before the first recognizable diff header.
    header_index = None
    for index, line in enumerate(lines):
        if line.startswith("diff --git ") or line.startswith("--- "):
            header_index = index
            break
    if header_index is None:
        return {
            "repaired": False,
            "repaired_text": "",
            "repairs_applied": repairs,
            "reason": "no_diff_header_found",
        }
    if header_index > 0:
        repairs.append("stripped_leading_non_diff_lines")
    repaired_body = "\n".join(lines[header_index:]).strip("\n")
    repaired_text = repaired_body + "\n" if repaired_body else ""

    changed = repaired_text.strip() != (raw or "").strip()
    return {
        "repaired": changed and bool(repaired_text.strip()),
        "repaired_text": repaired_text,
        "repairs_applied": sorted(set(repairs)),
        "reason": "" if repaired_text.strip() else "empty_after_repair",
    }


def write_openrouter_artifacts(task, prompt: str, raw_response: str, metadata: dict[str, Any]) -> dict[str, str]:
    out = safe_task_output_dir(task)
    prompt_path = out / "prompt.md"
    raw_path = out / "raw_response.md"
    patch_path = out / "candidate_patch.diff"
    normalized_patch_path = out / "normalized_candidate_patch.diff"
    repaired_patch_path = out / "repaired_candidate_patch.diff"
    meta_path = out / "metadata.json"

    prompt_path.write_text(prompt, encoding="utf-8")
    raw_path.write_text(raw_response, encoding="utf-8")
    candidate_patch = extract_diff_block(raw_response, strict_diff=bool(getattr(task, "strict_diff", False)))
    classification = classify_candidate_response(
        raw_response,
        getattr(task, "allowed_files", []) or [],
        getattr(task, "allowed_file_globs", []) or [],
        getattr(task, "forbidden_file_globs", []) or [],
    )
    normalized_patch = classification["normalized_patch"]
    patch_path.write_text(candidate_patch, encoding="utf-8")
    normalized_patch_path.write_text(normalized_patch, encoding="utf-8")

    repair = repair_candidate_diff(candidate_patch or normalized_patch or raw_response)
    repaired_patch = repair["repaired_text"] if repair["repaired"] else ""
    repaired_patch_path.write_text(repaired_patch, encoding="utf-8")

    metadata = dict(metadata)
    patch_validation = classification["patch_validation"]
    normalized_patch_validation = validate_candidate_patch(
        normalized_patch,
        getattr(task, "allowed_files", []) or [],
        getattr(task, "allowed_file_globs", []) or [],
        getattr(task, "forbidden_file_globs", []) or [],
    )
    repaired_patch_validation = validate_candidate_patch(
        repaired_patch,
        getattr(task, "allowed_files", []) or [],
        getattr(task, "allowed_file_globs", []) or [],
        getattr(task, "forbidden_file_globs", []) or [],
    )
    if bool(getattr(task, "validate_git_apply", False)):
        patch_validation["git_apply_check"] = git_apply_check(candidate_patch, getattr(task, "context_repo_path", "") or "")
        normalized_patch_validation["git_apply_check"] = git_apply_check(normalized_patch, getattr(task, "context_repo_path", "") or "")
        if repaired_patch.strip():
            repaired_patch_validation["git_apply_check"] = git_apply_check(repaired_patch, getattr(task, "context_repo_path", "") or "")
    classification = dict(classification)
    classification.pop("normalized_patch", None)
    classification["normalized_patch_path"] = str(normalized_patch_path)
    classification["normalized_patch_validation"] = normalized_patch_validation
    classification["repaired_patch_path"] = str(repaired_patch_path)
    classification["repaired_patch_applied_repairs"] = repair["repairs_applied"]
    classification["repaired_patch_validation"] = repaired_patch_validation
    try:
        from plugins.parallel_swarm.python.helpers.trading_v4_policy import build_context_manifest

        context_manifest = build_context_manifest(task)
        context_manifest = dict(context_manifest)
        context_manifest["files"] = [
            {key: value for key, value in item.items() if key != "content"}
            for item in context_manifest.get("files", [])
        ]
    except Exception as exc:  # pragma: no cover - defensive metadata path
        context_manifest = {"status": "context_manifest_unavailable", "error": f"{type(exc).__name__}: {exc}"}

    metadata.update({
        "prompt_path": str(prompt_path),
        "raw_response_path": str(raw_path),
        "candidate_patch_path": str(patch_path),
        "normalized_candidate_patch_path": str(normalized_patch_path),
        "repaired_candidate_patch_path": str(repaired_patch_path),
        "output_dir": str(out),
        "strict_diff": bool(getattr(task, "strict_diff", False)),
        "include_allowed_file_context": bool(getattr(task, "include_allowed_file_context", False)),
        "context_repo_path": getattr(task, "context_repo_path", "") or "",
        "allowed_file_globs": getattr(task, "allowed_file_globs", []) or [],
        "forbidden_file_globs": getattr(task, "forbidden_file_globs", []) or [],
        "read_only_context_files": getattr(task, "read_only_context_files", []) or [],
        "read_only_context_globs": getattr(task, "read_only_context_globs", []) or [],
        "context_manifest": context_manifest,
        "candidate_classification": classification,
        "patch_validation": patch_validation,
    })
    with meta_path.open("w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, sort_keys=True)

    return {
        "output_dir": str(out),
        "prompt_path": str(prompt_path),
        "raw_response_path": str(raw_path),
        "candidate_patch_path": str(patch_path),
        "normalized_candidate_patch_path": str(normalized_patch_path),
        "repaired_candidate_patch_path": str(repaired_patch_path),
        "metadata_path": str(meta_path),
    }
