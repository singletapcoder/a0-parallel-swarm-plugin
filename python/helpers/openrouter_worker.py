"""Direct OpenRouter worker path for Parallel Swarm.

This module deliberately fails closed. It does not fall back to Agent Zero
subordinate monologue when OpenRouter is unavailable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from urllib import error, request

from plugins.parallel_swarm.python.helpers.artifacts import write_openrouter_artifacts
from plugins.parallel_swarm.python.helpers.trading_v4_policy import build_trading_v4_worker_prompt

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterUnavailable(RuntimeError):
    """Raised when an OpenRouter-backed task cannot run safely."""


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise OpenRouterUnavailable("BLOCKED_OPENROUTER_UNAVAILABLE: OPENROUTER_API_KEY is not configured")
    return key


def _extract_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return str(msg.get("content") or "")


def _extract_usage(payload: dict[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def _timeout_seconds() -> float:
    raw = os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "120")
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 120.0


def _call_openrouter_sync(model: str, prompt: str, *, api_key: str) -> tuple[str, dict[str, Any]]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a precise coding worker. Produce candidate patches only; do not claim execution."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }
    req = request.Request(
        OPENROUTER_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://agent-zero.local/parallel_swarm",
            "X-Title": "Agent Zero Parallel Swarm",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=_timeout_seconds()) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise OpenRouterUnavailable(f"BLOCKED_OPENROUTER_UNAVAILABLE: HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise OpenRouterUnavailable(f"BLOCKED_OPENROUTER_UNAVAILABLE: {exc}") from exc
    return _extract_content(payload), _extract_usage(payload)


async def run_openrouter_task(task) -> tuple[str, dict[str, Any]]:
    if not getattr(task, "model", ""):
        raise OpenRouterUnavailable("BLOCKED_OPENROUTER_UNAVAILABLE: OpenRouter task missing exact model id")
    if getattr(task, "fallback_policy", "stop_not_direct_code") != "stop_not_direct_code":
        raise OpenRouterUnavailable("BLOCKED_OPENROUTER_UNAVAILABLE: unsupported fallback policy")

    prompt = build_trading_v4_worker_prompt(task)
    metadata = {
        "task_id": task.id,
        "backend": "openrouter",
        "model": task.model,
        "role": getattr(task, "role", ""),
        "lane": getattr(task, "lane", ""),
        "fallback_policy": getattr(task, "fallback_policy", "stop_not_direct_code"),
        "fallback_used": False,
        "created_at_utc": _utc_stamp(),
        "status": "started",
    }
    try:
        content, usage = _call_openrouter_sync(task.model, prompt, api_key=_api_key())
        metadata.update({"status": "completed", "token_usage": usage})
    except OpenRouterUnavailable as exc:
        content = str(exc)
        usage = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": 0}
        metadata.update({"status": "blocked", "error": str(exc), "token_usage": usage})
        write_openrouter_artifacts(task, prompt, content, metadata)
        raise

    paths = write_openrouter_artifacts(task, prompt, content, metadata)
    return content + "\n\n---\nOpenRouter artifact metadata: " + paths["metadata_path"], usage
