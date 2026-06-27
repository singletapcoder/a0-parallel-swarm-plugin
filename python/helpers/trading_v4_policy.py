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


def build_trading_v4_worker_prompt(task) -> str:
    allowed = "\n".join(f"- {x}" for x in (getattr(task, "allowed_files", []) or []))
    if not allowed:
        allowed = "- No file mutations allowed unless explicitly requested in the task."
    forbidden = "\n".join(f"- {x}" for x in (getattr(task, "forbidden_actions", []) or TRADING_V4_FORBIDDEN_ACTIONS))
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

## Required output contract

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

## Task

{task.message}
"""
