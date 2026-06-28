"""Tests for OpenRouter wave controller manifest and monitor helpers."""

import json
from datetime import datetime, timezone
from pathlib import Path

from plugins.parallel_swarm.python.helpers.openrouter_wave_controller import (
    apply_budget_guard,
    compute_next_state,
    create_wave_manifest,
    load_manifest,
    refresh_manifest_state,
    register_scheduler_task,
    summarize_registered_scheduler_tasks,
    summarize_workers,
    update_worker_from_result,
    write_monitor_report,
)


def test_create_wave_manifest_is_durable_and_planned(tmp_path):
    manifest = create_wave_manifest(
        run_id="wave_test",
        output_dir=tmp_path,
        max_workers_total=35,
        max_concurrency=10,
        token_budget=1000,
        cost_budget_usd=1.25,
        worker_payloads=[{"id": "w1", "model": "m", "lane": "M5", "role": "coder"}],
    )
    saved = load_manifest(tmp_path / "wave_manifest.json")
    assert manifest["state"] == "planned"
    assert saved["run_id"] == "wave_test"
    assert saved["workers"][0]["task_id"] == "w1"
    assert saved["trading_v4_mutation_performed"] is False


def test_update_worker_from_result_and_summary_counts(tmp_path):
    manifest = create_wave_manifest(run_id="wave", output_dir=tmp_path, max_workers_total=2, max_concurrency=2)
    result_path = tmp_path / "w1" / "pilot_result.json"
    result_path.parent.mkdir()
    result_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "task_id": "w1",
                "model": "model-a",
                "output_dir": str(tmp_path / "w1" / "tasks" / "w1"),
                "auditable_worker_result": True,
                "usage": {"total_tokens": 50, "raw_usage": {"cost": 0.0123}},
            }
        ),
        encoding="utf-8",
    )
    worker = update_worker_from_result(manifest, task_id="w1", result_path=result_path)
    summary = summarize_workers(manifest)
    assert worker["status"] == "completed"
    assert summary["counts"]["completed"] == 1
    assert summary["tokens_used"] == 50
    assert summary["cost_usd"] == 0.0123


def test_compute_next_state_tracks_worker_completion_and_lane_lead_readiness(tmp_path):
    manifest = create_wave_manifest(
        run_id="wave",
        output_dir=tmp_path,
        max_workers_total=2,
        max_concurrency=2,
        worker_payloads=[{"id": "a"}, {"id": "b"}],
    )
    manifest["workers"][0]["status"] = "completed"
    manifest["workers"][1]["status"] = "running"
    assert compute_next_state(manifest) == "workers_running"
    manifest["workers"][1]["status"] = "failed"
    assert compute_next_state(manifest) == "ready_for_lane_lead"
    manifest["lane_lead_reports"] = [{"path": "lane.json"}]
    assert compute_next_state(manifest) == "lane_leads_done"
    manifest["repair_test_plans"] = [{"path": "plan.json"}]
    assert compute_next_state(manifest) == "ready_for_jarvis"


def test_budget_guard_blocks_when_cost_or_tokens_exceeded(tmp_path):
    manifest = create_wave_manifest(
        run_id="wave",
        output_dir=tmp_path,
        max_workers_total=1,
        max_concurrency=1,
        token_budget=10,
        cost_budget_usd=0.01,
        worker_payloads=[{"id": "a"}],
    )
    manifest["workers"][0].update({"status": "completed", "token_usage": {"total_tokens": 11}, "cost_usd": 0.02})
    guard = apply_budget_guard(manifest)
    assert guard["token_budget_exceeded"] is True
    assert guard["cost_budget_exceeded"] is True
    assert compute_next_state(manifest) == "blocked"


def test_register_scheduler_task_records_uuid_without_creating_task(tmp_path):
    manifest = create_wave_manifest(run_id="wave", output_dir=tmp_path, max_workers_total=1, max_concurrency=1)
    record = register_scheduler_task(manifest, uuid="abc123", name="lane lead", stage="lane_lead", prompt_path="prompt.md")
    register_scheduler_task(manifest, uuid="abc123", name="lane lead", stage="lane_lead", prompt_path="prompt.md")
    assert record["uuid"] == "abc123"
    assert len(manifest["scheduler_tasks"]) == 1


