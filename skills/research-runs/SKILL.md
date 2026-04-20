---
name: research-runs
description: Kick off research on any topic. Returns a markdown report after ~1 minute of work.
when_to_use: When the user asks to research, investigate, dig into, or summarize current information about a topic.
---

# Research Runs

## When to trigger

Call `start_research` whenever the user asks you to research, investigate,
summarize the state of, or "go find out about" a topic. Examples:

- "Research current pricing for managed Postgres providers"
- "Dig into what's new in MCP servers this quarter"
- "Summarize recent moves in AI agent frameworks"

## How to call

`start_research(query=<user's question>)` — the tool is task-augmented, so the
engine will automatically wrap the call, poll for completion, and deliver the
final result. You do not need to manage polling or task IDs yourself.

The tool returns a markdown report. Render it directly to the user; do not
restate or re-summarize unless they ask.

## While the run is in flight

The user can watch progress live in the Research app (sidebar → Research → Active).
Each run shows its current phase (planning, gathering, writing) and percent
complete. If they ask "what's the status?" while a run is active, direct them
there — don't poll yourself.

## Rules

- One query per call. If the user asks about multiple topics, call once per topic.
- Keep queries concise and well-formed — they are passed straight to the research
  engine and rewritten internally into sub-questions.
- If a run fails, surface the error message from the returned `error_message`
  field and offer to retry with a refined query.
