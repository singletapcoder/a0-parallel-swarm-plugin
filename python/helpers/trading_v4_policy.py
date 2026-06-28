"""Trading V4 safety wrapper for OpenRouter swarm workers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

TRADING_V4_FORBIDDEN_ACTIONS = [
    "broker_calls",
    "credential_resolution",
    "live_trading",
    "runtime_activation",
    "halt_clearing",
    "deployment",
    "packaging",
    "release_claims",
]

_CONTEXT_ERROR_STATUSES = {
    "BLOCKED_CONTEXT_PATH_OUTSIDE_REPO",
    "MISSING_CONTEXT_FILE",
    "CONTEXT_PATH_NOT_FILE",
    "CONTEXT_FILE_NOT_UTF8",
}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return []


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        normalized = str(item).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _safe_context_file(repo_root: str, rel_path: str) -> tuple[str, str | None]:
    """Return file content for a repo-relative path without escaping repo root."""
    root = Path(repo_root).resolve()
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return rel_path, "BLOCKED_CONTEXT_PATH_OUTSIDE_REPO"
    if not target.exists():
        return rel_path, "MISSING_CONTEXT_FILE"
    if not target.is_file():
        return rel_path, "CONTEXT_PATH_NOT_FILE"
    try:
        return rel_path, target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return rel_path, "CONTEXT_FILE_NOT_UTF8"


_SKIPPED_CONTEXT_DIR_PARTS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}

_SKIPPED_CONTEXT_SUFFIXES = {
    ".7z",
    ".db",
    ".dmg",
    ".gz",
    ".jpg",
    ".jpeg",
    ".pdf",
    ".png",
    ".pyc",
    ".sqlite",
    ".tar",
    ".zip",
}


def _is_context_candidate(path: Path) -> bool:
    """Return True for source-like files suitable for prompt context."""
    if any(part in _SKIPPED_CONTEXT_DIR_PARTS for part in path.parts):
        return False
    if path.suffix.lower() in _SKIPPED_CONTEXT_SUFFIXES:
        return False
    return True


def _expand_repo_globs(repo_root: str, patterns: list[str], *, max_matches: int = 50) -> list[str]:
    """Expand repo-relative glob patterns to concrete source-like files."""
    if not repo_root or not patterns:
        return []
    root = Path(repo_root).resolve()
    matches: list[str] = []
    for pattern in patterns:
        if not pattern or pattern.startswith("/") or ".." in Path(pattern).parts:
            continue
        for target in sorted(root.glob(pattern)):
            try:
                resolved = target.resolve()
                resolved.relative_to(root)
            except ValueError:
                continue
            if resolved.is_file() and _is_context_candidate(resolved.relative_to(root)):
                matches.append(resolved.relative_to(root).as_posix())
            if len(matches) >= max_matches:
                return _dedupe(matches)
    return _dedupe(matches)


def _bounded_content(content: str, *, per_file_limit: int, remaining_limit: int) -> tuple[str, int, bool]:
    encoded = content.encode("utf-8")
    limit = max(0, min(per_file_limit, remaining_limit))
    if len(encoded) <= limit:
        return content, len(encoded), False
    truncated = encoded[:limit].decode("utf-8", errors="ignore")
    note = f"\n[TRUNCATED_CONTEXT_FILE: original_bytes={len(encoded)} included_bytes={limit}]\n"
    return truncated + note, limit, True


def build_context_manifest(task) -> dict[str, Any]:
    """Build structured, bounded context diagnostics for an OpenRouter worker."""
    repo_root = getattr(task, "context_repo_path", "") or ""
    include = bool(getattr(task, "include_allowed_file_context", False))
    allowed_files = _dedupe(_as_list(getattr(task, "allowed_files", [])))
    allowed_file_globs = _dedupe(_as_list(getattr(task, "allowed_file_globs", [])))
    read_only_context_files = _dedupe(_as_list(getattr(task, "read_only_context_files", [])))
    read_only_context_globs = _dedupe(_as_list(getattr(task, "read_only_context_globs", [])))
    per_file_limit = max(1, int(getattr(task, "context_file_max_bytes", 20000) or 20000))
    total_limit = max(1, int(getattr(task, "context_total_max_bytes", 100000) or 100000))

    manifest: dict[str, Any] = {
        "include_requested": include,
        "repo_root": repo_root,
        "allowed_files": allowed_files,
        "allowed_file_globs": allowed_file_globs,
        "read_only_context_files": read_only_context_files,
        "read_only_context_globs": read_only_context_globs,
        "expanded_allowed_file_globs": [],
        "expanded_read_only_context_globs": [],
        "per_file_limit_bytes": per_file_limit,
        "total_limit_bytes": total_limit,
        "included_bytes": 0,
        "files": [],
        "warnings": [],
    }

    if not include:
        manifest["warnings"].append("context_disabled")
        return manifest
    if not repo_root:
        manifest["warnings"].append("context_requested_without_repo_path")
        return manifest

    expanded_allowed = _expand_repo_globs(repo_root, allowed_file_globs)
    expanded_read_only = _expand_repo_globs(repo_root, read_only_context_globs)
    manifest["expanded_allowed_file_globs"] = expanded_allowed
    manifest["expanded_read_only_context_globs"] = expanded_read_only

    mutable_context_files = _dedupe(allowed_files + expanded_allowed)
    read_only_files = _dedupe(read_only_context_files + expanded_read_only)
    if not mutable_context_files and not read_only_files:
        manifest["warnings"].append("context_requested_without_files")
        return manifest

    total = 0
    context_entries = [("allowed_mutation_file", path) for path in mutable_context_files]
    context_entries += [("read_only_context_file", path) for path in read_only_files]
    for role, rel_path in context_entries:
        if total >= total_limit:
            manifest["warnings"].append("context_total_limit_reached")
            break
        path, content = _safe_context_file(repo_root, rel_path)
        item: dict[str, Any] = {"path": path, "role": role, "status": "included", "bytes": 0, "truncated": False, "content": ""}
        if content is None:
            item["content"] = "<empty file>"
        elif content in _CONTEXT_ERROR_STATUSES:
            item["status"] = content
            item["content"] = f"[{content}]"
            manifest["warnings"].append(content)
        else:
            bounded, used, truncated = _bounded_content(content, per_file_limit=per_file_limit, remaining_limit=total_limit - total)
            item.update({"bytes": used, "truncated": truncated, "content": bounded})
            total += used
            if truncated:
                manifest["warnings"].append("context_file_truncated")
        manifest["files"].append(item)
    manifest["included_bytes"] = total
    return manifest


def build_allowed_file_context(task) -> str:
    """Build a bounded context bundle from allowed/read-only files when enabled."""
    manifest = build_context_manifest(task)
    if not manifest["include_requested"]:
        return "Context mode: disabled."
    if "context_requested_without_repo_path" in manifest["warnings"]:
        return "Context mode: requested but no context_repo_path was provided."
    if "context_requested_without_files" in manifest["warnings"]:
        return "Context mode: requested but no allowed/read-only context files were provided."

    chunks = [
        f"Context repo path: {manifest['repo_root']}",
        "Mutation authority: ONLY files listed under Allowed files may be changed.",
        "Read-only context files are evidence for understanding; do NOT modify them unless they are also explicitly listed as Allowed files.",
        f"Context limits: per_file={manifest['per_file_limit_bytes']} bytes total={manifest['total_limit_bytes']} bytes included={manifest['included_bytes']} bytes.",
    ]
    if manifest["warnings"]:
        chunks.append("Context warnings: " + ", ".join(sorted(set(manifest["warnings"]))))
    for item in manifest["files"]:
        label = "ALLOWED MUTATION FILE" if item["role"] == "allowed_mutation_file" else "READ-ONLY CONTEXT FILE"
        chunks.append(f"\n--- BEGIN {label}: {item['path']} status={item['status']} bytes={item['bytes']} truncated={item['truncated']} ---")
        chunks.append(item["content"])
        chunks.append(f"--- END {label}: {item['path']} ---")
    return "\n".join(chunks)


def build_trading_v4_worker_prompt(task) -> str:
    allowed_files = _dedupe(_as_list(getattr(task, "allowed_files", [])))
    allowed_globs = _dedupe(_as_list(getattr(task, "allowed_file_globs", [])))
    allowed_lines = [f"- {x}" for x in allowed_files]
    allowed_lines += [f"- glob: {x}" for x in allowed_globs]
    allowed = "\n".join(allowed_lines) if allowed_lines else "- No file mutations allowed unless explicitly requested in the task."
    read_only_files = _dedupe(_as_list(getattr(task, "read_only_context_files", [])))
    read_only_globs = _dedupe(_as_list(getattr(task, "read_only_context_globs", [])))
    read_only_lines = [f"- {x}" for x in read_only_files] + [f"- glob: {x}" for x in read_only_globs]
    read_only = "\n".join(read_only_lines) if read_only_lines else "- None"
    forbidden = "\n".join(f"- {x}" for x in (getattr(task, "forbidden_actions", []) or TRADING_V4_FORBIDDEN_ACTIONS))
    strict_diff = bool(getattr(task, "strict_diff", False))
    context_bundle = build_allowed_file_context(task)

    if strict_diff:
        output_contract = """## Required output contract

