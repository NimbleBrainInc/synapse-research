"""Research MCP server — FastMCP + Upjack, with a task-augmented start_research tool.

Data root resolution (in priority order):
  1. UPJACK_ROOT env var
  2. MPAK_WORKSPACE env var
  3. ~/.synapse-research (fallback)

Each workspace runs its own server process with its own root. Tasks and entities
are isolated to that root; there is no cross-workspace state.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastmcp import Context
from fastmcp.server.tasks import TaskConfig
from upjack.app import UpjackApp
from upjack.server import create_server

from mcp_research.worker import run_research

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_MANIFEST_PATH = _PROJECT_ROOT / "manifest.json"
_UI_HTML = _PROJECT_ROOT / "ui" / "dist" / "index.html"


def _resolve_root() -> str:
    root = os.environ.get("UPJACK_ROOT") or os.environ.get("MPAK_WORKSPACE")
    if root:
        return root
    return str(Path.home() / ".synapse-research")


_WORKSPACE_ROOT = _resolve_root()

mcp = create_server(_MANIFEST_PATH, root=_WORKSPACE_ROOT)
_app = UpjackApp.from_manifest(_MANIFEST_PATH, root=_WORKSPACE_ROOT)


def _reap_orphaned_runs() -> None:
    """Mark any research_run entities stuck in 'working' as failed.

    A subprocess death mid-run (platform restart, OOM, etc.) leaves entities
    with run_status='working' that will never transition. Rather than let
    them linger indefinitely — confusing the UI and the agent — flip each to
    'failed' with a clear message on server start. Users can then retry
    cleanly, and the entity history shows the exact reason.
    """
    try:
        runs = _app.list_entities("research_run", status="active", limit=500)
    except Exception as exc:  # pragma: no cover - defensive; don't crash startup
        print(f"[synapse-research] reaper: failed to list entities: {exc}", file=sys.stderr)
        return

    reaped = 0
    for run in runs:
        if run.get("run_status") != "working":
            continue
        try:
            _app.update_entity(
                "research_run",
                run["id"],
                {
                    "run_status": "failed",
                    "status_message": "Server restarted mid-run",
                    "error_message": (
                        "This research run was interrupted by a server restart "
                        "and cannot resume. Start a new run to retry."
                    ),
                },
            )
            reaped += 1
        except Exception as exc:  # pragma: no cover
            print(
                f"[synapse-research] reaper: failed to update {run.get('id')}: {exc}",
                file=sys.stderr,
            )

    if reaped:
        print(
            f"[synapse-research] reaper: marked {reaped} orphaned run(s) as failed",
            file=sys.stderr,
        )


_reap_orphaned_runs()

mcp._mcp_server.instructions = (
    (mcp.instructions or "") + "\n\nResearch Runs:\n"
    "- Research is a long-running operation (~60s–3min in production).\n"
    "- ALWAYS invoke start_research with MCP task augmentation. "
    "The engine will handle polling and deliver the final report when complete.\n"
    "- The tool creates a research_run entity and updates its progress in real time. "
    "The UI watches the entity store and displays live status.\n"
    "- Render the returned markdown report directly to the user."
)


@mcp.tool(
    task=TaskConfig(mode="optional"),
    name="start_research",
    description=(
        "Run a research task on the given query. Supports MCP task augmentation — "
        "clients that advertise `tasks.requests.tools.call` may wrap the request with "
        "a `task` field to receive a CreateTaskResult and poll via tasks/get; clients "
        "that do not will block until the research completes and receive the full "
        "report inline. Either way, the server creates a `research_run` entity and "
        "updates its progress in real time so the Synapse UI can render live status. "
        "The worker typically takes ~60 seconds to 3 minutes and returns a markdown report."
    ),
)
async def start_research(query: str, ctx: Context) -> dict:
    """Kick off a research run.

    Args:
        query: The research query or topic.
        ctx: FastMCP Context — used for progress and structured logging.

    Returns:
        A dict with `run_id`, `status`, and `report` (markdown). On cancellation or
        failure, the underlying asyncio exception propagates and FastMCP marks the
        task terminal.
    """
    return await run_research(app=_app, query=query, ctx=ctx)


@mcp.resource("ui://research/main")
def research_ui() -> str:
    """UI resource rendered in the platform sidebar."""
    if _UI_HTML.exists():
        return _UI_HTML.read_text()
    return (
        "<html><body style='font-family:system-ui;padding:2rem'>"
        "<h2>Research</h2>"
        "<p>UI not built. Run <code>cd ui &amp;&amp; npm install &amp;&amp; npm run build</code>.</p>"
        "</body></html>"
    )


app = mcp.http_app()


if __name__ == "__main__":
    print(
        f"synapse-research starting (stdio); workspace root: {_WORKSPACE_ROOT}",
        file=sys.stderr,
    )
    mcp.run()
