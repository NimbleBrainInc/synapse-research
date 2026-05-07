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


async def test_phase_history_records_transitions_with_timestamps(upjack_app):
    """The entity's ``phase_history`` records each phase with non-null
    ``started_at`` and (after the run completes) non-null ``ended_at``.
    The final phase is ``completed``. Phases appear in workflow order."""
    app, worker = upjack_app
    ctx = FakeContext()

    result = await worker.run_research(app, "history check", ctx)

    run = app.get_entity("research_run", result["run_id"])
    history = run.get("phase_history") or []
    assert len(history) >= 2, f"expected ≥2 phase records, got {history}"

    # Every entry has both timestamps once the run is terminal.
    for entry in history:
        assert entry.get("started_at"), f"missing started_at: {entry}"
        assert entry.get("ended_at"), f"missing ended_at after completion: {entry}"
        # started_at must be before-or-equal-to ended_at — string compare is
        # safe because both are ISO 8601 with the same TZ suffix.
        assert entry["started_at"] <= entry["ended_at"], f"phase ends before it starts: {entry}"

    # Final phase is "completed" — the worker enters it after the inner
    # workflow returns successfully.
    assert history[-1]["phase"] == "completed", (
        f"final phase should be 'completed', got {history[-1]['phase']}"
    )

    # Phases are unique within the history (no spurious re-entries from
    # event repetition) — _PhaseTracker.enter is idempotent on the same
    # phase. We don't enforce strict order beyond "completed is last"
    # because gpt-researcher may emit events out of the canonical order.
    phase_names = [entry["phase"] for entry in history]
    assert len(set(phase_names)) == len(phase_names), f"phase appears more than once: {phase_names}"


async def test_liveness_ticks_during_long_write_without_advancing_progress(
    upjack_app, fake_researcher, monkeypatch
):
    """During a slow ``write_report``, the heartbeat MUST update
    ``last_heartbeat_at`` without bumping ``progress``. This is the
    architectural invariant: liveness is a separate signal from progress.

    Patches ``_HEARTBEAT_PERIOD_S`` low so the test runs in <2s instead of
    waiting for the production 5s cadence.
    """
    app, worker = upjack_app

    monkeypatch.setattr(worker, "_HEARTBEAT_PERIOD_S", 0.1)

    async def slow_write(self):
        # Long enough to span ≥5 heartbeat ticks at the patched period.
        await asyncio.sleep(0.6)
        return f"# {self.query}\n\nSlow report."

    fake_researcher.write_report = slow_write

    ctx = FakeContext()

    # Take a snapshot of progress while the run is in flight, mid-write.
    async def _probe():
        await asyncio.sleep(0.3)  # let writing start; heartbeats accumulate
        runs = app.list_entities("research_run", status="active", limit=10)
        return runs[0] if runs else None

    probe_task = asyncio.create_task(_probe())
    result_task = asyncio.create_task(worker.run_research(app, "slow write", ctx))
    mid_run = await probe_task
    await result_task

    assert mid_run is not None, "entity should exist mid-run"
    # (1) Liveness fresh — the heartbeat is the only signal proving the
    # bundle is still alive while writing is in flight.
    assert mid_run.get("last_heartbeat_at"), "heartbeat must be set"
    assert mid_run["last_heartbeat_at"] > mid_run["started_at"], (
        "heartbeat should advance past run start during write"
    )
    # (2) Progress stable at the writing bucket. Liveness is a separate
    # signal from progress; ticking the heartbeat must not bump it.
    assert mid_run["progress"] == worker._PHASES["writing"], (
        f"liveness must not advance progress; expected progress={worker._PHASES['writing']} "
        f"during writing phase, got {mid_run['progress']}"
    )
    # (3) status_message is the clean phase label — NO " · Ns" suffix.
    # The UI computes elapsed time itself from current_phase_started_at;
    # if the worker is appending Ns to status_message that's a regression
    # to the old display-cadence-coupled-to-write-cadence pattern.
    assert mid_run["status_message"] == "Writing report", (
        "status_message must be the stable phase label without an elapsed "
        f"suffix. Got {mid_run['status_message']!r}"
    )
    # (4) current_phase_started_at exists and is older than the latest
    # heartbeat — proves heartbeats are NOT re-stamping the phase clock.
    assert mid_run.get("current_phase_started_at"), (
        "current_phase_started_at must be set so the UI can derive elapsed time"
    )
    assert mid_run["current_phase_started_at"] < mid_run["last_heartbeat_at"], (
        "current_phase_started_at must be older than last_heartbeat_at — "
        "heartbeats must NOT re-stamp the phase clock"
    )


