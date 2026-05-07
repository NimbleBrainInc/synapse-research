"""UI retry-handler structural contract tests.

These are synchronous source-text assertions against ``ui/src/App.tsx``. The UI
is a static Vite single-file bundle with no test harness of its own, and we
want a regression guard that doesn't require wiring vitest + jsdom +
testing-library into a bundle-only project. Source-text checks are good enough
here: the only way to satisfy them is to actually use the task-aware SDK API,
and they fail loudly when someone reverts to the pre-migration blocking
``callTool`` or breaks the dual-channel contract.

Kept in its own file (not in ``test_spec_compliance.py``) so the module-level
``pytest.mark.asyncio`` there doesn't warn about these non-coroutine tests.
"""

from __future__ import annotations

from pathlib import Path


def _read_app_tsx() -> str:
    app_tsx = Path(__file__).resolve().parent.parent / "ui" / "src" / "App.tsx"
    assert app_tsx.exists(), f"expected ui/src/App.tsx at {app_tsx}"
    return app_tsx.read_text()


def test_ui_retry_uses_call_tool_as_task() -> None:
    """UI retry handler MUST invoke ``useCallToolAsTask`` (or
    ``callToolAsTask``) against ``start_research``. If someone reverts to
    ``synapse.callTool('start_research', ...)`` for the retry path the UI
    will freeze for minutes waiting for the task to reach terminal — this
    guards against that regression."""
    src = _read_app_tsx()

    # Must import the task-aware hook from the SDK.
    assert "useCallToolAsTask" in src, (
        "ui/src/App.tsx must import useCallToolAsTask from "
        "@nimblebrain/synapse/react for the retry flow"
    )

    # Must actually invoke it with the start_research tool name. The hook
    # call may carry TS generics (`useCallToolAsTask<...>(...)`) so match
    # on the call token and the tool name as separate-but-required signals.
    import re

    # `useCallToolAsTask` followed (optionally) by `<...>` then `(` — i.e. a
    # real call site, not just an import.
    call_site_pattern = re.compile(r"useCallToolAsTask\s*(?:<[^>(]*>)?\s*\(")
    assert call_site_pattern.search(src), (
        "useCallToolAsTask must appear as a call expression (e.g. "
        "`useCallToolAsTask(...)` or `useCallToolAsTask<T, U>(...)`) — just "
        "importing it is not enough"
    )
    assert '"start_research"' in src, (
        'The literal "start_research" must appear somewhere in App.tsx — '
        "if the retry flow uses useCallToolAsTask it will be the tool-name "
        "argument to that hook"
    )


def test_ui_retry_has_legacy_host_fallback() -> None:
    """The retry flow must gracefully fall back to plain ``callTool`` when
    the host doesn't advertise the tasks capability. ``callToolAsTask``
    throws with a message mentioning ``tasks.requests.tools.call`` — App.tsx
    catches that and re-routes through ``synapse.callTool``. Verify the
    fallback path still exists so a future refactor doesn't silently drop
    support for older platform deploys."""
    src = _read_app_tsx()
    assert "tasks.requests.tools.call" in src, (
        "App.tsx should detect the 'host missing tasks capability' error by "
        "its spec-mandated message (contains 'tasks.requests.tools.call') "
        "and fall back to synapse.callTool"
    )
    assert "synapse.callTool" in src or "synapse\n    .callTool" in src, (
        "App.tsx must retain a synapse.callTool fallback path for legacy hosts"
    )


def test_ui_retry_navigates_via_entity_not_task_result() -> None:
    """The new run_id is delivered via the entity channel (``useDataSync``),
    not by awaiting ``task.result()``. Navigation to the new run's detail
    page MUST be driven by entity-list diff, not by the task handle's
    terminal payload — because the task takes minutes to complete and the
    run_id is already known via the entity after ~100ms.

    Guard against a future refactor that awaits the handle's result and
    then reads ``result.run_id``: that works functionally but defeats the
    whole point of task augmentation (fast navigation)."""
    src = _read_app_tsx()
    # Negative assertion: DetailView must not await its onRetry prop. A
    # regression to the old blocking retry would naturally reintroduce
    # ``await onRetry(...)``.
    assert "await onRetry" not in src, (
        "DetailView must not await onRetry — awaiting the task's terminal "
        "result defeats the dual-channel contract (minutes of UI hang)."
    )


