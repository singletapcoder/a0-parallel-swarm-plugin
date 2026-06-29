"""Lane-lead aggregation helpers for OpenRouter worker artifacts.

The lane lead consumes existing worker artifact directories and produces
compact JSON/Markdown evidence for higher-level review. It is intentionally
report-only: it never applies candidate patches or mutates Trading V4.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from plugins.parallel_swarm.python.helpers.artifacts import git_apply_check

CLASSIFICATION_ORDER = [
    "usable_raw_diff",
    "repairable_fenced_diff",
    "idea_only",
    "no_patch",
    "blocked_for_safety",
    "unsafe_or_out_of_scope",
    "nonsense",
]

REJECT_CLASSIFICATIONS = {"blocked_for_safety", "unsafe_or_out_of_scope", "nonsense"}


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"_json_error": "decode_failed", "path": str(path)}
    return data if isinstance(data, dict) else {"_json_error": "not_object", "path": str(path)}


def _classification(metadata: dict[str, Any]) -> str:
    candidate = metadata.get("candidate_classification")
    if isinstance(candidate, dict):
        value = str(candidate.get("classification") or "")
        if value:
            return value
    return str(metadata.get("classification") or "nonsense")


def _task_id(candidate_dir: Path, metadata: dict[str, Any]) -> str:
    return str(metadata.get("task_id") or metadata.get("id") or candidate_dir.name)


def _title(metadata: dict[str, Any], raw_response: str, task_id: str) -> str:
    for key in ("title", "description", "role", "lane"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value[:120]
    first_line = next((line.strip() for line in raw_response.splitlines() if line.strip()), "")
    return (first_line or task_id)[:120]


def _classification_rank(classification: str) -> int:
    try:
        return CLASSIFICATION_ORDER.index(classification)
    except ValueError:
        return len(CLASSIFICATION_ORDER)


def _allowed_file_violations(metadata: dict[str, Any]) -> list[str]:
    candidate = metadata.get("candidate_classification") if isinstance(metadata.get("candidate_classification"), dict) else {}
    validations = [
        candidate.get("patch_validation") if isinstance(candidate, dict) else None,
        candidate.get("normalized_patch_validation") if isinstance(candidate, dict) else None,
        metadata.get("patch_validation"),
    ]
    violations: list[str] = []
    for validation in validations:
        if not isinstance(validation, dict):
            continue
        for item in validation.get("allowed_files_violated") or []:
            if item not in violations:
                violations.append(str(item))
    return violations


def _touched_files(metadata: dict[str, Any]) -> list[str]:
    candidate = metadata.get("candidate_classification") if isinstance(metadata.get("candidate_classification"), dict) else {}
    validations = [
        candidate.get("patch_validation") if isinstance(candidate, dict) else None,
        candidate.get("normalized_patch_validation") if isinstance(candidate, dict) else None,
        metadata.get("patch_validation"),
    ]
    touched: list[str] = []
    for validation in validations:
        if not isinstance(validation, dict):
            continue
        for item in validation.get("touched_files") or []:
            if item not in touched:
                touched.append(str(item))
    return touched


def _normalized_patch_path(candidate_dir: Path, metadata: dict[str, Any]) -> str:
    for key in ("normalized_candidate_patch_path", "normalized_patch_path"):
        value = str(metadata.get(key) or "")
        if value:
            return value
    candidate = metadata.get("candidate_classification")
    if isinstance(candidate, dict):
        value = str(candidate.get("normalized_patch_path") or "")
        if value:
            return value
    default = candidate_dir / "normalized_candidate_patch.diff"
    return str(default) if default.exists() else ""


def _git_apply_status_from(validation: Any) -> str:
    """Return the recorded git_apply_check status from one validation block."""
    if not isinstance(validation, dict):
        return ""
    check = validation.get("git_apply_check")
    if not isinstance(check, dict):
        return ""
    return str(check.get("status") or "")


def _apply_outcome(metadata: dict[str, Any]) -> dict[str, str]:
    """Derive an apply-awareness outcome from recorded git_apply_check statuses.

    Behavior-preserving when no git_apply_check is recorded (returns 'unknown'),
    so structurally-classified candidates without apply evidence are unchanged.
    Only an explicitly recorded apply failure downgrades a usable diff.
    """
    candidate = metadata.get("candidate_classification") if isinstance(metadata.get("candidate_classification"), dict) else {}
    candidate_status = (
        _git_apply_status_from(candidate.get("patch_validation"))
        or _git_apply_status_from(candidate.get("normalized_patch_validation"))
        or _git_apply_status_from(metadata.get("patch_validation"))
        or _git_apply_status_from(metadata.get("normalized_patch_validation"))
    )
    repaired_status = (
        _git_apply_status_from(candidate.get("repaired_patch_validation"))
        or _git_apply_status_from(metadata.get("repaired_patch_validation"))
    )
    if candidate_status == "ok":
        outcome = "applies_clean"
    elif repaired_status == "ok":
        outcome = "applies_after_repair"
    elif candidate_status in {"failed", "error"} or repaired_status in {"failed", "error"}:
        outcome = "apply_failed"
    else:
        outcome = "unknown"
    return {
        "apply_outcome": outcome,
        "candidate_git_apply_status": candidate_status or "unknown",
        "repaired_git_apply_status": repaired_status or "unknown",
    }


def _recommendation(classification: str, allowed_file_violations: list[str], apply_outcome: str = "unknown") -> str:
    if classification in REJECT_CLASSIFICATIONS or allowed_file_violations:
        return "reject_or_skip"
    if classification == "usable_raw_diff":
        if apply_outcome == "apply_failed":
            return "apply_failed_needs_fix_or_repair"
        if apply_outcome == "applies_after_repair":
            return "validate_repaired_then_jarvis_review"
        return "validate_then_jarvis_review"
    if classification == "repairable_fenced_diff":
        return "preserve_for_validation_or_repair"
    if classification == "idea_only":
        return "manual_triage_for_possible_prompt_or_patch"
    if classification == "no_patch":
        return "record_and_skip"
    return "reject"


def _usefulness_score(classification: str, allowed_file_violations: list[str], touched_files: list[str], raw_response: str, apply_outcome: str = "unknown") -> int:
    if classification in REJECT_CLASSIFICATIONS or allowed_file_violations:
        return 0
    if classification == "usable_raw_diff":
        if apply_outcome == "apply_failed":
            # Structurally well-formed but does not apply: rank below genuine
            # usable/repairable so it cannot masquerade as cleanly usable.
            return 25
        if apply_outcome == "applies_after_repair":
            return 90 + min(10, len(touched_files))
        return 100 + min(10, len(touched_files))
    if classification == "repairable_fenced_diff":
        return 80 + min(10, len(touched_files))
    if classification == "idea_only":
        return 40 + min(20, len(raw_response) // 400)
    if classification == "no_patch":
        return 10
    return 0


def _title_key(title: str, task_id: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", f"{title} {task_id}".lower()).strip()
    return " ".join(normalized.split()[:8])


def collect_candidate(candidate_dir: str | Path) -> dict[str, Any]:
    """Read a single worker artifact directory into a deterministic summary."""
    path = Path(candidate_dir)
    metadata_path = path / "metadata.json"
    raw_response_path = path / "raw_response.md"
    metadata = _read_json(metadata_path)
    raw_response = _read_text(raw_response_path)
    classification = _classification(metadata)
    task_id = _task_id(path, metadata)
    title = _title(metadata, raw_response, task_id)
    touched_files = _touched_files(metadata)
    violations = _allowed_file_violations(metadata)
    normalized_path = _normalized_patch_path(path, metadata)
    apply_info = _apply_outcome(metadata)
    apply_outcome = apply_info["apply_outcome"]
    return {
        "task_id": task_id,
        "title": title,
        "artifact_dir": str(path),
        "metadata_path": str(metadata_path) if metadata_path.exists() else "",
        "raw_response_path": str(raw_response_path) if raw_response_path.exists() else "",
        "normalized_patch_path": normalized_path,
        "classification": classification,
        "classification_rank": _classification_rank(classification),
        "git_apply_outcome": apply_outcome,
        "candidate_git_apply_status": apply_info["candidate_git_apply_status"],
        "repaired_git_apply_status": apply_info["repaired_git_apply_status"],
        "recommended_action": _recommendation(classification, violations, apply_outcome),
        "usefulness_score": _usefulness_score(classification, violations, touched_files, raw_response, apply_outcome),
        "touched_files": touched_files,
        "allowed_file_violations": violations,
        "raw_response_bytes": len(raw_response.encode("utf-8")),
        "metadata_status": str(metadata.get("status") or ""),
        "metadata_json_error": metadata.get("_json_error", ""),
        "overlap_keys": {
            "touched_files": sorted(touched_files),
            "title_task_key": _title_key(title, task_id),
        },
    }


def find_candidate_dirs(paths: list[str | Path]) -> list[Path]:
    """Resolve direct candidate dirs and one-level task containers deterministically."""
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if (path / "metadata.json").exists() or (path / "raw_response.md").exists():
            found.append(path)
            continue
        if not path.exists():
            continue
        for child in sorted(p for p in path.iterdir() if p.is_dir()):
            if (child / "metadata.json").exists() or (child / "raw_response.md").exists():
                found.append(child)
    unique: list[Path] = []
    seen: set[str] = set()
    for item in found:
        key = str(item.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _overlap_groups(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[str]] = {}
    for candidate in candidates:
        files = candidate.get("touched_files") or []
        if files:
            key = "files:" + "|".join(sorted(files))
        else:
            key = "title:" + str(candidate.get("overlap_keys", {}).get("title_task_key") or candidate.get("task_id"))
        groups.setdefault(key, []).append(str(candidate.get("task_id")))
    return [
        {"overlap_key": key, "task_ids": sorted(task_ids), "count": len(task_ids)}
        for key, task_ids in sorted(groups.items())
        if len(task_ids) > 1
    ]


def build_lane_lead_report(candidate_paths: list[str | Path], *, output_dir: str | Path) -> dict[str, Any]:
    """Aggregate worker artifact directories and write JSON/Markdown reports."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    candidate_dirs = find_candidate_dirs(candidate_paths)
    candidates = [collect_candidate(path) for path in candidate_dirs]
    candidates.sort(key=lambda item: (-int(item["usefulness_score"]), int(item["classification_rank"]), str(item["task_id"])))

    grouped: dict[str, list[str]] = {name: [] for name in CLASSIFICATION_ORDER}
    grouped["unknown"] = []
    for candidate in candidates:
        grouped.setdefault(candidate["classification"], []).append(candidate["task_id"])

    report = {
        "schema": "parallel_swarm.openrouter_lane_lead_report.v1",
        "candidate_count": len(candidates),
        "candidate_task_ids": [item["task_id"] for item in candidates],
        "groups_by_classification": grouped,
        "ranked_candidates": candidates,
        "overlap_groups": _overlap_groups(candidates),
        "trading_v4_mutation_performed": False,
        "notes": [
            "Report-only aggregation; no candidate patch was applied.",
            "Rank order favors usable raw diffs, repairable fenced diffs, and high-value idea-only notes while rejecting unsafe/nonsense output.",
        ],
    }
    json_path = out / "lane_lead_report.json"
    md_path = out / "lane_lead_report.md"
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    md_path.write_text(render_lane_lead_markdown(report), encoding="utf-8")
    report["json_report_path"] = str(json_path)
    report["markdown_report_path"] = str(md_path)
    return report


