"""Parsing tests for OpenRouter fields in call_swarm."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from agent import Agent, AgentConfig, AgentContext


@pytest.mark.asyncio
async def test_call_swarm_parses_openrouter_fields():
    from tools.call_swarm import SwarmDelegation

    agent = Agent(number=0, config=AgentConfig(), context=AgentContext())
    tool = SwarmDelegation(agent=agent, name="call_swarm", method=None, args={}, message="", loop_data=None)
    tasks_json = json.dumps([
        {
            "id": "M5_001",
            "description": "Task",
            "message": "Do it",
            "backend": "openrouter",
            "model": "qwen/qwen-2.5-coder-32b-instruct",
            "role": "cheap_coder",
            "lane": "M5",
            "fallback_policy": "stop_not_direct_code",
            "output_dir": "/tmp/swarm/M5_001",
            "allowed_files": ["tests/test_position_accounting.py"],
            "forbidden_actions": ["broker_calls"],
            "expected_artifacts": ["metadata.json"],
        }
    ])
    captured = []

    async def capture_dispatch(self_orch, tasks):
        captured.extend(tasks)
        return {"M5_001": "done"}

    with patch("plugins.parallel_swarm.python.helpers.swarm.SwarmOrchestrator.dispatch", capture_dispatch), patch(
        "plugins.parallel_swarm.python.helpers.swarm.SwarmOrchestrator.format_results", return_value="done"
    ), patch(
        "plugins.parallel_swarm.python.helpers.token_pool.TokenPool.get_usage_report", new_callable=AsyncMock, return_value={"total_consumed": 0}
    ):
        await tool.execute(tasks=tasks_json)

    task = captured[0]
    assert task.backend == "openrouter"
    assert task.model == "qwen/qwen-2.5-coder-32b-instruct"
    assert task.role == "cheap_coder"
    assert task.lane == "M5"
    assert task.allowed_files == ["tests/test_position_accounting.py"]
    assert task.forbidden_actions == ["broker_calls"]
    assert task.expected_artifacts == ["metadata.json"]