# ---------------------------------------------------------------------------
# Four-signal model — UI side
#
# The worker emits four distinct signals (progress, phase+message,
# liveness, phase_history). The UI honors that separation by rendering
# each via its own affordance. These tests guard the structural pieces;
# behavioral coverage waits on a real DOM harness (vitest + jsdom).
# ---------------------------------------------------------------------------


def test_ui_declares_liveness_type() -> None:
    """The UI's ResearchRun shape must declare ``last_heartbeat_at`` so
    consumers can read it without `as any` casts. ``phase_history`` is
    intentionally NOT required in the UI types — phase timing is recorded
    in the entity for diagnostics but isn't rendered (a bar visualization
    of phase durations conflated 'is this complete' with 'how long did
    it take'; we removed it). If it ever comes back, this test should be
    updated then, not before."""
    src = _read_app_tsx()
    assert "last_heartbeat_at" in src, (
        "ResearchRun must declare last_heartbeat_at — without it the UI can't compute liveness"
    )


def test_ui_has_compute_liveness_helper() -> None:
    """The architectural separation between progress and liveness only
    holds if liveness is computed somewhere. Verify the helper exists and
    distinguishes 'live' / 'stale' / 'hung' states."""
    src = _read_app_tsx()
    assert "function computeLiveness" in src, (
        "computeLiveness helper must exist — it's the bridge between "
        "last_heartbeat_at and the UI's stale/hung indicators"
    )
    # All three states must be reachable from the helper or its callers,
    # otherwise the staleness UI can't render its tiered messaging.
    for state in ('"live"', '"stale"', '"hung"'):
        assert state in src, (
            f"liveness state {state} must appear in App.tsx — without all "
            "three the staleness indicator can't tier from 'last signal' "
            "to 'no signal'"
        )


def test_ui_staleness_line_renders_only_when_not_live() -> None:
    """The staleness line is itself a diagnostic — its absence on a
    healthy run is the signal. Guard against a future change that
    accidentally renders it always (which would produce constant
    'last signal 0s ago' visual noise)."""
    src = _read_app_tsx()
    # The render condition must include a check that liveness is NOT
    # "live". Two acceptable forms: `liveness !== "live"` or
    # `liveness === "stale" || liveness === "hung"`. We accept either.
    assert 'liveness !== "live"' in src or (
        'liveness === "stale"' in src and 'liveness === "hung"' in src
    ), "staleness line must be gated on liveness != 'live' so a healthy run shows nothing here"
    # And the message strings differ between stale and hung — a single
    # message would lose the tiered diagnostic value.
    assert "Last signal" in src, "stale-state message 'Last signal Ns ago' must be present"
    assert "may be stalled" in src or "No signal for" in src, (
        "hung-state message must be distinguishable from the stale message"
    )


def test_ui_use_tick_gated_on_working_status() -> None:
    """``useTick`` is the sub-poll re-render hook. It MUST be gated on
    ``run_status === 'working'`` so terminal runs don't churn — and so
    the staleness counter / running-phase bar legitimately freeze on
    completion."""
    src = _read_app_tsx()
    assert "function useTick" in src, "useTick hook must exist"
    # The hook's `active` flag at every call site must reference the
    # working status — otherwise it ticks forever and burns cycles on
    # terminal runs.
    assert 'useTick(run.run_status === "working"' in src, (
        'useTick must be invoked with `run.run_status === "working"` as '
        "its `active` flag — anything else risks ticking on terminal runs"
    )
