"""Research worker powered by GPT-Researcher.

Runs a real web-research workflow and streams progress to both the FastMCP
Context (engine-visible) and the Upjack entity (UI-visible). GPT-Researcher's
async log handler surfaces phase-like events which we map to monotonic
progress buckets.

## Four signals, deliberately separated

The worker emits four distinct signals into the entity. Each answers a
different question and updates on a different cadence — see the entity
schema for full descriptions:

  * ``progress``           — int 0..100, step function on phase transition
  * ``status_message``     — string, "what is it doing right now"
  * ``last_heartbeat_at``  — timestamp, "is it still alive" (heartbeats
                              every 5s while work is in flight, independent
                              of progress)
  * ``phase_history``      — append-only timeline, "where did time go"

Critical: liveness updates do NOT advance progress. During the writing
phase, progress legitimately holds at the writing-bucket value (70) for
the entire LLM generation; ``last_heartbeat_at`` keeps moving so the UI
can show "still working" without lying about completion percent.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

from gpt_researcher import GPTResearcher

from mcp_research._title import generate_title

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastmcp import Context
    from upjack.app import UpjackApp


# Phase → progress mapping. Rebalanced so the writing phase holds 70 for
# the duration of the LLM generation; the bar visibly stalls there but
# `last_heartbeat_at` ticks every 5s so the UI can show liveness honestly
# rather than fabricating fake progress. See worker docstring §"Four
# signals".
_PHASES: dict[str, int] = {
    "planning": 5,
    "searching": 20,
    "scraping": 40,
    "analyzing": 60,
    "writing": 70,
    "completed": 100,
}

# Hard wall-clock cap for conduct_research() + write_report() COMBINED.
# Single asyncio.wait_for around the entire inner block — not per-call —
# so total runtime cannot exceed this regardless of how time splits between
# the two phases. Matches the doc contract.
_TIMEOUT_SECONDS = 300

# Liveness heartbeat cadence. Five seconds keeps the staleness window
# small enough that the UI can usefully discriminate "live" / "stale" /
# "hung" with the thresholds it bakes in (10s / 30s).
_HEARTBEAT_PERIOD_S = 5.0


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# PhaseTracker — owns the discrete phase state machine + history.
#
# Calling `enter("writing")` is the only way phase changes. Records the
# transition with timestamps. No coupling to entity writes; pure data.
# Callers (worker, log handler) read `history` and pass it to
# `app.update_entity` alongside other fields.
# ---------------------------------------------------------------------------


class _PhaseTracker:
    """Append-only timeline of phase transitions.

    Each phase is recorded as ``{phase, started_at, ended_at}``. The
    currently-running phase has ``ended_at = None``. `enter` is idempotent
    on the same phase (no spurious transitions when the same event fires
    twice). `close` finalizes the last open phase — call on terminal exit
    so the history never has a dangling open phase after a run completes.
    """

    def __init__(self) -> None:
        self.history: list[dict[str, Any]] = []
        self.current: str | None = None

    def enter(self, phase: str) -> None:
        if self.current == phase:
            return
        now = _now()
        if self.history and self.history[-1].get("ended_at") is None:
            self.history[-1]["ended_at"] = now
        self.history.append({"phase": phase, "started_at": now, "ended_at": None})
        self.current = phase

    def close(self) -> None:
        if self.history and self.history[-1].get("ended_at") is None:
            self.history[-1]["ended_at"] = _now()


# ---------------------------------------------------------------------------
# Liveness heartbeat — separate channel from progress.
#
# Spawns a background task that, every _HEARTBEAT_PERIOD_S seconds, calls
# handler.touch_liveness(elapsed_s). That method updates last_heartbeat_at
# and refreshes status_message with an elapsed-time suffix — but never
# touches `progress`. So during the long write_report() call the bar
# legitimately sits at 70 while the user sees "Writing report · 47s"
# updating in real time.
#
# Using an asynccontextmanager keeps the cleanup obvious: the heartbeat
# task is cancelled on context exit regardless of how the inner block
# exits (success, exception, cancellation, timeout).
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _liveness(handler: _MCPLogHandler) -> AsyncIterator[None]:
    started = monotonic()
    stop = asyncio.Event()

    async def _tick() -> None:
        # First heartbeat fires after the period, not immediately — the
        # first phase transition typically happens within ~1s and already
        # writes last_heartbeat_at via _bump.
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_PERIOD_S)
                return  # stop was set
            except TimeoutError:
                elapsed = int(monotonic() - started)
                with suppress(Exception):
                    await handler.touch_liveness(elapsed)

    task = asyncio.create_task(_tick(), name="research-liveness-heartbeat")
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_research(
    app: UpjackApp,
    query: str,
    ctx: Context,
    *,
    title: str | None = None,
) -> dict:
    """Execute a GPT-Researcher run and stream state to app + ctx.

    Contract:
      1. Creates the ``research_run`` entity and returns its ``run_id``.
      2. Updates the entity on each phase transition (progress + message
         + phase_history) and on every liveness heartbeat
         (last_heartbeat_at + status_message elapsed suffix only).
      3. Calls ``ctx.report_progress(progress=..., total=100)`` on every
         phase transition, including the final 100%.
      4. On ``asyncio.CancelledError`` marks the entity ``cancelled`` and
         re-raises so FastMCP transitions the task terminal.
      5. On ``asyncio.TimeoutError`` marks the entity ``failed`` with an
         explicit timeout message and re-raises.
      6. On any other ``Exception`` marks the entity ``failed`` with the
         error message and re-raises.
      7. Returns ``{"run_id", "status": "completed", "report": <markdown>}``.

    Title handling: when ``title`` is supplied (e.g., the calling agent
    already composed one), it lands on the entity at creation. When
    omitted, a background task generates one via the FAST_LLM and patches
    the entity in-place — the research run never blocks on title
    generation. The UI sees the title arrive via ``useDataSync`` ~500ms
    after the entity surfaces and falls back to a truncated query in the
    meantime.
    """
    initial_fields: dict[str, Any] = {
        "query": query,
        "run_status": "working",
        "progress": 0,
        "status_message": "Queued",
        "started_at": _now(),
        "last_heartbeat_at": _now(),
        # Stamped here too so the UI's elapsed-time display has a
        # value to subtract against during the brief window between
        # entity creation and the first phase entry.
        "current_phase_started_at": _now(),
        "sources": [],
        "phase_history": [],
    }
    if title:
        initial_fields["title"] = title

    run = app.create_entity("research_run", initial_fields)
    run_id = run["id"]

    # Fire title generation in the background ONLY when the caller didn't
    # supply one. The task patches the entity when it lands; failure is
    # benign (UI falls back to a truncated query). Cancelled in every
    # terminal path below so a slow provider can't outlive the run.
    title_task: asyncio.Task[None] | None = None
    if not title:
        title_task = asyncio.create_task(
            _store_generated_title(app, run_id, query),
            name=f"research-title-{run_id}",
        )

    phases = _PhaseTracker()
    handler = _MCPLogHandler(app=app, ctx=ctx, run_id=run_id, phases=phases)

    try:
        researcher = GPTResearcher(
            query=query,
            report_type="research_report",
            log_handler=handler,
        )

        # Single wall-clock cap covers BOTH conduct_research and
        # write_report. The previous code wrapped each in its own
        # wait_for, which silently doubled the budget.
        async with _liveness(handler):
            report, sources = await asyncio.wait_for(
                _execute(handler, phases, researcher),
                timeout=_TIMEOUT_SECONDS,
            )

        phases.enter("completed")
        phases.close()
        app.update_entity(
            "research_run",
            run_id,
            {
                "run_status": "completed",
                "progress": 100,
                "status_message": "Completed",
                "report": report,
                "sources": sources,
                "phase_history": list(phases.history),
                "last_heartbeat_at": _now(),
                "completed_at": _now(),
                # Clear the elapsed-time clock on terminal — the UI
                # stops ticking once this is null, so the final view
                # doesn't read "completed · 47s · 51s · …".
                "current_phase_started_at": None,
            },
        )
        await ctx.report_progress(progress=100, total=100)
        await ctx.info(f"Research complete: {run_id}")

        # Wait briefly for the title to land so the completed entity
        # carries it on the same write the report arrives on. Cap is
        # tight — the run already completed; we don't want to add visible
        # latency just for a label.
        await _await_title_briefly(title_task)

        return {"run_id": run_id, "status": "completed", "report": report}

    except asyncio.CancelledError:
        _cancel_title_task(title_task)
        phases.close()
        app.update_entity(
            "research_run",
            run_id,
            {
                "run_status": "cancelled",
                "status_message": "Cancelled by client",
                "phase_history": list(phases.history),
                "last_heartbeat_at": _now(),
                "completed_at": _now(),
                "current_phase_started_at": None,
            },
        )
        raise

    except TimeoutError:
        _cancel_title_task(title_task)
        phases.close()
        app.update_entity(
            "research_run",
            run_id,
            {
                "run_status": "failed",
                "status_message": "Timed out after 5 min",
                "error_message": "Exceeded 300s wall-clock budget",
                "phase_history": list(phases.history),
                "last_heartbeat_at": _now(),
                "completed_at": _now(),
                "current_phase_started_at": None,
            },
        )
        raise

    except Exception as exc:
        _cancel_title_task(title_task)
        phases.close()
        app.update_entity(
            "research_run",
            run_id,
            {
                "run_status": "failed",
                "status_message": "Failed",
                "error_message": str(exc),
                "phase_history": list(phases.history),
                "last_heartbeat_at": _now(),
                "completed_at": _now(),
                "current_phase_started_at": None,
            },
        )
        raise


async def _store_generated_title(app: UpjackApp, run_id: str, query: str) -> None:
    """Background task body: generate a title and patch the entity.

    Exits cleanly on ``CancelledError`` (the worker cancels us on
    terminal exit). All other exceptions are absorbed inside
    ``generate_title`` — this wrapper just deals with the entity write
    itself, which can also fail (e.g., entity was deleted between
    creation and title arrival).
    """
    title = await generate_title(query)
    if not title:
        return
    try:
        app.update_entity("research_run", run_id, {"title": title})
    except Exception:  # noqa: BLE001
        # Entity gone (deleted) or transient store error — neither
        # warrants failing the research run, which has already returned.
        return


def _cancel_title_task(task: asyncio.Task[None] | None) -> None:
    """Cancel the title task if it's still pending. No-op if already done.

    Called from every terminal path in ``run_research``. We don't await
    the cancellation — the task either honours it or finishes on its own;
    either way the asyncio runtime cleans up after the parent returns.
    """
    if task is None or task.done():
        return
    task.cancel()


async def _await_title_briefly(task: asyncio.Task[None] | None) -> None:
    """Give the title task a short window to land before we return.

    The success path calls this so the final ``app.update_entity`` write
    is followed (likely) by a single small title patch rather than
    racing the caller's downstream logic. Cap is intentionally short —
    we never want a slow provider to stretch the visible run duration.
    Failures (timeout, exception, cancellation) are swallowed; the title
    is best-effort.
    """
    if task is None or task.done():
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
    except (TimeoutError, asyncio.CancelledError, Exception):
        return


async def _execute(
    handler: _MCPLogHandler,
    phases: _PhaseTracker,
    researcher: GPTResearcher,
) -> tuple[str, list[dict[str, str]]]:
    """Inner workflow. Pulled out so the outer ``run_research`` can wrap it
    in a single ``asyncio.wait_for`` and a single ``_liveness`` context."""
    phases.enter("planning")
    await handler.bump(_PHASES["planning"], "Starting research")

    await researcher.conduct_research()

    # Writing phase enters explicitly so the liveness suffix in
    # status_message reads "Writing report · Ns" rather than the last
    # event from conduct_research.
    phases.enter("writing")
    await handler.bump(_PHASES["writing"], "Writing report")
    report = await researcher.write_report()

    raw_sources = researcher.get_research_sources() or []
    if raw_sources:
        sources = [
            {
                "url": s.get("url", ""),
                "title": s.get("title", s.get("url", "")),
                "snippet": (s.get("content") or "")[:200],
            }
            for s in raw_sources
        ]
    else:
        sources = list(handler._sources)

    return report, sources


# ---------------------------------------------------------------------------
# _MCPLogHandler — bridges gpt-researcher events to the four-signal model.
# ---------------------------------------------------------------------------


class _MCPLogHandler:
    """Map gpt-researcher's async log events onto progress + phase + msg.

    The handler owns:
      * ``_last``           — monotonic progress floor (never regresses)
      * ``_last_message``   — base status_message *without* the elapsed
                              suffix; the heartbeat re-derives the suffix
                              each tick rather than mutating the base
      * ``_sources``        — accumulator for streamed source URLs

    Phase transitions go through the shared ``_PhaseTracker``; the handler
    is the only thing that calls ``phases.enter(...)`` from inside event
    callbacks (the worker's outer ``_execute`` calls it for boundaries
    that aren't event-driven).
    """

    def __init__(
        self,
        app: UpjackApp,
        ctx: Context,
        run_id: str,
        phases: _PhaseTracker,
    ) -> None:
        self._app = app
        self._ctx = ctx
        self._run_id = run_id
        self._phases = phases
        self._last = 0
        self._last_message = ""
        self._sources: list[dict[str, str]] = []

    # -- gpt-researcher callbacks ------------------------------------------

    async def on_research_step(self, step: str, details: dict[str, Any]) -> None:
        progress, msg, phase = self._classify(step)
        urls = details.get("urls") or details.get("source_urls") or []
        new_url_added = False
        for url in urls:
            if not any(s["url"] == url for s in self._sources):
                self._sources.append({"url": url, "title": url, "snippet": ""})
                new_url_added = True
        # Only write sources when the list actually changed — avoids
        # flooding the entity store on high-frequency events.
        if new_url_added:
            self._app.update_entity(
                "research_run",
                self._run_id,
                {"sources": list(self._sources)},
            )
        await self.bump(progress, msg, phase=phase)

    async def on_agent_action(self, action: str, **kwargs: Any) -> None:
        progress, msg, phase = self._classify(action)
        await self.bump(progress, msg, phase=phase)

    async def on_tool_start(self, tool_name: str, **kwargs: Any) -> None:
        # Map known gpt-researcher tools to user-friendly verbs; fall
        # back to a humanized form of the raw name. Avoids leaking
        # implementation details like "read_resource" into the UI.
        await self.bump(
            max(self._last, _PHASES["searching"]),
            _humanize_tool(tool_name),
            phase="searching",
        )

    # -- public surface used by run_research + _liveness ------------------

    async def bump(self, progress: int, message: str, *, phase: str | None = None) -> None:
        """Record a phase-transition signal: progress + message + phase
        history + ctx.report_progress + ctx.info, all together. Liveness
        is implicitly fresh after this (last_heartbeat_at also updated).

        On phase change, also stamps ``current_phase_started_at`` so the
        UI can derive elapsed-time client-side. The worker writes the
        timestamp ONCE per phase transition (a stable input); the UI
        renders ``now - current_phase_started_at`` every second (the
        smooth output). This decouples display cadence from heartbeat
        cadence — see worker docstring §"Four signals"."""
        phase_changed = bool(phase) and phase != self._phases.current
        if phase:
            self._phases.enter(phase)
        progress = max(progress, self._last)
        if progress == self._last and message == self._last_message and not phase_changed:
            return
        self._last = progress
        self._last_message = message
        update: dict[str, Any] = {
            "run_status": "working",
            "progress": progress,
            "status_message": message,
            "phase_history": list(self._phases.history),
            "last_heartbeat_at": _now(),
        }
        if phase_changed:
            # Reset the elapsed-time clock the UI is about to derive.
            update["current_phase_started_at"] = _now()
        self._app.update_entity("research_run", self._run_id, update)
        await self._ctx.report_progress(progress=progress, total=100)
        await self._ctx.info(message)

    async def touch_liveness(self, elapsed_seconds: int) -> None:
        """Pure liveness signal. Updates ``last_heartbeat_at`` and
        nothing else. The status_message stays put; the UI derives any
        elapsed-time display from ``current_phase_started_at`` on its
        own clock. Worker writes one stable timestamp; the UI animates
        the rest. Argument is unused here — kept in the signature so the
        ``_liveness`` context manager call site can pass it for future
        diagnostic logging."""
        del elapsed_seconds  # reserved for future use
        self._app.update_entity(
            "research_run",
            self._run_id,
            {"last_heartbeat_at": _now()},
        )

    # -- classification ---------------------------------------------------

    def _classify(self, label: str) -> tuple[int, str, str | None]:
        """Map a gpt-researcher label to (progress, message, phase).

        Architectural rules:

        1. **Token-based matching, not substring.** Earlier we used
           ``"search" in s`` style checks, which matched any string
           *containing* the substring. That mis-routed e.g.
           ``"conducting_research"`` (substring "search" is present
           inside "research") to the searching phase, then pinned
           progress at 20 and printed "Searching: conducting_research".
           Splitting the label into tokens (``_``/``-``/whitespace
           delimited) and checking word membership eliminates that
           class of false positive.

        2. **Return canonical messages, never raw labels.** Every
           branch returns a hand-written user-facing string. We do NOT
           interpolate ``label`` into the message — gpt-researcher's
           event names are internal identifiers and leak implementation
           details into the UI. The ``_humanize_label`` fallback in the
           default branch is the only place a derived form of the raw
           label surfaces, and it's snake-case-to-prose, not verbatim.

        3. **MUST NOT produce phase="completed".** The terminal
           "completed" phase is the worker's outer authority — only
           ``run_research`` enters it on successful return. Intra-run
           events like "search_completed" or "phase_complete" must NOT
           transition us to the terminal state (doing so pins progress
           at 100 via the monotonic floor — observed in production).
        """
        # Token-based matching. Split on every word-boundary character
        # we expect from gpt-researcher's labels (snake_case, kebab,
        # space). `tokens` is a set for cheap membership checks.
        tokens = {t for t in re.split(r"[_\-\s]+", str(label).lower()) if t}

        # Order matters: writing is checked first because it's the
        # phase whose label most often co-occurs with shorter words.
        if tokens & {"writing", "write"} or "generating report" in str(label).lower():
            return _PHASES["writing"], "Writing report", "writing"
        if tokens & {"analyzing", "analyze", "analysis", "summarize", "summarizing"}:
            return _PHASES["analyzing"], "Analyzing findings", "analyzing"
        if tokens & {"scraping", "scrape", "fetching", "fetch", "extract", "extracting"}:
            return _PHASES["scraping"], "Reading sources", "scraping"
        if tokens & {"search", "searching", "query", "querying"}:
            return _PHASES["searching"], "Searching the web", "searching"
        if tokens & {"plan", "planning"}:
            return _PHASES["planning"], "Planning research", "planning"
        # Anything else (including "X completed" / "X finished" intra-run
        # events, and umbrella labels like "conducting_research") holds
        # the current phase + progress. Humanize the label for display
        # so raw event names like "read_resource" don't leak into the UI.
        return self._last, _humanize_label(label), None


# ---------------------------------------------------------------------------
# Humanization helpers
#
# gpt-researcher's tool / event labels are internal identifiers like
# `read_resource`, `tavily_search`, `aweb_browse`. They're fine for logs
# but leaky in a user-facing status line. These helpers translate known
# names to verbs and snake_case anything unknown into prose. Mappings are
# intentionally small — gpt-researcher's vocabulary changes between
# versions, so the safety net is the snake_case fallback rather than an
# exhaustive table.
# ---------------------------------------------------------------------------

_HUMAN_TOOL_NAMES: dict[str, str] = {
    "tavily_search": "Searching the web",
    "tavily_extract": "Reading sources",
    "web_search": "Searching the web",
    "browse": "Reading source",
    "read_resource": "Reading source",
    "scrape": "Reading source",
    "duckduckgo": "Searching the web",
    "google": "Searching the web",
}


def _humanize_label(label: str | None) -> str:
    """Turn a raw event label into something readable.

    `read_resource` → `Read resource`. Empty or None → `Researching`.
    No fancy NLP — just snake-to-prose. Keeps unknowns legible without
    pretending we recognize them.
    """
    if not label:
        return "Researching"
    cleaned = str(label).replace("_", " ").strip()
    if not cleaned:
        return "Researching"
    return cleaned[0].upper() + cleaned[1:]


def _humanize_tool(tool_name: str) -> str:
    """Map a gpt-researcher tool name to a user-friendly verb. Unknown
    tools fall back to ``"Using {humanized}"`` so the verb still reads
    naturally without leaking the raw identifier."""
    if not tool_name:
        return "Working"
    if tool_name in _HUMAN_TOOL_NAMES:
        return _HUMAN_TOOL_NAMES[tool_name]
    return f"Using {_humanize_label(tool_name)}"
