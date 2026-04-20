# Research app — agent context

This app provides one capability: **kick off a research run and get a markdown
report back**.

## Entity model

One entity: `research_run`.

| Field            | Purpose                                                      |
|------------------|--------------------------------------------------------------|
| `query`          | The research question                                        |
| `run_status`     | `working` / `completed` / `failed` / `cancelled`             |
| `progress`       | 0–100 percent complete                                       |
| `status_message` | Short description of the current phase                       |
| `report`         | Final markdown (populated on completion)                     |
| `error_message`  | Populated on failure                                         |
| `started_at`     | ISO 8601                                                     |
| `completed_at`   | ISO 8601, populated on any terminal state                    |

The `run_status` values mirror the MCP task lifecycle exactly — there is a 1:1
mapping between a `research_run` entity and the MCP task that executed it. The
base entity also has a `status` field (`active`/`archived`/`deleted`) reserved
by Upjack for lifecycle; don't confuse the two.

## Tools

- `start_research(query)` — **the** tool. Task-augmented (required). The client
  wraps the call with a `task` field; the engine polls and retrieves the result
  when the task completes.
- Auto-generated entity tools (`get_research_run`, `list_research_runs`, etc.)
  come from the Upjack entity declaration and are safe to use for reads.

## When not to call

- Don't call `start_research` for quick factual lookups — those belong to the
  LLM's own knowledge or a fast search tool.
- Don't re-run the same query in rapid succession. Check recent runs first.

## How state flows

```
user asks to research X
  → agent calls start_research (task-augmented)
    → server creates research_run entity, run_status=working, progress=0
    → worker updates entity on each phase; calls ctx.report_progress
      → engine sees notifications/tasks/status → streams to client
      → Synapse UI sees entity update → re-renders card live
    → worker returns { run_id, status, report }
    → engine retrieves via tasks/result → returns to LLM → agent replies
```

Both channels (engine notifications, entity stream) are kept in lockstep by the
worker. If they ever diverge, the entity is the UI source of truth; the task is
the engine source of truth.
