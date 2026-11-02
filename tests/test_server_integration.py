"""Integration test: the real FastMCP server, over a real client session.

This is the test that proves Loom is actually usable as an MCP server — not just
that its internals work.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402

from loom import store  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "loom.db")
    yield


@pytest.fixture
def client():
    from loom.server import mcp

    return Client(mcp)


async def test_server_lists_five_tools(client):
    async with client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    # Core tools + session management tools + new tools
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


async def test_store_get_roundtrip_over_mcp(client):
    content = "exit_code: 0\nerror: fail https://a.example.com/v1\nport 8080\n"
    async with client:
        res = await client.call_tool("ctx_store", {"content": content, "label": "log"})
        data = res.data
        assert data["lossless"] is True
        assert data["chunk_ids"]

        got = await client.call_tool("ctx_get", {"chunk_id": data["chunk_ids"][0]})
        assert got.data["found"] is True
        assert got.data["content"] == content


async def test_check_reports_coverage_over_mcp(client):
    content = "exit_code: 0\npath C:\\Temp\\x.txt\nnever print secrets\n"
    async with client:
        await client.call_tool("ctx_store", {"content": content, "label": "log"})
        res = await client.call_tool("ctx_check", {"session_id": "default"})
    assert res.data["coverage"] == 1.0
    assert res.data["lossless"] is True


async def test_profile_switch(client):
    async with client:
        res = await client.call_tool("ctx_profile", {"mode": "strict"})
    assert res.data["active"] == "strict"
    assert res.data["allow_lossy"] is False