def render_lane_lead_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# OpenRouter Lane-Lead Aggregation Report",
        "",
        f"- Candidate count: {report.get('candidate_count', 0)}",
        f"- Trading V4 mutation performed: {report.get('trading_v4_mutation_performed')}",
        "",
        "## Groups by classification",
        "",
    ]
    groups = report.get("groups_by_classification") or {}
    for classification in CLASSIFICATION_ORDER:
        ids = groups.get(classification) or []
        lines.append(f"- **{classification}**: {len(ids)}" + (f" — {', '.join(ids)}" if ids else ""))
    lines.extend(["", "## Ranked candidates", "", "| Rank | Task ID | Classification | Score | Recommendation | Touched files |", "|---:|---|---|---:|---|---|"])
    for idx, candidate in enumerate(report.get("ranked_candidates") or [], start=1):
        files = ", ".join(candidate.get("touched_files") or []) or "—"
        lines.append(
            f"| {idx} | {candidate.get('task_id')} | {candidate.get('classification')} | "
            f"{candidate.get('usefulness_score')} | {candidate.get('recommended_action')} | {files} |"
        )
    overlaps = report.get("overlap_groups") or []
    lines.extend(["", "## Duplicate / overlap groups", ""])
    if not overlaps:
        lines.append("No deterministic duplicates detected.")
    else:
        for group in overlaps:
            lines.append(f"- `{group.get('overlap_key')}`: {', '.join(group.get('task_ids') or [])}")
    lines.extend(["", "## Notes", ""])
    for note in report.get("notes") or []:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def write_repair_test_plan(candidate_dir: str | Path, *, output_dir: str | Path, repo_path: str = "", focused_tests: list[str] | None = None) -> dict[str, Any]:
    """Write a report-only repair/test plan for one candidate artifact directory."""
    candidate = collect_candidate(candidate_dir)
    patch_path = Path(candidate.get("normalized_patch_path") or "")
    normalized_patch = _read_text(patch_path) if patch_path.exists() else ""
    apply_check = git_apply_check(normalized_patch, repo_path) if normalized_patch else {"status": "skipped", "reason": "no_normalized_patch"}
    worth_repair = candidate["classification"] in {"usable_raw_diff", "repairable_fenced_diff", "idea_only"} and not candidate["allowed_file_violations"]
    plan = {
        "schema": "parallel_swarm.openrouter_repair_test_plan.v1",
        "candidate": candidate,
        "deterministic_extraction_has_normalized_patch": bool(normalized_patch.strip()),
        "git_apply_check": apply_check,
        "worth_manual_jarvis_repair": bool(worth_repair),
        "focused_tests_required_if_repaired": focused_tests or [],
        "trading_v4_mutation_performed": False,
    }
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{candidate['task_id']}_repair_test_plan.json"
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2, sort_keys=True)
    plan["json_report_path"] = str(json_path)
    return plan
