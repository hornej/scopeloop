"""Exercise Git metadata collection with the real MCP stdio pipes, without hardware."""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_session_creation_under_stdio(tmp_path):
    async with asyncio.timeout(20):
        async with (
            stdio_client(
                StdioServerParameters(
                    command=sys.executable, args=["-m", "scopeloop.mcp_server"], cwd=str(tmp_path)
                )
            ) as (r, w),
            ClientSession(r, w) as client,
        ):
            await client.initialize()
            result = await client.call_tool("scopeloop_session_start", {})
            assert not result.isError
            assert json.loads(result.content[0].text)["started"]
            result = await client.call_tool("scopeloop_session_end", {"outcome": "success"})
            assert not result.isError
    metadata = list((tmp_path / "sessions").glob("*/git_state.json"))
    assert len(metadata) == 1
    assert json.loads(metadata[0].read_text())["commit_hash"] is None
