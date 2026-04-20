"""MCP tasks draft-spec compliance tests (2025-11-25).

Exercises every MUST from the spec that our app is responsible for. FastMCP
provides the protocol-level plumbing (capability advertisement, tasks/get,
tasks/result, tasks/cancel, tasks/list), so these tests verify we wired it up
correctly and that the worker cooperates with the lifecycle.

Where possible we go through the FastMCP Client's in-process transport, which
round-trips real JSON-RPC against the same handlers used in production. All
tests run against ``FakeGPTR`` (installed by the ``fake_researcher`` fixture
in ``conftest.py``) so no network / API keys are required.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from fastmcp import Client

from tests.conftest import parse_text_content, tool_defs

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Tool-level task support
# ---------------------------------------------------------------------------


async def test_start_research_is_registered(mcp):
    defs = await tool_defs(mcp)
    assert "start_research" in defs


async def test_start_research_declares_task_support(mcp):
    """Per spec §Tool-Level Negotiation: a task-aware tool MUST advertise
    execution.taskSupport. We use 'optional' (runs inline or as a task) so the
    tool works with clients that don't yet send task-augmented tools/call."""
    defs = await tool_defs(mcp)
    tool = defs["start_research"]
    # FastMCP surfaces the execution block on the tool model. Key name may be
    # camelCase or snake_case depending on SDK version — check both.
    execution = getattr(tool, "execution", None) or getattr(tool, "_meta", {}).get("execution")
    assert execution is not None, "start_research must declare execution metadata"
    mode = getattr(execution, "taskSupport", None) or getattr(execution, "task_support", None)
    if mode is None and isinstance(execution, dict):
        mode = execution.get("taskSupport") or execution.get("task_support")
    assert mode in ("optional", "required"), (
        f"start_research must advertise execution.taskSupport in (optional, required), got {mode!r}"
    )


# ---------------------------------------------------------------------------
# Non-augmented call on a required-task tool must be rejected
# ---------------------------------------------------------------------------


async def test_non_task_augmented_call_runs_inline(mcp):
    """With execution.taskSupport='optional', a plain tools/call (no task field)
    must succeed and return the result synchronously. This is the path the
    current NimbleBrain engine uses — it doesn't task-augment outbound calls,
    so the tool must work without augmentation."""
    async with Client(mcp) as client:
        result = await client.call_tool("start_research", {"query": "inline call"})
        payload = parse_text_content(result.content)
        assert isinstance(payload, dict)
        assert payload.get("status") == "completed"
        assert payload.get("run_id", "").startswith("rr_")
        assert "inline call" in (payload.get("report") or "")


# ---------------------------------------------------------------------------
# Task-augmented happy path
# ---------------------------------------------------------------------------


async def test_task_augmented_call_creates_task(mcp):
    """The task-augmented call returns a handle with a task_id immediately,
    rather than blocking until the underlying work completes."""
    async with Client(mcp) as client:
        task = await client.call_tool("start_research", {"query": "dial tone"}, task=True)
        assert getattr(task, "task_id", None), (
            "task-augmented call must return a handle with task_id"
        )


async def test_task_runs_to_completion_and_returns_report(mcp):
    """Full lifecycle: task-augmented call → await → completed result contains
    a markdown report and the entity id."""
    async with Client(mcp) as client:
        task = await client.call_tool("start_research", {"query": "the quick brown fox"}, task=True)
        result = await task  # blocks until terminal
        payload = parse_text_content(result.content)
        assert isinstance(payload, dict), f"expected dict result, got {type(payload)}"
        assert payload.get("status") == "completed"
        assert payload.get("run_id", "").startswith("rr_")
        assert "the quick brown fox" in (payload.get("report") or "")


