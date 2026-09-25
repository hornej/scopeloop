"""Verify an installed release and stdio MCP discovery without opening hardware."""

import asyncio
import importlib.metadata
import json
import platform
import subprocess
import sys
import sysconfig
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import scopeloop


async def discover():
    async with asyncio.timeout(20):
        async with stdio_client(
            StdioServerParameters(command=sys.executable, args=["-m", "scopeloop.mcp_server"])
        ) as (reader, writer), ClientSession(reader, writer) as client:
            await client.initialize()
            result = await client.list_tools()
            names = sorted(tool.name for tool in result.tools)
            for required in (
                "scopeloop_scope_capture",
                "scopeloop_logic_capture",
                "scopeloop_logic_disconnect",
                "scopeloop_evidence_export",
            ):
                assert required in names, required
            return names


def main():
    cli = Path(sysconfig.get_path("scripts")) / (
        "scopeloop.exe" if sys.platform == "win32" else "scopeloop"
    )
    for args in (["--help"], ["scope", "--help"], ["logic", "--help"]):
        result = subprocess.run([str(cli), *args], capture_output=True, timeout=20)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace"))
    print(
        json.dumps(
            {
                "host": platform.node(),
                "python": sys.executable,
                "module": scopeloop.__file__,
                "versions": {
                    name: importlib.metadata.version(name)
                    for name in ("scopeloop", "mcp", "logic2-automation", "numpy", "scipy", "typer")
                },
                "cli": str(cli),
                "mcp_tools": asyncio.run(discover()),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