async def test_single_wall_clock_cap_caps_total_runtime(upjack_app, fake_researcher, monkeypatch):
    """The 300s cap covers conduct_research + write_report COMBINED, not
    each separately. We patch the cap low and split the budget across
    both calls so neither alone exceeds it but their sum does."""
    app, worker = upjack_app

    monkeypatch.setattr(worker, "_TIMEOUT_SECONDS", 0.3)

    async def slow_research(self):
        await asyncio.sleep(0.2)

    async def slow_write(self):
        await asyncio.sleep(0.2)
        return "should not arrive"

    fake_researcher.conduct_research = slow_research
    fake_researcher.write_report = slow_write

    ctx = FakeContext()
    with pytest.raises(TimeoutError):
        await worker.run_research(app, "double-budget check", ctx)

    runs = app.list_entities("research_run", status="active", limit=10)
    assert runs and runs[0]["run_status"] == "failed"
    assert "300s" in (runs[0].get("error_message") or "") or "wall-clock" in (
        runs[0].get("error_message") or ""
    )


async def test_classify_intra_run_completed_events_do_not_transition_phase(upjack_app):
    """Regression for a production bug: gpt-researcher emits intra-run
    events like "search_completed" / "phase_complete" / "research_finished"
    as each phase wraps. A naive ``_classify`` matched any string
    containing "complet" / "finish" and routed it to the terminal
    ``completed`` phase — pinning progress at 100 via the monotonic floor
    and producing a 0-duration "completed" entry mid-timeline (between
    e.g. "searching" and "writing").

    Architectural rule under test: the terminal "completed" phase is the
    worker's outer authority. Intra-run events that *mention* completion
    must NOT transition us to "completed" or advance progress; they
    should hold the current phase. ``run_research`` is the only caller
    that should ever enter "completed", and it does so on successful
    return.
    """
    app, worker = upjack_app

    from mcp_research.worker import _MCPLogHandler, _PhaseTracker

    phases = _PhaseTracker()
    handler = _MCPLogHandler(
        app=app,
        ctx=FakeContext(),
        run_id="rr_test",
        phases=phases,
    )
    # Establish a non-zero current state so we can prove the classifier
    # holds it rather than trampling.
    handler._last = worker._PHASES["searching"]

    # Each label is a real-ish event string that mentions completion. None
    # of them should classify as the terminal "completed" phase or advance
    # progress to 100 — those are reserved for the worker's outer success
    # path. Phase-internal classification (e.g. "scraping_completed" still
    # routing to the scraping phase, since the label also contains "scrap")
    # is fine; the invariant is just "no terminal jump".
    real_intra_run_events = [
        "search_completed",
        "phase_complete",
        "research_finished",
        "task completed",
        "log_completed_steps",
        "scraping_completed",
    ]
    for label in real_intra_run_events:
        progress, _msg, phase = handler._classify(label)
        assert phase != "completed", (
            f"intra-run event {label!r} must NOT classify as terminal "
            f"'completed' phase — that pins progress at 100 mid-run. "
            f"Got phase={phase!r}"
        )
        assert progress < worker._PHASES["completed"], (
            f"intra-run event {label!r} must NOT advance progress to "
            f"the terminal value ({worker._PHASES['completed']}); got {progress}"
        )


