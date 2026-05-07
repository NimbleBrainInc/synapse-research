"""In-process MCP tasks-utility helper for FastMCP bundles.

Implements the bundle-side of the MCP 2025-11-25 tasks utility natively in
the server process — no Redis, no Docket, no shared infrastructure. Composes
the mcp Python SDK's own primitives (`InMemoryTaskStore`,
`server.experimental.enable_tasks`) and adds a thin layer for FastMCP tool
integration.

Why this exists: FastMCP 3.x's built-in `@mcp.tool(task=TaskConfig(...))`
path routes through `fastmcp.server.tasks.handlers.submit_to_docket`, which
requires Redis. Our tenant pods enforce namespace isolation and don't expose
Redis. See `.tasks/task-aware-tools/PLATFORM_RELAY_VERIFIED.md` and
`.tasks/task-aware-tools/011-bundle-task-helper.md`.

Bundle authors use it like this:

    from fastmcp import FastMCP, Context
    from mcp_research._tasks import enable_in_memory_tasks, task_aware

    mcp = FastMCP("research")
    enable_in_memory_tasks(mcp)

    @task_aware(mode="optional")
    @mcp.tool(name="start_research", description="...")
    async def start_research(query: str, ctx: Context) -> dict:
        ...

This module is intentionally private to synapse-research for v1. Once a
second consumer needs it, extract to a shared package (likely
`packages/mcp-tasks-py` or fold into `upjack`).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TypeVar

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.shared.experimental.tasks.in_memory_task_store import InMemoryTaskStore
from mcp.types import (
    METHOD_NOT_FOUND,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    CallToolRequest,
    CallToolResult,
    CreateTaskResult,
    ErrorData,
    ListToolsRequest,
    ListToolsResult,
    ServerNotification,
    ServerResult,
    TaskExecutionMode,
    TaskMetadata,
    TaskStatus,
    TaskStatusNotification,
    TaskStatusNotificationParams,
    TextContent,
    ToolExecution,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module state
#
# `_TASK_AWARE_TOOLS` maps tool name → mode. Populated by the @task_aware
# decorator at import time. Read by the wrapped tools/call and tools/list
# handlers installed by enable_in_memory_tasks.
#
# `_RUNNING_TASKS` maps taskId → asyncio.Task running the tool body. Used by
# the cancel path to abort in-flight work. The store guards terminal-state
# transitions on its own; this dict is just for reaching the asyncio handle.
# ---------------------------------------------------------------------------

_TASK_AWARE_TOOLS: dict[str, TaskExecutionMode] = {}
_RUNNING_TASKS: dict[str, asyncio.Task[Any]] = {}


# ---------------------------------------------------------------------------
# WireSafeTaskStore — interop bridge between Python's "null is just None"
# and TypeScript's "optional means undefined, never null".
#
# This is the principled boundary for the cleaning, not a sprinkle of
# `_clean_for_wire(...)` calls scattered through the worker. Wire shape
# is the store's invariant; once data enters the store we treat it as
# "soon to be serialized," and that's where we strip Python `None`s
# from spec-optional fields.
#
# ## Why this exists
#
# The MCP Python SDK uses Pydantic. Optional fields default to `None`.
# Pydantic's `model_dump` includes `None` as JSON `null`. The MCP
# TypeScript SDK validates responses with Zod. Zod's `.optional()`
# means "undefined or absent" — it does NOT accept `null`. So a
# TextContent serialized by the Python side as
# `{"type":"text","text":"...","annotations":null,"_meta":null}` is
# rejected by the TS side with `-32603 invalid_union`.
#
# This is a real interop bug between two SDKs from the same project.
# Two clean upstream fixes are possible:
#   1. TS SDK: use `.nullish()` (= optional + nullable) on these fields.
#   2. Python SDK: default `exclude_none=True` for spec-optional fields.
#
# Either resolves it for everyone. Both should be filed. Until they
# land, we apply a thin compatibility layer at the store boundary —
# the natural place for "make sure what's in here is wire-correct."
#
# ## How
#
# `store_result(task_id, result)` round-trips the result through
# `model_dump(exclude_none=True)` and re-validates as a subclass that
# also overrides `model_dump` to always exclude None. The
# `TaskResultHandler` (in the SDK) calls `result.model_dump(by_alias=True)`
# during `tasks/result` — our override applies there, producing a
# null-free dict that survives the SDK's downstream re-validation
# without re-introducing nulls into the wire payload.
#
# ## When this can be removed
#
# - When the TS SDK accepts `null` for spec-optional fields, OR
# - When the Python SDK serializes spec-optional fields with
#   `exclude_none=True` by default,
#
# delete `WireSafeTaskStore` and use `InMemoryTaskStore` directly.
# Tracked: TODO file upstream issue with both SDKs and link here.
# ---------------------------------------------------------------------------


class _WireSafeResult(CallToolResult):
    """CallToolResult subclass whose ``model_dump`` always excludes None.

    This is what the SDK's TaskResultHandler dumps when serving
    `tasks/result`; the override is what makes the wire JSON null-free
    without us having to intercept every call site.
    """

    def model_dump(self, **kwargs: Any) -> Any:
        kwargs.setdefault("exclude_none", True)
        return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> Any:
        kwargs.setdefault("exclude_none", True)
        return super().model_dump_json(**kwargs)


class WireSafeTaskStore(InMemoryTaskStore):
    """In-memory task store that guarantees stored results serialize
    without ``null`` fields on the JSON-RPC wire.

    Drop-in replacement for ``InMemoryTaskStore`` — same interface,
    same in-memory semantics, plus one invariant: anything that enters
    via ``store_result`` will dump without None fields when retrieved
    and serialized for the wire. See module-level comment for the
    interop rationale.
    """

    async def store_result(self, task_id: str, result: Any) -> None:
        # Round-trip through `exclude_none=True` to drop None fields,
        # then re-validate into the override class so future
        # `model_dump` calls stay null-free.
        if isinstance(result, CallToolResult):
            clean = _WireSafeResult.model_validate(
                result.model_dump(exclude_none=True, by_alias=True)
            )
            await super().store_result(task_id, clean)
            return
        # Anything else (non-CallToolResult Result types we don't
        # currently produce) — pass through. We only own the
        # CallToolResult shape today.
        await super().store_result(task_id, result)


# Fallback TTL applied when a client sends task metadata without `ttl`
# (e.g., the TS MCP SDK's `callToolStream` stamps `task: {}` over
# user-supplied params.task in `Protocol.request`, dropping ttl). The MCP
# spec lets the receiver pick the actual TTL; this number just needs to be
# present so the resulting `Task.ttl` is a real number rather than None,
# avoiding `task.ttl: undefined` Zod failures on the client. 10 minutes
# matches the synapse-research worker's outer wall-clock budget.
_DEFAULT_TTL_MS = 600_000

# Context variable carrying the active taskId into the tool body. Lets the
# tool emit `notifications/tasks/status` from inside its work without the
# helper having to plumb taskId through every call site. Use
# `current_task_id()` to read.
_CURRENT_TASK_ID: ContextVar[str | None] = ContextVar("current_task_id", default=None)


T = TypeVar("T", bound=Callable[..., Any])


def task_aware(*, mode: TaskExecutionMode = "optional") -> Callable[[T], T]:
    """Mark a FastMCP tool as task-aware. Use INSTEAD of `task=TaskConfig(...)`.

    Args:
        mode: One of "optional", "required". "forbidden" is the implicit
              default for tools that don't use this decorator. We don't allow
              "forbidden" here because the absence of the decorator already
              means that.

    The decorator runs at import time; it relies on the wrapped function's
    `__name__` matching the tool name. If you pass `name=` to `@mcp.tool`,
    apply this decorator INSIDE (closest to the function), so it sees the
    wrapped function's name first; or set the registry entry by hand via
    `register_task_aware_tool(name, mode)`.
    """
    if mode not in ("optional", "required"):
        raise ValueError(f"task_aware mode must be 'optional' or 'required', got {mode!r}")

    def decorator(fn: T) -> T:
        # FastMCP's @mcp.tool(name=...) decorator wraps fn into a Tool
        # instance; we need the *tool* name, not the python function name.
        # The simplest reliable approach: inspect for an existing registered
        # tool that wraps this function, or fall back to fn.__name__ and let
        # the caller use register_task_aware_tool() if needed.
        _TASK_AWARE_TOOLS[fn.__name__] = mode
        return fn

    return decorator


def register_task_aware_tool(name: str, mode: TaskExecutionMode = "optional") -> None:
    """Imperative registration. Use when @mcp.tool's `name=` differs from
    the Python function name."""
    if mode not in ("optional", "required"):
        raise ValueError(f"mode must be 'optional' or 'required', got {mode!r}")
    _TASK_AWARE_TOOLS[name] = mode


def current_task_id() -> str | None:
    """Return the active taskId, or None if not inside a task-augmented call."""
    return _CURRENT_TASK_ID.get()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def enable_in_memory_tasks(mcp: FastMCP) -> InMemoryTaskStore:
    """Wire in-memory tasks utility onto a FastMCP server.

    What this does, in order:

    1. Calls the SDK's `mcp._mcp_server.experimental.enable_tasks(store=...)`
       which auto-registers spec-compliant handlers for tasks/get,
       tasks/result, tasks/list, tasks/cancel and advertises the `tasks`
       capability at initialize.

    2. Wraps the lowlevel server's `CallToolRequest` handler so that
       requests with `params.task` are routed through the task lifecycle:
       create task → spawn body → return CreateTaskResult immediately.

    3. Wraps the lowlevel server's `ListToolsRequest` handler so that
       task-aware tools advertise `execution.taskSupport` per their
       registered mode.

    Idempotent: subsequent calls return the existing store.
    """
    low = mcp._mcp_server

    # Idempotency: if we've already enabled tasks, return the existing store.
    existing = getattr(low, "_synapse_research_task_store", None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]

    # FastMCP's `_setup_task_protocol_handlers` (called from `_setup_handlers`
    # in FastMCP.__init__) unconditionally installs Docket-backed handlers
    # for tasks/get, tasks/result, tasks/list, tasks/cancel as long as
    # pydocket is importable. Pydocket IS importable in our environment (it
    # ships in our deps tree), so those handlers are present. We delete them
    # so the SDK's `experimental.enable_tasks(store=...)` — which only
    # registers defaults if absent — installs our in-memory path instead.
    from mcp.types import (
        CancelTaskRequest as _CancelTaskRequest,
    )
    from mcp.types import (
        GetTaskPayloadRequest as _GetTaskPayloadRequest,
    )
    from mcp.types import (
        GetTaskRequest as _GetTaskRequest,
    )
    from mcp.types import (
        ListTasksRequest as _ListTasksRequest,
    )

    for req_type in (
        _GetTaskRequest,
        _GetTaskPayloadRequest,
        _ListTasksRequest,
        _CancelTaskRequest,
    ):
        low.request_handlers.pop(req_type, None)

    # Use the wire-safe variant so `tasks/result` responses don't carry
    # null fields that the TS SDK's Zod validator rejects. See the
    # `WireSafeTaskStore` comment for the interop rationale.
    store = WireSafeTaskStore()
    low.experimental.enable_tasks(store=store)
    low._synapse_research_task_store = store  # ty:ignore[unresolved-attribute]

    _wrap_call_tool(mcp, low, store)
    _wrap_list_tools(low)
    _wrap_cancel_task(low, store)

    return store


# ---------------------------------------------------------------------------
# Handler wrappers
# ---------------------------------------------------------------------------


def _wrap_call_tool(mcp: FastMCP, low: Any, store: InMemoryTaskStore) -> None:
    """Replace the CallToolRequest handler with a task-aware wrapper.

    For requests with `params.task` set:
      - If the named tool is task-aware → dispatch through the helper.
      - If the named tool is `forbidden` (i.e., not registered as task-aware)
        → return -32601 per spec.

    For requests without `params.task`:
      - If the named tool is `mode="required"` → return -32601 per spec.
      - Otherwise → delegate to the original FastMCP handler (inline path).
    """
    original = low.request_handlers.get(CallToolRequest)
    if original is None:
        raise RuntimeError(
            "FastMCP did not register a CallToolRequest handler before enable_in_memory_tasks. "
            "Call enable_in_memory_tasks(mcp) AFTER all @mcp.tool registrations."
        )

    async def handler(req: CallToolRequest) -> ServerResult:
        tool_name = req.params.name
        task_meta = req.params.task
        mode = _TASK_AWARE_TOOLS.get(tool_name)  # None if not task-aware

        is_task_aware = mode in ("optional", "required")
        is_task_request = task_meta is not None

        if is_task_request and not is_task_aware:
            # Spec §: client requested task augmentation on a tool that
            # doesn't support it. Reject with -32601.
            raise McpError(
                ErrorData(
                    code=METHOD_NOT_FOUND,
                    message=f"tool {tool_name!r} does not support task-augmented execution",
                )
            )

        if is_task_aware and mode == "required" and not is_task_request:
            raise McpError(
                ErrorData(
                    code=METHOD_NOT_FOUND,
                    message=f"tool {tool_name!r} requires task-augmented execution",
                )
            )

        if is_task_request:
            # Spawn as a task and return CreateTaskResult immediately.
            # task_meta is non-None here (is_task_request was set from `task_meta is not None`).
            assert task_meta is not None
            return await _dispatch_as_task(mcp, req, task_meta, store)

        # Inline: delegate to the original FastMCP handler unchanged.
        return await original(req)

    low.request_handlers[CallToolRequest] = handler


async def _dispatch_as_task(
    mcp: FastMCP,
    req: CallToolRequest,
    task_meta: TaskMetadata,
    store: InMemoryTaskStore,
) -> ServerResult:
    """Create a task in the store, spawn the tool body, return CreateTaskResult.

    The spawned asyncio.Task calls FastMCP's `call_tool(name, args,
    task_meta=None)` directly — bypassing the SDK's CallToolRequest handler
    wrapper that would otherwise read `ctx.experimental.task_metadata` from
    the request context and route through FastMCP's Docket-coupled task path.
    By passing `task_meta=None` explicitly, we force the inline dispatch.

    Cancellation surfaces as `asyncio.CancelledError` inside the body —
    workers handle it normally.
    """
    # Force a real ttl number into the metadata if the client didn't supply
    # one. Without this the resulting Task has `ttl=None`, Pydantic drops
    # the field on JSON serialization, and the TS MCP client's TaskSchema
    # rejects the response (TaskSchema.ttl is `union(number, null)`,
    # required — undefined is not valid). See the SDK bug where
    # `Protocol.requestStream` stamps `task: {}` over the caller's
    # `params.task: {ttl}` in `client.experimental.tasks.callToolStream`.
    effective_meta = (
        task_meta
        if task_meta.ttl is not None
        else task_meta.model_copy(update={"ttl": _DEFAULT_TTL_MS})
    )
    task = await store.create_task(effective_meta)
    task_id = task.taskId

    background = asyncio.create_task(
        _run_tool_as_task(mcp, task_id, req.params.name, dict(req.params.arguments or {}), store),
        name=f"mcp-task:{task_id}:{req.params.name}",
    )
    _RUNNING_TASKS[task_id] = background

    return ServerResult(CreateTaskResult(task=task))


async def _run_tool_as_task(
    mcp: FastMCP,
    task_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    store: InMemoryTaskStore,
) -> None:
    """Body of the spawned task. Calls FastMCP's `call_tool` with
    `task_meta=None`, captures the result/exception, transitions task state,
    stores the payload."""
    token = _CURRENT_TASK_ID.set(task_id)
    try:
        try:
            tool_result = await mcp.call_tool(tool_name, arguments, task_meta=None)
            call_tool_result = _to_call_tool_result(tool_result)
        except asyncio.CancelledError:
            await _safe_terminal(store, task_id, TASK_STATUS_CANCELLED, "Cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — capture every error
            logger.exception("task %s failed during tool body", task_id)
            err_result = CallToolResult(
                content=[TextContent(type="text", text=f"Tool failed: {exc}")],
                isError=True,
            )
            try:
                await store.store_result(task_id, err_result)
            except ValueError:
                pass
            await _safe_terminal(store, task_id, TASK_STATUS_FAILED, str(exc))
            return

        try:
            await store.store_result(task_id, call_tool_result)
        except ValueError:
            pass
        terminal_status = TASK_STATUS_FAILED if call_tool_result.isError else TASK_STATUS_COMPLETED
        await _safe_terminal(store, task_id, terminal_status, None)
    finally:
        _CURRENT_TASK_ID.reset(token)
        _RUNNING_TASKS.pop(task_id, None)


def _to_call_tool_result(tool_result: Any) -> CallToolResult:
    """Convert FastMCP's ToolResult into an MCP CallToolResult.

    `ToolResult.to_mcp_result()` may return one of three shapes:
    - `CallToolResult` (when the tool already constructed one, or when meta
      is set)
    - `list[ContentBlock]` (no structured output)
    - `tuple[list[ContentBlock], dict]` (combined structured + unstructured)

    We normalize all three into a `CallToolResult`. Mirrors the SDK
    `Server.call_tool()` wrapper at `mcp/server/lowlevel/server.py` so the
    payload stored in the task store is identical to what an inline call
    would have returned.
    """
    raw = tool_result.to_mcp_result() if hasattr(tool_result, "to_mcp_result") else tool_result

    if isinstance(raw, CallToolResult):
        return raw

    # Tuple of (content, structured_content) — FastMCP's standard combo shape.
    if isinstance(raw, tuple) and len(raw) == 2:
        content, structured = raw
        return CallToolResult(
            content=list(content),
            structuredContent=structured,
            isError=False,
        )

    # Bare list of ContentBlocks.
    if isinstance(raw, list):
        return CallToolResult(content=raw, isError=False)

    # Unexpected — stringify defensively rather than raise.
    return CallToolResult(
        content=[TextContent(type="text", text=str(raw))],
        isError=False,
    )


async def _safe_terminal(
    store: InMemoryTaskStore,
    task_id: str,
    status: TaskStatus,
    message: str | None,
) -> None:
    """Transition a task to a terminal state, swallowing the SDK's
    'already terminal' guard (which raises ValueError per spec). Emit a
    `notifications/tasks/status` so subscribers (FastMCP Client, the
    platform engine) see the terminal transition without having to poll.
    """
    try:
        task = await store.update_task(task_id, status=status, status_message=message)
    except ValueError:
        # Already terminal (e.g., raced with tasks/cancel) — fine. Don't emit.
        return

    # Best-effort terminal notification. Spec marks these optional; clients
    # MUST poll if they need the answer. We emit anyway for live UX.
    try:
        from mcp.server.lowlevel.server import request_ctx as _request_ctx

        rc = _request_ctx.get()
        notification = ServerNotification(
            TaskStatusNotification(
                method="notifications/tasks/status",
                params=TaskStatusNotificationParams(
                    taskId=task.taskId,
                    status=task.status,
                    statusMessage=task.statusMessage,
                    createdAt=task.createdAt,
                    lastUpdatedAt=task.lastUpdatedAt or datetime.now(UTC),
                    ttl=task.ttl,
                    pollInterval=task.pollInterval,
                ),
            )
        )
        await rc.session.send_notification(notification)
    except (LookupError, Exception):  # noqa: BLE001
        # No active session, or transport gone. Polling still works.
        logger.debug("could not emit notifications/tasks/status for %s", task_id, exc_info=True)


def _wrap_cancel_task(low: Any, store: InMemoryTaskStore) -> None:
    """Wrap the SDK's tasks/cancel handler so it also aborts the running
    asyncio.Task.

    The SDK's default cancel handler transitions the store to `cancelled`
    via `helpers.cancel_task`, but doesn't reach the asyncio.Task that's
    actually running the work. We wrap to do both: cancel the asyncio.Task
    first (so the body sees CancelledError), then let the SDK transition
    the store. The store guards terminal-state transitions so a race with
    `_run_tool_as_task`'s own cleanup is harmless.
    """
    from mcp.types import CancelTaskRequest

    sdk_default = low.request_handlers.get(CancelTaskRequest)
    if sdk_default is None:
        return  # SDK didn't register one (shouldn't happen post-enable_tasks)

    async def handler(req: CancelTaskRequest) -> ServerResult:
        task_id = req.params.taskId
        running = _RUNNING_TASKS.get(task_id)
        if running is not None and not running.done():
            running.cancel()
            # Don't await — the SDK handler will transition the store; our
            # asyncio.Task's finally clause will pop itself from _RUNNING_TASKS.
        return await sdk_default(req)

    low.request_handlers[CancelTaskRequest] = handler


def _wrap_list_tools(low: Any) -> None:
    """Replace the ListToolsRequest handler with one that stamps
    `execution.taskSupport` on task-aware tools.

    FastMCP only stamps `taskSupport` when a tool is registered with
    `task=TaskConfig(...)`, which we deliberately don't use. So we add it
    post-hoc in the response.
    """
    original = low.request_handlers.get(ListToolsRequest)
    if original is None:
        raise RuntimeError(
            "FastMCP did not register a ListToolsRequest handler. "
            "Call enable_in_memory_tasks(mcp) AFTER all @mcp.tool registrations."
        )

    async def handler(req: ListToolsRequest) -> ServerResult:
        result = await original(req)
        # ServerResult wraps a Result; for ListTools that's ListToolsResult.
        list_result = result.root if hasattr(result, "root") else result
        if not isinstance(list_result, ListToolsResult):
            return result
        for tool in list_result.tools:
            mode = _TASK_AWARE_TOOLS.get(tool.name)
            if mode is None:
                continue
            tool.execution = ToolExecution(taskSupport=mode)
        return ServerResult(list_result)

    low.request_handlers[ListToolsRequest] = handler


# ---------------------------------------------------------------------------
# Status emission helper for tool bodies
# ---------------------------------------------------------------------------


async def update_task_status(
    mcp: FastMCP,
    *,
    message: str | None = None,
) -> None:
    """Emit a `notifications/tasks/status` for the active task.

    Optional. The MCP spec says receivers MAY emit these and clients MUST
    NOT rely on them — polling `tasks/get` is the contract. We emit them as
    a quality-of-life signal so platform UIs can render live progress
    without waiting for the next poll.

    If called outside a task-augmented context (no active taskId), this is a
    no-op.
    """
    task_id = _CURRENT_TASK_ID.get()
    if task_id is None:
        return

    low = mcp._mcp_server
    store: InMemoryTaskStore | None = getattr(low, "_synapse_research_task_store", None)
    if store is None:
        return

    # Update the store first so a polling tasks/get sees the new message.
    # The SDK's update_task already calls notify_update internally, which
    # wakes waiters in tasks/result.
    try:
        task = await store.update_task(task_id, status_message=message)
    except ValueError:
        # Task gone — terminal or deleted. No-op.
        return

    # Emit the optional notifications/tasks/status. We have to construct it
    # by hand because the SDK doesn't expose a session helper for this.
    try:
        ctx = low.request_context
    except LookupError:
        return  # Not inside a request context — nothing to send through.

    notification = ServerNotification(
        TaskStatusNotification(
            method="notifications/tasks/status",
            params=TaskStatusNotificationParams(
                taskId=task.taskId,
                status=task.status,
                statusMessage=task.statusMessage,
                createdAt=task.createdAt,
                lastUpdatedAt=task.lastUpdatedAt or datetime.now(UTC),
                ttl=task.ttl,
                pollInterval=task.pollInterval,
            ),
        )
    )
    try:
        await ctx.session.send_notification(notification)
    except Exception:  # noqa: BLE001
        # Notifications are best-effort. Don't break the tool body if the
        # transport is gone.
        logger.debug("failed to send notifications/tasks/status for %s", task_id, exc_info=True)


__all__ = [
    "current_task_id",
    "enable_in_memory_tasks",
    "register_task_aware_tool",
    "task_aware",
    "update_task_status",
]


# Suppress unused-import warning for uuid; reserved for future use if we
# decide to generate task IDs ourselves rather than letting the SDK do it.
_ = uuid
