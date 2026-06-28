"""Tests for safe OpenRouter wave launch executor adapter."""

from pathlib import Path

from plugins.parallel_swarm.python.helpers.openrouter_wave_dispatcher import build_dispatch_plan
from plugins.parallel_swarm.python.helpers.openrouter_wave_controller import load_manifest
from plugins.parallel_swarm.python.helpers.openrouter_wave_launch_executor import (
    execute_next_launch_batch,
    write_launch_execution_markdown,
)


def _payload(task_id: str) -> dict:
    return {"id": task_id, "backend": "openrouter", "model": "model/test", "lane": "M5", "role": "worker"}


def test_dry_run_marks_next_batch_ready_without_launching(tmp_path):
    plan = build_dispatch_plan(
        run_id="wave_launch",
        output_dir=tmp_path,
        worker_payloads=[_payload("a"), _payload("b"), _payload("c")],
        max_workers_total=35,
        max_concurrency=2,
    )
    record = execute_next_launch_batch(plan["manifest_path"].replace("wave_manifest.json", "dispatch_plan.json"), output_dir=tmp_path)
    assert record["status"] == "dry_run_ready"
    assert record["openrouter_calls_launched"] is False
    assert record["trading_v4_mutation_performed"] is False
    assert record["selected_batch"]["worker_ids"] == ["a", "b"]
    manifest = load_manifest(tmp_path / "wave_manifest.json")
    assert manifest["last_launch_status"] == "dry_run_ready"


def test_non_dry_run_blocks_without_explicit_launch_approval(tmp_path):
    build_dispatch_plan(run_id="wave_block", output_dir=tmp_path, worker_payloads=[_payload("a")])
    record = execute_next_launch_batch(tmp_path / "dispatch_plan.json", output_dir=tmp_path, dry_run=False)
    assert record["status"] == "blocked"
    assert "explicit_openrouter_launch_approval_required" in record["errors"]
    assert record["openrouter_calls_launched"] is False


def test_explicit_launch_flag_still_requires_separate_runner(tmp_path):
    build_dispatch_plan(run_id="wave_ready", output_dir=tmp_path, worker_payloads=[_payload("a")])
    record = execute_next_launch_batch(
        tmp_path / "dispatch_plan.json",
        output_dir=tmp_path,
        dry_run=False,
        allow_openrouter_launch=True,
    )
    assert record["status"] == "launch_ready_requires_runner"
    assert record["openrouter_calls_launched"] is False
    assert "separately-gated runner" in " ".join(record["notes"])


def test_invalid_plan_blocks_and_writes_evidence(tmp_path):
    plan = build_dispatch_plan(
        run_id="bad",
        output_dir=tmp_path,
        worker_payloads=[{"id": "bad", "backend": "agent_zero"}],
    )
    assert plan["status"] == "invalid"
    record = execute_next_launch_batch(tmp_path / "dispatch_plan.json", output_dir=tmp_path)
    assert record["status"] == "blocked"
    assert "dispatch_plan_invalid" in record["errors"]
    assert Path(tmp_path / "launch_execution.json").exists()


def test_launch_execution_markdown_records_boundaries(tmp_path):
    build_dispatch_plan(run_id="wave_md", output_dir=tmp_path, worker_payloads=[_payload("a")])
    record = execute_next_launch_batch(tmp_path / "dispatch_plan.json", output_dir=tmp_path)
    md_path = write_launch_execution_markdown(record, output_dir=tmp_path)
    markdown = Path(md_path).read_text(encoding="utf-8")
    assert "# OpenRouter Wave Launch Execution Report" in markdown
    assert "OpenRouter calls launched: `False`" in markdown
    assert "Trading V4 mutation performed: `False`" in markdown
