"""Trading V4 safety wrapper for OpenRouter swarm workers."""

from __future__ import annotations

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


def _safe_context_file(repo_root: str, rel_path: str) -> tuple[str, str | None]:
    """Return file content for a repo-relative path without escaping repo root."""
    from pathlib import Path

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


def build_allowed_file_context(task) -> str:
    """Build a bounded context bundle from allowed files when explicitly enabled."""
    repo_root = getattr(task, "context_repo_path", "") or ""
    include = bool(getattr(task, "include_allowed_file_context", False))
    allowed_files = getattr(task, "allowed_files", []) or []
    if not include:
        return "Context mode: disabled."
    if not repo_root:
        return "Context mode: requested but no context_repo_path was provided."
    if not allowed_files:
        return "Context mode: requested but allowed_files is empty."

    chunks = [f"Context repo path: {repo_root}", "Allowed file contents follow."]
    for rel_path in allowed_files:
        path, content = _safe_context_file(repo_root, str(rel_path))
        chunks.append(f"\n--- BEGIN FILE: {path} ---")
        if content is None:
            chunks.append("<empty file>")
        elif content in {
            "BLOCKED_CONTEXT_PATH_OUTSIDE_REPO",
            "MISSING_CONTEXT_FILE",
            "CONTEXT_PATH_NOT_FILE",
            "CONTEXT_FILE_NOT_UTF8",
        }:
            chunks.append(f"[{content}]")
        else:
            chunks.append(content)
        chunks.append(f"--- END FILE: {path} ---")
    return "\n".join(chunks)


def build_trading_v4_worker_prompt(task) -> str:
    allowed = "\n".join(f"- {x}" for x in (getattr(task, "allowed_files", []) or []))
    if not allowed:
        allowed = "- No file mutations allowed unless explicitly requested in the task."
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
- Use repo-relative paths in `diff --git a/... b/...` headers.
- A partial/truncated diff is worse than `NO_PATCH`.
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

## Current allowed-file context

{context_bundle}

{output_contract}

## Task

{task.message}
"""
