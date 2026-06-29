"""Tests for report-only OpenRouter lane-lead aggregation."""

import json
from pathlib import Path

from plugins.parallel_swarm.python.helpers.openrouter_lane_lead import build_lane_lead_report, write_repair_test_plan


def _diff(path="tests/test_example.py"):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1,2 @@\n old\n+new\n"


def _candidate(base: Path, task_id: str, classification: str, *, touched=None, violation=None, raw="", patch="") -> Path:
    out = base / task_id
    out.mkdir(parents=True)
    touched = touched or []
    violation = violation or []
    patch = patch or (_diff(touched[0]) if touched else "")
    raw = raw or patch or f"Idea for {task_id}"
    normalized = out / "normalized_candidate_patch.diff"
    normalized.write_text(patch, encoding="utf-8")
    (out / "raw_response.md").write_text(raw, encoding="utf-8")
    metadata = {
        "task_id": task_id,
        "description": f"Review {task_id}",
        "status": "completed",
        "normalized_candidate_patch_path": str(normalized),
        "candidate_classification": {
            "classification": classification,
            "normalized_patch_path": str(normalized),
            "patch_validation": {
                "touched_files": touched,
                "allowed_files_violated": violation,
            },
            "normalized_patch_validation": {
                "touched_files": touched,
                "allowed_files_violated": violation,
            },
        },
    }
    with (out / "metadata.json").open("w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, sort_keys=True)
    return out


def test_lane_aggregation_groups_and_ranks_multiple_classifications(tmp_path):
    usable = _candidate(tmp_path, "usable", "usable_raw_diff", touched=["tests/test_example.py"])
    repairable = _candidate(tmp_path, "repairable", "repairable_fenced_diff", touched=["tests/test_other.py"])
    idea = _candidate(tmp_path, "idea", "idea_only", raw="Consider adding a boundary test.")
    out = tmp_path / "report"

    report = build_lane_lead_report([usable, repairable, idea], output_dir=out)

    assert report["candidate_count"] == 3
    assert report["groups_by_classification"]["usable_raw_diff"] == ["usable"]
    assert report["groups_by_classification"]["repairable_fenced_diff"] == ["repairable"]
    assert report["groups_by_classification"]["idea_only"] == ["idea"]
    assert [item["task_id"] for item in report["ranked_candidates"]] == ["usable", "repairable", "idea"]


def test_unsafe_out_of_scope_candidates_are_ranked_as_reject_skip(tmp_path):
    unsafe = _candidate(
        tmp_path,
        "unsafe",
        "unsafe_or_out_of_scope",
        touched=["src/live_broker.py"],
        violation=["src/live_broker.py"],
    )
    safe = _candidate(tmp_path, "safe", "repairable_fenced_diff", touched=["tests/test_example.py"])

    report = build_lane_lead_report([unsafe, safe], output_dir=tmp_path / "report")

    by_id = {item["task_id"]: item for item in report["ranked_candidates"]}
    assert by_id["unsafe"]["recommended_action"] == "reject_or_skip"
    assert by_id["unsafe"]["usefulness_score"] == 0
    assert report["ranked_candidates"][0]["task_id"] == "safe"


def test_repairable_fenced_diffs_are_preserved_for_validation_or_repair(tmp_path):
    candidate = _candidate(tmp_path, "repairable", "repairable_fenced_diff", touched=["tests/test_example.py"])

    report = build_lane_lead_report([candidate], output_dir=tmp_path / "report")

    ranked = report["ranked_candidates"][0]
    assert ranked["classification"] == "repairable_fenced_diff"
    assert ranked["recommended_action"] == "preserve_for_validation_or_repair"
    assert Path(ranked["normalized_patch_path"]).read_text(encoding="utf-8") == _diff()


def test_json_and_markdown_reports_are_written_deterministically(tmp_path):
    candidate = _candidate(tmp_path, "c1", "usable_raw_diff", touched=["tests/test_example.py"])

    report = build_lane_lead_report([tmp_path], output_dir=tmp_path / "report")

    json_path = Path(report["json_report_path"])
    md_path = Path(report["markdown_report_path"])
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = md_path.read_text(encoding="utf-8")
    assert saved["candidate_task_ids"] == ["c1"]
    assert "# OpenRouter Lane-Lead Aggregation Report" in markdown
    assert "usable_raw_diff" in markdown
    assert report["trading_v4_mutation_performed"] is False


def test_detects_simple_duplicate_overlap_by_touched_files(tmp_path):
    first = _candidate(tmp_path, "dup_a", "usable_raw_diff", touched=["tests/test_example.py"])
    second = _candidate(tmp_path, "dup_b", "repairable_fenced_diff", touched=["tests/test_example.py"])

    report = build_lane_lead_report([first, second], output_dir=tmp_path / "report")

    assert report["overlap_groups"] == [
        {"overlap_key": "files:tests/test_example.py", "task_ids": ["dup_a", "dup_b"], "count": 2}
    ]


def test_repair_test_plan_is_report_only_and_records_apply_check(tmp_path):
    candidate = _candidate(tmp_path, "repairable", "repairable_fenced_diff", touched=["tests/test_example.py"])

    plan = write_repair_test_plan(
        candidate,
        output_dir=tmp_path / "plans",
        repo_path=str(tmp_path / "missing_repo"),
        focused_tests=["python -m pytest -q tests/test_example.py"],
    )

    assert plan["deterministic_extraction_has_normalized_patch"] is True
    assert plan["git_apply_check"]["status"] == "skipped"
    assert plan["git_apply_check"]["reason"] == "repo_path_missing"
    assert plan["worth_manual_jarvis_repair"] is True
    assert plan["trading_v4_mutation_performed"] is False
    assert Path(plan["json_report_path"]).exists()


def _candidate_with_apply(base: Path, task_id: str, classification: str, *, touched, candidate_status, repaired_status=None) -> Path:
    """Build a candidate whose metadata records git_apply_check statuses."""
    out = base / task_id
    out.mkdir(parents=True)
    patch = _diff(touched[0])
    normalized = out / "normalized_candidate_patch.diff"
    normalized.write_text(patch, encoding="utf-8")
    (out / "raw_response.md").write_text(patch, encoding="utf-8")
    candidate_classification = {
        "classification": classification,
        "normalized_patch_path": str(normalized),
        "patch_validation": {
            "touched_files": touched,
            "allowed_files_violated": [],
            "git_apply_check": {"status": candidate_status},
        },
        "normalized_patch_validation": {
            "touched_files": touched,
            "allowed_files_violated": [],
            "git_apply_check": {"status": candidate_status},
        },
    }
    if repaired_status is not None:
        candidate_classification["repaired_patch_validation"] = {
            "touched_files": touched,
            "allowed_files_violated": [],
            "git_apply_check": {"status": repaired_status},
        }
    metadata = {
        "task_id": task_id,
        "description": f"Review {task_id}",
        "status": "completed",
        "normalized_candidate_patch_path": str(normalized),
        "candidate_classification": candidate_classification,
    }
    with (out / "metadata.json").open("w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, sort_keys=True)
    return out


def test_usable_diff_that_fails_git_apply_is_downgraded(tmp_path):
    clean = _candidate_with_apply(tmp_path, "clean", "usable_raw_diff", touched=["tests/test_a.py"], candidate_status="ok")
    broken = _candidate_with_apply(tmp_path, "broken", "usable_raw_diff", touched=["tests/test_b.py"], candidate_status="failed")

    report = build_lane_lead_report([clean, broken], output_dir=tmp_path / "report")
    by_id = {item["task_id"]: item for item in report["ranked_candidates"]}

    assert by_id["clean"]["git_apply_outcome"] == "applies_clean"
    assert by_id["clean"]["recommended_action"] == "validate_then_jarvis_review"

    assert by_id["broken"]["git_apply_outcome"] == "apply_failed"
    assert by_id["broken"]["recommended_action"] == "apply_failed_needs_fix_or_repair"
    assert by_id["broken"]["usefulness_score"] == 25
    # A failing diff must never outrank a genuinely clean one.
    assert report["ranked_candidates"][0]["task_id"] == "clean"


def test_usable_diff_rescued_by_repair_is_marked_repaired(tmp_path):
    rescued = _candidate_with_apply(
        tmp_path, "rescued", "usable_raw_diff", touched=["tests/test_c.py"],
        candidate_status="failed", repaired_status="ok",
    )

    report = build_lane_lead_report([rescued], output_dir=tmp_path / "report")
    ranked = report["ranked_candidates"][0]

    assert ranked["git_apply_outcome"] == "applies_after_repair"
    assert ranked["recommended_action"] == "validate_repaired_then_jarvis_review"
    assert ranked["repaired_git_apply_status"] == "ok"


def test_missing_git_apply_check_is_behavior_preserving_unknown(tmp_path):
    # No git_apply_check recorded -> unchanged usable behavior (outcome unknown).
    candidate = _candidate(tmp_path, "legacy", "usable_raw_diff", touched=["tests/test_d.py"])

    report = build_lane_lead_report([candidate], output_dir=tmp_path / "report")
    ranked = report["ranked_candidates"][0]

    assert ranked["git_apply_outcome"] == "unknown"
    assert ranked["recommended_action"] == "validate_then_jarvis_review"
    assert ranked["usefulness_score"] == 101