def test_write_monitor_report_is_report_only_and_recommends_next_action(tmp_path):
    manifest = create_wave_manifest(
        run_id="wave",
        output_dir=tmp_path,
        max_workers_total=1,
        max_concurrency=1,
        worker_payloads=[{"id": "a"}],
    )
    manifest["workers"][0]["status"] = "completed"
    report = write_monitor_report(manifest, output_dir=tmp_path / "monitor")
    assert report["state"] == "ready_for_lane_lead"
    assert report["trading_v4_mutation_performed"] is False
    assert Path(report["monitor_json_report_path"]).exists()
    markdown = Path(report["monitor_markdown_report_path"]).read_text(encoding="utf-8")
    assert "run_lane_lead_aggregation" in markdown



def test_update_worker_from_result_can_persist_manifest_for_later_reload(tmp_path):
    manifest = create_wave_manifest(
        run_id="wave_persist",
        output_dir=tmp_path,
        max_workers_total=1,
        max_concurrency=1,
        worker_payloads=[{"id": "w1"}],
    )
    result_path = tmp_path / "w1" / "pilot_result.json"
    result_path.parent.mkdir()
    result_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "task_id": "w1",
                "model": "model-a",
                "output_dir": str(tmp_path / "w1" / "tasks" / "w1"),
                "auditable_worker_result": True,
                "usage": {"total_tokens": 3, "raw_usage": {"cost": 0.001}},
            }
        ),
        encoding="utf-8",
    )
    update_worker_from_result(
        manifest,
        task_id="w1",
        result_path=result_path,
        manifest_path=tmp_path / "wave_manifest.json",
    )
    reloaded = load_manifest(tmp_path / "wave_manifest.json")
    assert reloaded["workers"][0]["status"] == "completed"
    assert compute_next_state(reloaded) == "ready_for_lane_lead"

def test_summarize_registered_scheduler_tasks_flags_overdue_running_tasks(tmp_path):
    manifest = create_wave_manifest(run_id="wave_sched", output_dir=tmp_path, max_workers_total=1, max_concurrency=1)
    register_scheduler_task(manifest, uuid="stuck123", name="stuck worker", stage="worker", prompt_path="prompt.md")
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "uuid": "stuck123",
                        "name": "stuck worker",
                        "state": "running",
                        "type": "planned",
                        "updated_at": "2026-06-28T00:00:00+00:00",
                        "last_run": None,
                        "next_run": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_registered_scheduler_tasks(
        manifest,
        tasks_path=tasks_path,
        now=datetime(2026, 6, 28, 1, 0, tzinfo=timezone.utc),
        default_timeout_minutes=30,
    )

    assert summary["report_only"] is True
    assert summary["mutates_scheduler"] is False
    assert summary["registered_count"] == 1
    assert summary["found_count"] == 1
    assert summary["running_count"] == 1
    assert summary["overdue_count"] == 1
    assert summary["tasks"][0]["age_minutes"] == 60
    assert summary["tasks"][0]["overdue_running"] is True
    assert summary["tasks"][0]["recommended_action"] == "recover_or_reset_overdue_scheduler_task"


def test_write_monitor_report_surfaces_overdue_registered_scheduler_tasks(tmp_path):
    manifest = create_wave_manifest(
        run_id="wave_sched_report",
        output_dir=tmp_path,
        max_workers_total=1,
        max_concurrency=1,
        worker_payloads=[{"id": "a"}],
    )
    manifest["workers"][0]["status"] = "running"
    register_scheduler_task(manifest, uuid="overdue456", name="overdue scheduled worker", stage="worker", prompt_path="prompt.md")
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "uuid": "overdue456",
                        "name": "overdue scheduled worker",
                        "state": "running",
                        "type": "planned",
                        "updated_at": "2026-06-28T00:00:00Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = write_monitor_report(
        manifest,
        output_dir=tmp_path / "monitor",
        tasks_path=tasks_path,
        now=datetime(2026, 6, 28, 1, 0, tzinfo=timezone.utc),
    )

    assert report["scheduler_task_summary"]["overdue_count"] == 1
    assert "overdue_registered_scheduler_tasks" in report["monitor_alerts"]
    assert report["scheduler_task_summary"]["mutates_scheduler"] is False
    markdown = Path(report["monitor_markdown_report_path"]).read_text(encoding="utf-8")
    assert "Scheduler tasks overdue: `1`" in markdown
    assert "overdue456" in markdown
    assert "overdue scheduled worker" in markdown
