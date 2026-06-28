import json

from python.helpers.tool import Tool, Response
from python.helpers import dirty_json
from plugins.parallel_swarm.python.helpers.pilot_launcher import run_one_openrouter_payload


class RunOpenRouterWorker(Tool):
    """Run one OpenRouter-backed swarm worker with deterministic artifacts."""

    async def execute(self, task="", output_dir="", **kwargs):
        payload = dirty_json.DirtyJson.parse_string(str(task))
        if not isinstance(payload, dict):
            return Response(
                message="Error: 'task' must be a JSON object for one OpenRouter worker.",
                break_loop=False,
            )
        if not output_dir:
            return Response(
                message="Error: 'output_dir' is required so artifacts are durable.",
                break_loop=False,
            )
        result = await run_one_openrouter_payload(payload, run_out=str(output_dir))
        return Response(message=json.dumps(result, indent=2, sort_keys=True), break_loop=False)
