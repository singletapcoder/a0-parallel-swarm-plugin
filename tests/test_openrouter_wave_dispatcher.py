"""Tests for bounded OpenRouter wave dispatcher planning."""

import json
from pathlib import Path

from plugins.parallel_swarm.python.helpers.openrouter_wave_dispatcher import (
    build_dispatch_plan,
    validate_dispatch_inputs,
    write_dispatch_markdown,
)
from plugins.parallel_swarm.python.helpers.openrouter_wave_controller import load_manifest


def _payload(task_id: str, lane: str = "M5") -> dict:
    return {"id": task_id, "backend": "openrouter", "model": "model/test", "lane": lane, "role": "worker"}


def test_validate_dispatch_inputs_enforces_hard_cap_and_duplicate_ids():
    payloads = [_payload("a"), _payload("a")]
    result = validate_dispatch_inputs(payloads, max_workers_total=36, max_concurrency=4)
    assert result["valid"] is False
    assert "max_workers_total_exceeds_hard_cap_35" in result["errors"]
    assert "duplicate_worker_ids" in result["errors"]
    assert result["duplicate_worker_ids"] == ["a"]


def test_build_dispatch_plan_creates_manifest_and_batches_without_launching(tmp_path):
    payloads = [_payload(f"w{i}") for i in range(5)]
    plan = build_dispatch_plan(
        run_id="wave_dispatch",
        output_dir=tmp_path,
        worker_payloads=payloads,
        max_workers_total=35,
        max_concurrency=2,
        token_budget=1000,
        cost_budget_usd=0.5,
    )
    assert plan["status"] == "planned_dry_run"
    assert plan["openrouter_calls_launched"] is False
    assert plan["trading_v4_mutation_performed"] is False
    assert [batch["size"] for batch in plan["launch_batches"]] == [2, 2, 1]
    manifest = load_manifest(tmp_path / "wave_manifest.json")
    assert manifest["run_id"] == "wave_dispatch"
    assert manifest["dispatch_status"] == "planned_dry_run"
    assert len(manifest["workers"]) == 5


def test_invalid_dispatch_plan_is_written_without_manifest_launch(tmp_path):
    payloads = [_payload("w1", lane="M4"), {"id": "bad", "backend": "agent_zero"}]
    plan = build_dispatch_plan(run_id="bad_wave", output_dir=tmp_path, worker_payloads=payloads, max_workers_total=1, max_concurrency=1)
    assert plan["status"] == "invalid"
    assert plan["openrouter_calls_launched"] is False
    assert "worker_payload_count_exceeds_max_workers_total" in plan["validation"]["errors"]
    assert "worker_bad_backend_not_openrouter" in plan["validation"]["errors"]
    assert Path(tmp_path / "dispatch_plan.json").exists()


def test_write_dispatch_markdown_records_batches_and_boundaries(tmp_path):
    plan = build_dispatch_plan(
        run_id="wave_md",
        output_dir=tmp_path,
        worker_payloads=[_payload("a"), _payload("b")],
        max_workers_total=35,
        max_concurrency=2,
    )
    md_path = write_dispatch_markdown(plan, output_dir=tmp_path)
    markdown = Path(md_path).read_text(encoding="utf-8")
    assert "# OpenRouter Wave Dispatch Plan" in markdown
    assert "Batch 1" in markdown
    assert "OpenRouter calls launched: `False`" in markdown
    assert "Trading V4 mutation performed: `False`" in markdown