Return exactly one of the following, and nothing else:

1. A valid unified diff that applies to the provided current file contents.
2. `NO_PATCH: <short reason>` if no safe patch should be proposed.
3. `BLOCKED_FOR_SAFETY_BOUNDARY: <short reason>` if the task requires a forbidden action.

Strict rules:

- Do not wrap the diff in markdown fences.
- Do not include prose before or after the diff.
- Do not invent files, classes, functions, import paths, or placeholder index hashes.
- Only touch files listed in Allowed files.
- Treat Read-only context files as non-mutable evidence, not patch targets.
- A partial/truncated diff is worse than `NO_PATCH`.

### Exact diff format

The first line MUST start with `diff --git ` (never `--- diff --git`). Do not glue a `--- ` or `+++ ` marker in front of the `diff --git` line. Use repo-relative paths.

The diff MUST follow this exact structure:

diff --git a/<path> b/<path>
--- a/<path>
+++ b/<path>
@@ -<old_start>,<old_count> +<new_start>,<new_count> @@
<unchanged context line prefixed with a single space>
-<removed line>
+<added line>

Hunk header counts MUST be accurate: `<old_count>` is the number of context+removed lines in the hunk, and `<new_count>` is the number of context+added lines. If you cannot produce an exactly applyable diff with correct counts and matching context, return `NO_PATCH` instead of an approximate diff.

Every context line must begin with a single space, every removed line with `-`, and every added line with `+`. Do not emit blank lines without a leading space inside a hunk.
"""
    else:
        output_contract = """## Required output contract

Return concise markdown with these sections:

# Worker Result

## Summary

## Files Intended To Change

## Candidate Patch
```diff

```

## Suggested Tests

## Risk Notes

## Stop/Blockers
"""

    return f"""# Trading V4 OpenRouter Worker Task

You are an OpenRouter-backed worker producing a candidate patch/report for Trading V4.

## Non-negotiable boundaries

Trading V4 is standalone and outside Agent Zero. Agent Zero/Jarvis is an external operator/client, not the Trading V4 runtime.

Do not use old `/a0/usr/projects/Trading` runtime paths as authority. Historical/guardrail references only.

Forbidden actions:
{forbidden}

If the task requires a forbidden action, output exactly `BLOCKED_FOR_SAFETY_BOUNDARY` with a short reason.

## Scope

Lane: {getattr(task, "lane", "") or "unspecified"}
Role: {getattr(task, "role", "") or "unspecified"}
Allowed files:
{allowed}

Read-only context files:
{read_only}

## Current allowed-file and read-only context

{context_bundle}

{output_contract}

## Task

{task.message}
"""