async def test_wire_safe_task_store_strips_null_fields_from_dump(upjack_app):
    """Regression for an interop bug between the MCP Python SDK
    (Pydantic-based, emits ``None`` as ``null``) and the MCP TypeScript
    SDK (Zod-based, ``optional()`` means absent/undefined and rejects
    ``null``). A CallToolResult containing a TextContent with
    ``annotations=None`` and ``_meta=None`` would serialize as
    ``{"annotations":null,"_meta":null}`` and the platform's Zod
    rejected it with ``-32603 invalid_union``.

    The fix is centralized at the task-store boundary: ``WireSafeTaskStore``
    overrides ``store_result`` to round-trip through
    ``model_dump(exclude_none=True)``. This test asserts that invariant
    directly — store a result with explicit ``None`` fields, retrieve
    it, dump it, and confirm the dump has NO null values for the
    spec-optional fields.
    """
    from mcp.types import CallToolResult, TextContent

    from mcp_research._tasks import WireSafeTaskStore

    store = WireSafeTaskStore()
    task = await store.create_task(
        metadata=__import__("mcp.types", fromlist=["TaskMetadata"]).TaskMetadata(ttl=1000)
    )

    # Construct a CallToolResult WITH the offending None fields. This
    # is the exact shape FastMCP produces when a tool returns a dict —
    # TextContent's `annotations` and `_meta` default to None.
    dirty = CallToolResult(
        content=[TextContent(type="text", text="hello")],
        isError=False,
    )

    await store.store_result(task.taskId, dirty)
    retrieved = await store.get_result(task.taskId)
    assert retrieved is not None

    # The store's invariant: this dump MUST NOT contain nulls in the
    # spec-optional fields. If a future change reverts to plain
    # InMemoryTaskStore, this test fires.
    dumped = retrieved.model_dump(by_alias=True)
    content_block = dumped["content"][0]

    for offending_key in ("annotations", "_meta"):
        if offending_key in content_block:
            assert content_block[offending_key] is not None, (
                f"WireSafeTaskStore must drop None values from wire JSON. "
                f"Got content[0].{offending_key}=null which the TS SDK Zod "
                f"validator rejects. Full dump: {dumped!r}"
            )


async def test_classify_uses_token_matching_not_substring(upjack_app):
    """Regression for a production bug: classifier did substring matching
    (``"search" in s``) which mis-routed labels like ``conducting_research``
    to the searching phase — because "research" contains "search" as a
    substring. The fix is token-based matching: split the label on
    ``_``/``-``/whitespace and check word membership.

    Also asserts that the classifier returns CANONICAL user-facing
    messages, never interpolated raw labels. Earlier the searching
    branch returned ``f"Searching: {label}"``, leaking the snake_case
    event name into the UI ("searching: conducting_research · 4s · 20%"
    in production).
    """
    app, worker = upjack_app

    from mcp_research.worker import _MCPLogHandler, _PhaseTracker

    handler = _MCPLogHandler(
        app=app,
        ctx=FakeContext(),
        run_id="rr_test",
        phases=_PhaseTracker(),
    )

    # The exact label the user saw in production. MUST NOT route to
    # searching just because "research" contains "search".
    progress, msg, phase = handler._classify("conducting_research")
    assert phase != "searching", (
        f"'conducting_research' must not route to the searching phase via "
        f"substring match. Got phase={phase!r}"
    )
    # Falls through to the default branch — humanized label, no phase
    # transition.
    assert msg == "Conducting research", f"Got msg={msg!r}"

    # Genuine search-related labels still match correctly.
    for label in ["tavily_search", "web_search", "search_query", "query"]:
        progress, msg, phase = handler._classify(label)
        assert phase == "searching", (
            f"label {label!r} should route to searching, got phase={phase!r}"
        )
        # Canonical message — no raw label interpolated.
        assert msg == "Searching the web", (
            f"searching branch must return a canonical message, not interpolate "
            f"the raw label. Got msg={msg!r}"
        )
        assert label not in msg, f"raw label leaked into UI message: {msg!r}"

    # Other phases also return canonical messages (no leakage).
    for label, expected_phase, expected_msg in [
        ("writing_report", "writing", "Writing report"),
        ("analyzing_findings", "analyzing", "Analyzing findings"),
        ("scraping_url", "scraping", "Reading sources"),
        ("planning_steps", "planning", "Planning research"),
    ]:
        _, msg, phase = handler._classify(label)
        assert phase == expected_phase, f"label {label!r} → phase {phase!r}"
        assert msg == expected_msg, f"label {label!r} → msg {msg!r}"


