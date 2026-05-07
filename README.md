# synapse-research

[![mpak](https://img.shields.io/badge/mpak-registry-blue)](https://mpak.dev/packages/@nimblebraininc/synapse-research?utm_source=github&utm_medium=readme&utm_campaign=synapse-research)
[![NimbleBrain](https://img.shields.io/badge/NimbleBrain-nimblebrain.ai-purple)](https://nimblebrain.ai?utm_source=github&utm_medium=readme&utm_campaign=synapse-research)
[![Discord](https://img.shields.io/badge/Discord-community-5865F2)](https://nimblebrain.ai/discord?utm_source=github&utm_medium=readme&utm_campaign=synapse-research)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Deep-research MCP app. Exposes one task-augmented tool (`start_research`) that runs [GPT-Researcher](https://github.com/assafelovic/gpt-researcher) against Tavily (web search), Anthropic Claude (planner + writer LLM), and OpenAI (embeddings only), and streams progress back through both the MCP tasks protocol and the Upjack entity stream. Fully compliant with the MCP 2025-11-25 draft `tasks` utility via FastMCP 3.

**[View on mpak registry](https://mpak.dev/packages/@nimblebraininc/synapse-research?utm_source=github&utm_medium=readme&utm_campaign=synapse-research)** | **Built by [NimbleBrain](https://nimblebrain.ai?utm_source=github&utm_medium=readme&utm_campaign=synapse-research)**

## Quick Start

Install via [mpak](https://mpak.dev) into your NimbleBrain workspace:

```bash
mpak install @nimblebraininc/synapse-research
```

Set the three required credentials in your host's shell (Bun auto-loads `.env`, or export directly):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export TAVILY_API_KEY=tvly-...
export OPENAI_API_KEY=sk-...
```

Run a research task from your agent chat:

> "Research what's new with Model Context Protocol in 2026"

The agent fires `start_research`, the worker streams progress into the chat UI and into the Synapse sidebar dashboard, and you get back a markdown report in ~30s–3min.

## Architecture

```
chat: "research X"
  │
  ▼
NimbleBrain engine ──┐
                     │  tools/call (task-augmented)
                     ▼
            FastMCP server (this app)
                     │
                     ├─► creates research_run entity (status=working)
                     ├─► spawns worker (asyncio)
                     │     │
                     │     ├─► ctx.report_progress  ──► notifications/tasks/status ──► engine
                     │     └─► app.update_entity    ──► filesystem ──► Synapse UI live stream
                     │
                     └─► returns CreateTaskResult immediately
                         (engine polls tasks/get, retrieves via tasks/result when terminal)
```

Two independent channels update in lockstep:
- **Engine channel** — MCP task status notifications. The engine uses these to render progress in the chat UI and to stabilise polling cadence.
- **UI channel** — entity writes via Upjack. The Synapse sidebar app reads the entity stream to render a live dashboard of runs.

### UI retry flow

The sidebar's "Retry with same query" button uses `useCallToolAsTask("start_research")` from the Synapse SDK (≥ 0.6.0). The task handle returns a `taskId` in under a second; the new `research_run` entity materialises shortly after (the worker creates it as its first action), and the UI navigates to the new detail page off the entity channel — not by waiting on the task's terminal result, which arrives minutes later. Second click on the button while "Starting…" routes through `handle.cancel()`.

Legacy hosts (platform builds prior to the tasks-capability advertisement) cause `callToolAsTask` to throw. The UI catches that case and falls back to `synapse.callTool` fire-and-forget, so the feature keeps working with older deploys — only the starting-state indicator and the cancel handle degrade.

## Configuration

### Credentials (declared as `user_config` in `manifest.json`)

The host runtime prompts for these at install time or resolves them from a workspace-scoped store, then injects them into the bundle subprocess via `mcp_config.env`:

| Config key | Env var exposed | Purpose |
|---|---|---|
| `anthropic_api_key` | `ANTHROPIC_API_KEY` | Claude LLM — planning + report writing |
| `tavily_api_key` | `TAVILY_API_KEY` | Web search |
| `openai_api_key` | `OPENAI_API_KEY` | Embeddings only (`text-embedding-3-small`) |

All three are required and marked `sensitive: true`.

### Routing (hard-coded in `mcp_config.env`)

Not tenant-tunable in v1 — set directly in the manifest:

```
RETRIEVER=tavily
FAST_LLM=anthropic:claude-haiku-4-5
SMART_LLM=anthropic:claude-sonnet-4-6
STRATEGIC_LLM=anthropic:claude-sonnet-4-6
EMBEDDING=openai:text-embedding-3-small
```

To change an LLM or retriever, edit `manifest.json` and reinstall the bundle. Promoting any of these to `user_config` is a one-line change if per-workspace tuning is needed.

### Cost and latency

- Typical run: **30s–3min**.
- Typical cost: **$0.15–$0.60/run** on Sonnet 4.6 + Tavily advanced + OpenAI embeddings.
- Hard-cap: **5 minutes** via `asyncio.wait_for`. Longer runs are marked `failed` with a timeout error.

## Data layout

One entity: `research_run`. Lives under:
```
$UPJACK_ROOT/apps/research/data/research_runs/{id}.json
```

Data-root resolution priority:
1. `UPJACK_ROOT` env var
2. `MPAK_WORKSPACE` env var
3. `~/.synapse-research` (fallback)

Each workspace spawns its own server process with its own root. There is no cross-workspace state inside the server.

## Running locally

### Install deps

```bash
uv sync
cd ui && npm install && npm run build && cd ..
```

### Stdio (Claude Desktop, any MCP client)

```bash
uv run python -m mcp_research.server
```

### HTTP (NimbleBrain platform)

```bash
uv run uvicorn mcp_research.server:app --port 8002
```

### Tests (keyless — no API keys required)

```bash
uv run pytest tests/ -v
```

The spec-compliance suite (`tests/test_spec_compliance.py`) exercises every MUST from the MCP tasks draft: capability advertisement, `execution.taskSupport` gating, `tasks/get|result|cancel|list`, TTL behaviour, progress notifications, workspace isolation. The worker suite (`tests/test_worker.py`) covers happy path, cancel, failure, monotonic progress, and source streaming. All tests use a `FakeGPTR` monkeypatch so real providers are never called in CI.

## Tool reference

| Tool | Task support | Description |
|---|---|---|
| `start_research` | `optional` | The only custom tool. Runs the research worker end-to-end. |
| `get_research_run` | n/a | Auto-generated entity tool (read by id). |
| `list_research_runs` | n/a | Auto-generated entity tool. |
| `search_research_runs` | n/a | Auto-generated entity tool. |
| `delete_research_run` | n/a | Auto-generated entity tool (soft delete). |

Cancellation is handled at the MCP protocol level via `tasks/cancel`. The worker catches `asyncio.CancelledError`, flips the entity to `cancelled`, and re-raises so FastMCP transitions the task to its cancelled terminal state.

## Contributing

See [CLAUDE.md](CLAUDE.md) for the architecture walkthrough, commands, conventions, and build pipeline.

Quality gates (run before opening a PR):

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run ty check src/
uv run pytest tests/ -v
cd ui && npm ci && npm run build
```

CI enforces the same gates — see `.github/workflows/ci.yml`.

## Ecosystem

- **[NimbleBrain](https://nimblebrain.ai?utm_source=github&utm_medium=readme&utm_campaign=synapse-research)** — the agent platform this app runs on
- **[mpak](https://mpak.dev?utm_source=github&utm_medium=readme&utm_campaign=synapse-research)** — MCP bundle registry where releases are published
- **[Upjack](https://upjack.dev?utm_source=github&utm_medium=readme&utm_campaign=synapse-research)** — declarative AI-app framework (entity schemas, skills, hooks)
- **[Synapse SDK](https://www.npmjs.com/package/@nimblebrain/synapse)** — React hooks powering the UI
- **[GPT-Researcher](https://github.com/assafelovic/gpt-researcher)** — Apache-2.0 research engine this app wraps
- **[Discord community](https://nimblebrain.ai/discord?utm_source=github&utm_medium=readme&utm_campaign=synapse-research)**

## License

MIT — see [LICENSE](LICENSE).
