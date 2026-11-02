"""Real stdio server check — same path a client uses.

Spawns `loom-mcp` as a subprocess and speaks MCP over stdio with the official
SDK client, exactly like Claude Desktop / Hermes would.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"


@pytest.mark.skipif(not PYTHON.exists(), reason="project venv missing")
async def test_stdio_server_lists_tools():
    env = dict(os.environ)
    env["LOOM_DB"] = str(REPO / ".pytest_stdio.db")
    env["PYTHONPATH"] = str(REPO / "src")

    params = StdioServerParameters(
        command=str(PYTHON),
        args=["-m", "loom.server"],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert names == {
                "ctx_store",
                "ctx_get",
                "ctx_check",
                "ctx_stats",
                "ctx_profile",
                "ctx_list_sessions",
                "ctx_purge_session",
                "ctx_purge_expired",
                "ctx_vacuum",
                "ctx_health",
                "ctx_store_file",
                "ctx_store_cmd",
                "ctx_search",
                "ctx_verify",
        "ctx_transfer",
        "ctx_rebuild_index",
        "ctx_vector_stats",
            }
            print("\nTOOLS:", sorted(names))
            print("SCHEMAS OK for all tools")
    sys.stdout.flush()
