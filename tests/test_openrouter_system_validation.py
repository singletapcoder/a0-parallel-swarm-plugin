from pathlib import Path

from plugins.parallel_swarm.python.helpers.openrouter_system_validation import run_no_network_validation


def test_no_network_validation_exercises_parallel_swarm_operating_path(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    result = run_no_network_validation(tmp_path / "validation")

    assert result["schema"] == "parallel_swarm.openrouter_system_validation.v1"
    assert result["status"] == "pass"
    assert result["real_openrouter_network_calls"] is False
    assert result["real_openrouter_credentials_read"] is False
    assert result["trading_v4_mutation_performed"] is False
    assert result["scheduler_mutation_performed"] is False
    assert result["steps"]["dispatch"]["status"] == "pass"
    assert result["steps"]["launch_dry_run"]["status"] == "pass"
    assert result["steps"]["synthetic_workers"]["worker_count"] == 2
    assert result["steps"]["lane_lead"]["candidate_count"] == 2
    assert result["steps"]["monitor"]["registered_count"] == 2
    assert result["steps"]["monitor"]["overdue_count"] == 1
    assert result["steps"]["monitor"]["mutates_scheduler"] is False

    output = Path(result["output_dir"])
    assert (output / "validation_result.json").exists()
    assert (output / "dispatch" / "dispatch_plan.json").exists()
    assert (output / "launch" / "launch_execution.json").exists()
    assert (output / "lane_lead" / "lane_lead_report.json").exists()
    assert (output / "monitor" / "wave_controller_monitor_report.json").exists()
    for worker_record in result["steps"]["synthetic_workers"]["worker_records"]:
        assert Path(worker_record).exists()