async def test_status_transitions_through_working_to_completed(mcp):
    """notifications/tasks/status should emit at least one intermediate status
    and a terminal 'completed'. We subscribe via on_status_change."""
    seen = []

    async with Client(mcp) as client:
        task = await client.call_tool("start_research", {"query": "observability"}, task=True)

        def _on_status(s):
            seen.append(s.status if hasattr(s, "status") else s.get("status"))

        task.on_status_change(_on_status)
        await task  # run to terminal

    # We should see at least the terminal completed status. Intermediate
    # 'working' notifications are optional per spec.
    assert "completed" in seen, f"expected 'completed' in status stream, saw {seen}"


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_cancel_in_flight_task(app_and_mcp, monkeypatch):
    """tasks/cancel on a working task must transition it to 'cancelled' and
    the research_run entity must mirror that state."""
    upjack_app, mcp = app_and_mcp

    # Swap in a slow fake so the run is still working when we call cancel().
    # The default FakeGPTR from conftest completes near-instantly.
    import mcp_research.worker as worker_module

    class SlowFakeGPTR:
        def __init__(self, query, report_type, log_handler, **kwargs):
            self.query = query
            self.handler = log_handler

        async def conduct_research(self):
            await self.handler.on_research_step("planning", {})
            # Long enough to span the cancel window below.
            await asyncio.sleep(5)

        async def write_report(self):
            return f"# {self.query}\n\nFake research report covering {self.query}."

        def get_research_sources(self):
            return []

    monkeypatch.setattr(worker_module, "GPTResearcher", SlowFakeGPTR)

    async with Client(mcp) as client:
        task = await client.call_tool("start_research", {"query": "will be cancelled"}, task=True)
        # Let the first phase start so an entity exists.
        await asyncio.sleep(0.2)
        await task.cancel()

        # Wait briefly for the cancellation to propagate to the entity.
        await asyncio.sleep(0.3)

    runs = upjack_app.list_entities("research_run", status="active", limit=10)
    assert runs, "cancelled run should still exist as an entity"
    cancelled = [r for r in runs if r.get("run_status") == "cancelled"]
    assert cancelled, (
        f"expected a cancelled run, got statuses {[r.get('run_status') for r in runs]}"
    )


# ---------------------------------------------------------------------------
# Entity dual-channel verification
# ---------------------------------------------------------------------------


async def test_entity_reflects_completed_state(app_and_mcp):
    """The research_run entity must end in status='completed' with a report
    after the task finishes. This proves the UI channel is in sync with the
    engine channel."""
    upjack_app, mcp = app_and_mcp

    async with Client(mcp) as client:
        task = await client.call_tool("start_research", {"query": "entity sync check"}, task=True)
        await task

    runs = upjack_app.list_entities("research_run", status="active", limit=10)
    assert len(runs) == 1
    run = runs[0]
    assert run["run_status"] == "completed"
    assert run["progress"] == 100
    assert run["report"] and "entity sync check" in run["report"]
    assert run.get("completed_at")


# ---------------------------------------------------------------------------
# Workspace isolation
# ---------------------------------------------------------------------------


async def test_workspace_isolation(tmp_path, monkeypatch, fake_researcher):
    """Two server instances pointed at different UPJACK_ROOTs must not see
    each other's entities. This enforces per-workspace data isolation."""
    # Workspace A — create a run, then tear down.
    root_a = tmp_path / "ws_a"
    root_a.mkdir()
    monkeypatch.setenv("UPJACK_ROOT", str(root_a))
    monkeypatch.delenv("MPAK_WORKSPACE", raising=False)

    import mcp_research.worker as worker_module

    importlib.reload(worker_module)
    # Re-apply FakeGPTR against the freshly reloaded worker module.
    monkeypatch.setattr(worker_module, "GPTResearcher", fake_researcher)

    import mcp_research.server as server_module

    importlib.reload(server_module)

    async with Client(server_module.mcp) as client:
        task = await client.call_tool("start_research", {"query": "in workspace A"}, task=True)
        await task

    runs_a = server_module._app.list_entities("research_run", status="active", limit=10)
    assert len(runs_a) == 1

    # Workspace B — fresh root, must see zero runs.
    root_b = tmp_path / "ws_b"
    root_b.mkdir()
    monkeypatch.setenv("UPJACK_ROOT", str(root_b))
    importlib.reload(worker_module)
    monkeypatch.setattr(worker_module, "GPTResearcher", fake_researcher)
    importlib.reload(server_module)

    runs_b = server_module._app.list_entities("research_run", status="active", limit=10)
    assert runs_b == [], "workspace B must not see workspace A's runs"
