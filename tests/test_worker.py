"""Direct worker tests — exercise ``run_research`` without the MCP transport.

The ``fake_researcher`` fixture (in ``conftest.py``) patches
``mcp_research.worker.GPTResearcher`` with a scripted ``FakeGPTR`` so every
test drives the real ``run_research`` orchestration without making network
calls. Individual tests mutate ``FakeGPTR.conduct_research`` /
``FakeGPTR.write_report`` to inject cancel / failure behaviour.

Each test asserts an observable post-condition on the entity or the context —
never on internal machinery of ``run_research`` or ``_MCPLogHandler``.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


class FakeContext:
    """Minimal stand-in for ``fastmcp.Context`` — records progress + info calls."""

    def __init__(self) -> None:
        self.progress_calls: list[tuple[int, int]] = []
        self.info_calls: list[str] = []

    async def report_progress(self, *, progress: int, total: int) -> None:
        self.progress_calls.append((progress, total))

    async def info(self, message: str) -> None:
        self.info_calls.append(message)


@pytest.fixture
def upjack_app(tmp_path, monkeypatch, fake_researcher):
    """Reload worker+server against a clean ``tmp_path`` and return (app, worker).

    ``fake_researcher`` monkeypatches ``GPTResearcher`` on the worker module,
    but ``importlib.reload`` would rebind the symbol back to the real class,
    so we re-apply the patch after reload.
    """
    monkeypatch.setenv("UPJACK_ROOT", str(tmp_path))
    monkeypatch.delenv("MPAK_WORKSPACE", raising=False)

    import mcp_research.worker as worker_module

    importlib.reload(worker_module)

    from upjack.app import UpjackApp

    manifest_path = Path(__file__).resolve().parent.parent / "manifest.json"
    app = UpjackApp.from_manifest(manifest_path, root=str(tmp_path))

    monkeypatch.setattr(worker_module, "GPTResearcher", fake_researcher)

    return app, worker_module


async def test_happy_path_returns_completed_and_finalises_entity(upjack_app):
    """A successful run returns ``completed``, the entity lands at progress=100
    with a non-empty source list, and the final progress call is ``(100, 100)``."""
    app, worker = upjack_app
    ctx = FakeContext()

    result = await worker.run_research(app, "hello", ctx)

    # Return contract (SPEC §3.7).
    assert result["status"] == "completed"
    assert result["run_id"].startswith("rr_")
    assert "hello" in result["report"]

    # Entity contract — exact 100, not just truthy.
    run = app.get_entity("research_run", result["run_id"])
    assert run["run_status"] == "completed"
    assert run["progress"] == 100
    assert run["report"] == result["report"]
    assert isinstance(run["sources"], list)
    assert len(run["sources"]) > 0

    # Context contract — the final call is the terminal 100/100.
    assert ctx.progress_calls, "run_research must emit at least one progress call"
    assert ctx.progress_calls[-1] == (100, 100)


async def test_cancel_mid_run_marks_entity_cancelled(upjack_app, fake_researcher):
    """A mid-run ``CancelledError`` transitions the entity to ``cancelled`` and
    populates ``completed_at`` before the exception re-raises."""
    app, worker = upjack_app

    async def cancel_immediately(self):
        raise asyncio.CancelledError

    fake_researcher.conduct_research = cancel_immediately

    ctx = FakeContext()
    with pytest.raises(asyncio.CancelledError):
        await worker.run_research(app, "to cancel", ctx)

    runs = app.list_entities("research_run", status="active", limit=10)
    assert len(runs) == 1
    assert runs[0]["run_status"] == "cancelled"
    assert runs[0].get("completed_at")


async def test_failure_marks_entity_failed_with_error_message(upjack_app, fake_researcher):
    """A raised ``RuntimeError`` during research transitions the entity to
    ``failed`` with ``error_message`` containing the failure text and
    ``completed_at`` populated — all before the exception re-raises."""
    app, worker = upjack_app

    async def boom(self):
        raise RuntimeError("boom")

    fake_researcher.conduct_research = boom

    ctx = FakeContext()
    with pytest.raises(RuntimeError, match="boom"):
        await worker.run_research(app, "will boom", ctx)

    runs = app.list_entities("research_run", status="active", limit=10)
    assert len(runs) == 1
    assert runs[0]["run_status"] == "failed"
    assert "boom" in (runs[0].get("error_message") or "")
    assert runs[0].get("completed_at")


async def test_progress_sequence_is_monotonically_non_decreasing(upjack_app):
    """Scan the entire recorded progress sequence — every step must be ``>=``
    the previous step, including intermediate phase transitions."""
    app, worker = upjack_app
    ctx = FakeContext()

    await worker.run_research(app, "monotonic", ctx)

    progresses = [progress for progress, _total in ctx.progress_calls]
    assert len(progresses) >= 2, "need multiple progress points to validate monotonicity"
    for prev, curr in zip(progresses, progresses[1:], strict=False):
        assert curr >= prev, f"progress regressed: {progresses}"


async def test_sources_entries_have_url_title_snippet(upjack_app):
    """After a completed run, ``entity.sources`` is non-empty and every entry
    exposes the UI contract: ``url``, ``title``, ``snippet``."""
    app, worker = upjack_app
    ctx = FakeContext()

    result = await worker.run_research(app, "sources", ctx)

    run = app.get_entity("research_run", result["run_id"])
    sources = run["sources"]
    assert len(sources) > 0
    for source in sources:
        assert "url" in source
        assert "title" in source
        assert "snippet" in source
