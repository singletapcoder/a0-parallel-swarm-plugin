"""Artifact helpers for Parallel Swarm task evidence."""

from __future__ import annotations

import json
import re
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


def extract_diff_block(text: str) -> str:
    match = re.search(r"```(?:diff|patch)\n(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip() + "\n"
    return ""


def write_openrouter_artifacts(task, prompt: str, raw_response: str, metadata: dict[str, Any]) -> dict[str, str]:
    out = safe_task_output_dir(task)
    prompt_path = out / "prompt.md"
    raw_path = out / "raw_response.md"
    patch_path = out / "candidate_patch.diff"
    meta_path = out / "metadata.json"

    prompt_path.write_text(prompt, encoding="utf-8")
    raw_path.write_text(raw_response, encoding="utf-8")
    patch_path.write_text(extract_diff_block(raw_response), encoding="utf-8")

    metadata = dict(metadata)
    metadata.update({
        "prompt_path": str(prompt_path),
        "raw_response_path": str(raw_path),
        "candidate_patch_path": str(patch_path),
        "output_dir": str(out),
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
