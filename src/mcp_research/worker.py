"""Research worker powered by GPT-Researcher.

Runs a real web-research workflow and streams progress to both the FastMCP
Context (engine-visible) and the Upjack entity (UI-visible). GPT-Researcher's
async log handler surfaces phase-like events which we map to monotonic
progress buckets; the outer orchestration enforces a 5-minute wall-clock cap
and the standard status-transition contract.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gpt_researcher import GPTResearcher

if TYPE_CHECKING:
    from fastmcp import Context
    from upjack.app import UpjackApp


_PHASES: dict[str, int] = {
    "planning": 10,
    "searching": 30,
    "scraping": 55,
    "analyzing": 75,
    "writing": 85,
    "done": 100,
}


# Hard wall-clock cap for conduct_research() + write_report() per SPEC §8.2/§9.
_TIMEOUT_SECONDS = 300


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


async def run_research(app: UpjackApp, query: str, ctx: Context) -> dict:
    """Execute a GPT-Researcher run and stream state to app + ctx.

    Contract (SPEC §3):
      1. Creates the ``research_run`` entity and returns its ``run_id``.
      2. Updates the entity on each phase transition.
      3. Calls ``ctx.report_progress(progress=..., total=100)`` on every
         transition, including the final 100%.
      4. On ``asyncio.CancelledError`` marks the entity ``cancelled`` and
         re-raises so FastMCP transitions the task terminal.
      5. On ``asyncio.TimeoutError`` marks the entity ``failed`` with an
         explicit timeout message and re-raises.
      6. On any other ``Exception`` marks the entity ``failed`` with the
         error message and re-raises.
      7. Returns ``{"run_id", "status": "completed", "report": <markdown>}``.
    """
    run = app.create_entity(
        "research_run",
        {
            "query": query,
            "run_status": "working",
            "progress": 0,
            "status_message": "Queued",
            "started_at": _now(),
            "sources": [],
        },
    )
    run_id = run["id"]

    try:
        handler = _MCPLogHandler(app=app, ctx=ctx, run_id=run_id)
        researcher = GPTResearcher(
            query=query,
            report_type="research_report",
            log_handler=handler,
        )

        await handler._bump(5, "Starting research")
        await asyncio.wait_for(researcher.conduct_research(), timeout=_TIMEOUT_SECONDS)
        await handler._bump(85, "Writing report")
        report = await asyncio.wait_for(researcher.write_report(), timeout=_TIMEOUT_SECONDS)

        # Prefer the researcher's authoritative source list; fall back to the
        # streaming accumulator if the provider gave us nothing.
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

        app.update_entity(
            "research_run",
            run_id,
            {
                "run_status": "completed",
                "progress": 100,
                "status_message": "Completed",
                "report": report,
                "sources": sources,
                "completed_at": _now(),
            },
        )
        await ctx.report_progress(progress=100, total=100)
        await ctx.info(f"Research complete: {run_id}")

        return {"run_id": run_id, "status": "completed", "report": report}

    except asyncio.CancelledError:
        app.update_entity(
            "research_run",
            run_id,
            {
                "run_status": "cancelled",
                "status_message": "Cancelled by client",
                "completed_at": _now(),
            },
        )
        raise

    except TimeoutError:
        app.update_entity(
            "research_run",
            run_id,
            {
                "run_status": "failed",
                "status_message": "Timed out after 5 min",
                "error_message": "Exceeded 300s wall-clock budget",
                "completed_at": _now(),
            },
        )
        raise

    except Exception as exc:
        app.update_entity(
            "research_run",
            run_id,
            {
                "run_status": "failed",
                "status_message": "Failed",
                "error_message": str(exc),
                "completed_at": _now(),
            },
        )
        raise


class _MCPLogHandler:
    """Bridge GPT-Researcher's async log events to app + ctx updates.

    GPT-Researcher (``gpt_researcher/agent.py``) calls three async methods on
    the handler during a run:

      * ``on_research_step(step, details)``
      * ``on_agent_action(action, **kwargs)``
      * ``on_tool_start(tool_name, **kwargs)``

    We map the label strings to monotonic progress buckets. Matching is
    intentionally loose because the event strings aren't a stable API — if
    GPT-Researcher renames a phase we degrade gracefully (hold progress,
    keep streaming messages) instead of breaking.
    """

    def __init__(self, app: UpjackApp, ctx: Context, run_id: str) -> None:
        self._app = app
        self._ctx = ctx
        self._run_id = run_id
        self._last = 0
        self._sources: list[dict[str, str]] = []

    async def on_research_step(self, step: str, details: dict[str, Any]) -> None:
        progress, msg = self._classify(step)
        urls = details.get("urls") or details.get("source_urls") or []
        new_url_added = False
        for url in urls:
            if not any(s["url"] == url for s in self._sources):
                self._sources.append({"url": url, "title": url, "snippet": ""})
                new_url_added = True
        # Only write sources when the list actually changed — avoids flooding
        # the entity store on high-frequency events.
        if new_url_added:
            self._app.update_entity(
                "research_run",
                self._run_id,
                {"sources": list(self._sources)},
            )
        await self._bump(progress, msg)

    async def on_agent_action(self, action: str, **kwargs: Any) -> None:
        progress, msg = self._classify(action)
        await self._bump(progress, msg)

    async def on_tool_start(self, tool_name: str, **kwargs: Any) -> None:
        await self._bump(
            max(self._last, _PHASES["searching"]),
            f"Using {tool_name}",
        )

    def _classify(self, label: str) -> tuple[int, str]:
        s = str(label).lower()
        if "plan" in s:
            return _PHASES["planning"], "Planning research"
        if "search" in s or "query" in s:
            return _PHASES["searching"], f"Searching: {label}"
        if "scrap" in s or "fetch" in s or "extract" in s:
            return _PHASES["scraping"], "Reading sources"
        if "analyz" in s or "summar" in s:
            return _PHASES["analyzing"], "Analyzing findings"
        if "writ" in s or "report" in s:
            return _PHASES["writing"], "Writing report"
        return self._last, label or "Researching"

    async def _bump(self, progress: int, message: str) -> None:
        # Monotonic clamp — progress never regresses even if _classify picks a
        # lower bucket for a late-arriving event.
        progress = max(progress, self._last)
        if progress == self._last and not message:
            return
        self._last = progress
        self._app.update_entity(
            "research_run",
            self._run_id,
            {
                "run_status": "working",
                "progress": progress,
                "status_message": message,
            },
        )
        await self._ctx.report_progress(progress=progress, total=100)
        await self._ctx.info(message)
