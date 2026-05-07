"""Shared fixtures for synapse-research tests.

The server module is a singleton that binds to a workspace at import time. For
per-test isolation we point ``UPJACK_ROOT`` at ``tmp_path`` and reload the
module so ``create_server`` and ``UpjackApp.from_manifest`` re-bind to the
fresh directory.

The ``fake_researcher`` fixture swaps ``mcp_research.worker.GPTResearcher``
for a scripted ``FakeGPTR`` so tests exercise the full lifecycle without
making real LLM / Tavily calls. The ``mcp`` and ``app_and_mcp`` fixtures
depend on it so spec-compliance and worker tests never accidentally hit the
real provider. ``fake_researcher`` is intentionally not autouse — a future
integration test can opt out by requesting ``mcp``/``app_and_mcp`` directly
via a sibling fixture.
"""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _no_provider_keys(monkeypatch):
    """Strip provider API keys from the test env so any code path that
    attempts a real call (the new background title generator, future
    additions) silently no-ops instead of hitting a live provider.
    Matches the keyless policy in CLAUDE.md — CI already runs without
    these set; this just guarantees the same on a developer's machine
    where their shell may have them exported."""
    for key in ("ANTHROPIC_API_KEY", "TAVILY_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def fake_researcher(monkeypatch):
    """Patch ``GPTResearcher`` with a scripted fake that drives handler callbacks."""

    class FakeGPTR:
        def __init__(self, query, report_type, log_handler, **kwargs):
            self.query = query
            self.report_type = report_type
            self.handler = log_handler

        async def conduct_research(self):
            await self.handler.on_research_step("planning", {})
            await self.handler.on_tool_start("tavily_search")
            await self.handler.on_research_step(
                "scraping",
                {"urls": ["https://example.com/a", "https://example.com/b"]},
            )

        async def write_report(self):
            return f"# {self.query}\n\nFake research report covering {self.query}."

        def get_research_sources(self):
            return [
                {
                    "url": "https://example.com/a",
                    "title": "A",
                    "content": "snippet content",
                }
            ]

    monkeypatch.setattr("mcp_research.worker.GPTResearcher", FakeGPTR)
    return FakeGPTR


@pytest.fixture
def mcp(tmp_path, monkeypatch, fake_researcher):
    """Fresh server bound to an isolated workspace, with GPTResearcher patched."""
    monkeypatch.setenv("UPJACK_ROOT", str(tmp_path))
    monkeypatch.delenv("MPAK_WORKSPACE", raising=False)

    import mcp_research.worker as worker_module

    importlib.reload(worker_module)

    import mcp_research.server as server_module

    importlib.reload(server_module)

    # Re-apply the GPTResearcher patch against the freshly reloaded worker
    # module — ``importlib.reload`` re-binds the symbol back to the real
    # class, so the patch from ``fake_researcher`` would otherwise be lost.
    monkeypatch.setattr(worker_module, "GPTResearcher", fake_researcher)

    return server_module.mcp


@pytest.fixture
def app_and_mcp(tmp_path, monkeypatch, fake_researcher):
    """Same as ``mcp`` but also exposes the UpjackApp instance for entity reads."""
    monkeypatch.setenv("UPJACK_ROOT", str(tmp_path))
    monkeypatch.delenv("MPAK_WORKSPACE", raising=False)

    import mcp_research.worker as worker_module

    importlib.reload(worker_module)

    import mcp_research.server as server_module

    importlib.reload(server_module)

    monkeypatch.setattr(worker_module, "GPTResearcher", fake_researcher)

    return server_module._app, server_module.mcp


async def tool_names(mcp) -> set[str]:
    from fastmcp import Client

    async with Client(mcp) as client:
        tools = await client.list_tools()
        return {t.name for t in tools}


async def tool_defs(mcp) -> dict[str, Any]:
    """Return a dict of tool_name -> tool_definition (for inspecting task support)."""
    from fastmcp import Client

    async with Client(mcp) as client:
        tools = await client.list_tools()
        return {t.name: t for t in tools}


def parse_text_content(content: list) -> Any:
    """Unwrap the first text block from a tool result as JSON where possible."""
    if not content:
        return None
    text = content[0].text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