async def test_status_message_humanizes_raw_event_labels(upjack_app):
    """Raw gpt-researcher labels like ``read_resource`` must NOT surface
    verbatim in user-facing status messages. We don't want implementation
    details leaking into the UI; the humanizer translates known tool
    names to verbs and snake-cases unknowns into prose. This catches a
    regression where someone removes ``_humanize_label`` / ``_humanize_tool``
    and the raw labels start showing up again.
    """
    from mcp_research.worker import _humanize_label, _humanize_tool

    # Known tool → friendly verb.
    assert _humanize_tool("read_resource") == "Reading source"
    assert _humanize_tool("tavily_search") == "Searching the web"

    # Unknown tool → "Using {humanized}", not raw identifier.
    msg = _humanize_tool("some_new_tool")
    assert "some_new_tool" not in msg, f"raw tool name leaked into UI: {msg!r}"
    assert msg == "Using Some new tool"

    # Bare snake_case label → prose.
    assert _humanize_label("read_resource") == "Read resource"
    assert _humanize_label("phase_complete") == "Phase complete"

    # Empty / None → safe fallback.
    assert _humanize_label("") == "Researching"
    assert _humanize_label(None) == "Researching"


async def test_classify_does_not_match_completed_report_to_writing(upjack_app):
    """Earlier specific bug: ``"report"`` alone matched both "writing
    report" and "completed report", pinning progress at the writing
    bucket. With the architectural fix above, "completed report" no
    longer transitions to a terminal phase — it falls through to
    "hold current phase". "writing report" still classifies as writing.
    """
    app, worker = upjack_app

    from mcp_research.worker import _MCPLogHandler, _PhaseTracker

    handler = _MCPLogHandler(
        app=app,
        ctx=FakeContext(),
        run_id="rr_test",
        phases=_PhaseTracker(),
    )

    # "completed report" no longer pulls us to terminal — it falls
    # through to "no transition".
    progress, _msg, phase = handler._classify("completed report")
    assert phase != "completed"
    assert progress < worker._PHASES["completed"]

    # "writing report" still routes to the writing phase.
    progress, _msg, phase = handler._classify("writing report")
    assert phase == "writing"
    assert progress == worker._PHASES["writing"]


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


# ---------------------------------------------------------------------------
# Title generation — entity-side contract
#
# The worker fires title generation in the background after creating the
# entity. The research run never blocks on it, but the title MUST land on
# the entity before run_research returns on the success path (we wait
# briefly for it). Tests monkeypatch ``generate_title`` so nothing here
# touches a real Anthropic key — matching the keyless test policy in
# CLAUDE.md.
# ---------------------------------------------------------------------------


async def test_generated_title_lands_on_entity_after_successful_run(upjack_app, monkeypatch):
    """Background title task patches ``title`` on the entity. After a
    successful run returns, the entity carries the generated title — the
    success path waits briefly for the task before returning."""
    app, worker = upjack_app

    async def fake_generate(query: str) -> str:
        return "Test Title For Entity"

    monkeypatch.setattr(worker, "generate_title", fake_generate)

    ctx = FakeContext()
    result = await worker.run_research(app, "some query", ctx)

    run = app.get_entity("research_run", result["run_id"])
    assert run.get("title") == "Test Title For Entity"
    # The query is unchanged — title is additive, not a replacement.
    assert run["query"] == "some query"


async def test_explicit_title_param_skips_generation(upjack_app, monkeypatch):
    """When the caller supplies ``title``, the worker MUST NOT call
    ``generate_title``. This is the cheap path for agents that already
    know the topic."""
    app, worker = upjack_app

    called = False

    async def should_not_be_called(query: str) -> str:
        nonlocal called
        called = True
        return "should not appear"

    monkeypatch.setattr(worker, "generate_title", should_not_be_called)

    ctx = FakeContext()
    result = await worker.run_research(app, "with explicit title", ctx, title="Caller Provided")

    run = app.get_entity("research_run", result["run_id"])
    assert run.get("title") == "Caller Provided"
    assert not called, "generate_title must not be called when title is supplied"


async def test_title_generation_failure_does_not_fail_run(upjack_app, monkeypatch):
    """A None return from ``generate_title`` (its documented failure
    mode) leaves ``title`` null on the entity but MUST NOT affect the
    run's success or any other field. The UI falls back to a truncated
    query in this state."""
    app, worker = upjack_app

    async def returns_none(query: str) -> None:
        return None

    monkeypatch.setattr(worker, "generate_title", returns_none)

    ctx = FakeContext()
    result = await worker.run_research(app, "title fails", ctx)

    assert result["status"] == "completed"
    run = app.get_entity("research_run", result["run_id"])
    assert run["run_status"] == "completed"
    assert run.get("title") is None
    # Run still finished cleanly otherwise.
    assert run["progress"] == 100
    assert run["report"]


async def test_title_generation_exception_does_not_fail_run(upjack_app, monkeypatch):
    """Even if ``generate_title`` raises (it shouldn't, but defense in
    depth), the run still completes successfully. The title task body
    swallows store-write errors too — neither failure should propagate."""
    app, worker = upjack_app

    async def raises(query: str) -> str:
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(worker, "generate_title", raises)

    ctx = FakeContext()
    result = await worker.run_research(app, "title raises", ctx)

    assert result["status"] == "completed"
    run = app.get_entity("research_run", result["run_id"])
    assert run["run_status"] == "completed"
    assert run.get("title") is None


async def test_title_task_cancelled_when_run_is_cancelled(upjack_app, fake_researcher, monkeypatch):
    """A cancelled run propagates cancellation to the in-flight title
    task. The task should never outlive its parent run; otherwise a slow
    provider could keep an event-loop reference alive after the FastMCP
    task is terminal."""
    app, worker = upjack_app

    title_started = asyncio.Event()
    title_was_cancelled = asyncio.Event()

    async def slow_title(query: str) -> str:
        title_started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            title_was_cancelled.set()
            raise
        return "should never arrive"

    monkeypatch.setattr(worker, "generate_title", slow_title)

    async def cancel_after_title_starts(self):
        # Wait until the title task is actually running so the cancel
        # path exercises the cancel-the-title-task branch.
        await title_started.wait()
        raise asyncio.CancelledError

    fake_researcher.conduct_research = cancel_after_title_starts

    ctx = FakeContext()
    with pytest.raises(asyncio.CancelledError):
        await worker.run_research(app, "cancel mid title", ctx)

    # Wait for the cancellation signal to reach the title task. One
    # event-loop yield isn't always enough — the cancel is queued, the
    # task body has to hit its next await, and the except branch has to
    # run. 1s is generous for that.
    await asyncio.wait_for(title_was_cancelled.wait(), timeout=1.0)


async def test_title_module_handles_missing_api_key(monkeypatch):
    """``generate_title`` returns None (not raises) when no API key is
    configured. This is the local-dev path and the keyless test path —
    both must work."""
    from mcp_research import _title

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = await _title.generate_title("some query")
    assert result is None


async def test_title_module_cleans_quoted_output(monkeypatch):
    """The model occasionally wraps output in quotes or appends a
    period; ``_clean`` strips both. Hardcoded against the documented
    contract — if the cleaning rules change, the test should fail and
    force an explicit decision."""
    from mcp_research._title import _clean

    assert _clean('"Quoted Title"') == "Quoted Title"
    assert _clean("Title With Period.") == "Title With Period"
    assert _clean("  whitespace title  ") == "whitespace title"
    assert _clean("") is None
    assert _clean('""') is None
    # Question marks survive — they can be intentional in titles.
    assert _clean("Is This A Title?") == "Is This A Title?"
