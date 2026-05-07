# synapse-research

Deep-research MCP app with a task-augmented tool that runs GPT-Researcher under the hood. Two-project architecture: Python MCP server (FastMCP) + React/Vite UI (Synapse SDK).

## Architecture

```
synapse-research/
├── src/mcp_research/         Python MCP server (FastMCP)
│   ├── server.py             Tool registration, orphan reaper, ui:// resource
│   └── worker.py             run_research + _MCPLogHandler (GPT-Researcher wrapper)
├── schemas/                  Upjack entity JSON schemas
│   └── research_run.schema.json
├── skills/                   Natural-language skill definitions bundled with the app
│   └── research-runs/SKILL.md
├── ui/                       React + Vite project (Synapse SDK)
│   ├── src/App.tsx           Live dashboard of runs via useDataSync
│   ├── vite.config.ts        synapseVite() plugin for dev
│   └── dist/index.html       Built single-file bundle
└── tests/                    pytest-asyncio suite — keyless (FakeGPTR fixture)
```

**How it connects:** `start_research` is registered as a task-augmented MCP tool (`execution.taskSupport: "optional"`). The worker wraps GPT-Researcher and streams progress through two channels in lockstep — `ctx.report_progress` for the MCP task status stream (drives the chat UI), and `app.update_entity` for the Upjack entity store (drives the Synapse sidebar dashboard).

## Commands

```bash
# Install server deps
uv sync

# Install UI deps + build
cd ui && npm install && npm run build && cd ..

# Run tests (keyless — no API keys required)
uv run pytest tests/ -v

# Lint + typecheck + format check
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run ty check src/

# Run stdio (Claude Desktop, mpak, any MCP client)
uv run python -m mcp_research.server

# Run HTTP (NimbleBrain platform)
uv run uvicorn mcp_research.server:app --port 8002

# Build deps/ for mpak bundling (regenerates 460+ vendored packages)
rm -rf deps/ && uv pip install --target ./deps .

# Bump version across manifest.json, pyproject.toml, __init__.py
make bump VERSION=0.2.0
```

## How the task-augmented tool works

`start_research` declares `execution.taskSupport: "optional"` via the in-process task helper (`src/mcp_research/_tasks.py`) — NOT FastMCP's built-in `TaskConfig(mode="optional")`. FastMCP's task module routes through pydocket/Redis, which we deliberately avoid: tenant pods enforce namespace isolation and don't expose Redis (see `.tasks/task-aware-tools/PLATFORM_RELAY_VERIFIED.md`). The helper composes the MCP Python SDK's `InMemoryTaskStore` and `experimental.enable_tasks(store=...)` to provide a fully spec-compliant tasks utility in-process. No Redis. No Docket. Tasks live for the bundle subprocess lifetime; `_reap_orphaned_runs()` on startup cleans up entities orphaned by a crash.

The worker contract (`run_research` in `worker.py`):

1. Creates a `research_run` entity (`run_status="working"`, `progress=0`)
2. Instantiates `GPTResearcher(query, report_type="research_report", log_handler=handler)`
3. Wraps `conduct_research()` + `write_report()` in `asyncio.wait_for(timeout=300)` (5-min hard cap)
4. Streams phase transitions via the `_MCPLogHandler` — maps GPT-Researcher's async log events onto monotonic progress buckets (planning=10 → searching=30 → scraping=55 → analyzing=75 → writing=85 → done=100)
5. Updates the entity with `sources` as they stream in, then the final `report`
6. Returns `{"run_id", "status": "completed", "report": <markdown>}`

Error handling:
- `asyncio.CancelledError` → mark entity `cancelled`, re-raise (FastMCP transitions task terminal)
- `asyncio.TimeoutError` → mark entity `failed` with explicit timeout message, re-raise
- Any other exception → mark entity `failed` with `error_message`, re-raise

On server restart, `_reap_orphaned_runs()` flips any entity stuck in `working` to `failed` with a clear message — no lingering ghost runs.

## SDK dependency

Requires `@nimblebrain/synapse ≥ 0.7.0` (the release that introduces `useCallToolAsTask`). Until 0.7.0 is on npm, install via `npm pack` from the sibling SDK checkout: `(cd packages/synapse && npm pack --pack-destination /tmp) && (cd synapse-apps/synapse-research/ui && npm install /tmp/nimblebrain-synapse-0.7.0.tgz)`. `ui/dist/` is gitignored and rebuilt by the release workflow.

## UI retry: dual-channel pattern

The "Retry with same query" button uses `useCallToolAsTask("start_research")` (synapse ≥ 0.7.0), not blocking `callTool`. Lifecycle ("Starting…" indicator, cancel) comes from the task channel; the new `run_id` comes from `useDataSync` on `research_run` because the worker creates the entity before any GPT-Researcher work — so the UI sees the new id within ~100ms while the task is still `working`. `App.tsx`'s `useRetryFlow` snapshots the known-id set at fire time and navigates when a new id appears.

Fallback for legacy hosts: when `callToolAsTask` throws (host didn't advertise `tasks.requests.tools.call`), `App.tsx` falls back to fire-and-forget `synapse.callTool`. Entity channel still delivers `run_id`; only the starting indicator and cancel degrade.

Excluded from the task path: `list_research_runs` / `delete_research_run` (fast CRUD, plain `callTool`); agent-initiated `start_research` (platform engine augments at its layer).

Pattern docs: [docs.nimblebrain.ai/apps/synapse#long-running-tools](https://docs.nimblebrain.ai/apps/synapse/#long-running-tools). Regression guards: `tests/test_ui_retry_contract.py`, `tests/test_spec_compliance.py::test_entity_appears_before_task_terminal`.

## Configuration

Three required `user_config` fields declared in `manifest.json`, resolved by the host at startup:

| Config key | Env var | Purpose |
|---|---|---|
| `anthropic_api_key` | `ANTHROPIC_API_KEY` | Claude LLM (planner + writer) |
| `tavily_api_key` | `TAVILY_API_KEY` | Web search |
| `openai_api_key` | `OPENAI_API_KEY` | Embeddings only (`text-embedding-3-small`) |

Routing is hard-coded in `mcp_config.env` (not tenant-tunable in v1):
```
RETRIEVER=tavily
FAST_LLM=anthropic:claude-haiku-4-5
SMART_LLM=anthropic:claude-sonnet-4-6
STRATEGIC_LLM=anthropic:claude-sonnet-4-6
EMBEDDING=openai:text-embedding-3-small
```

## Server tools

| Tool | Task support | Description |
|---|---|---|
| `start_research` | `optional` | The only custom tool. Runs the full research workflow. |
| `get_research_run` | n/a | Auto-generated entity tool (read by id). |
| `list_research_runs` | n/a | Auto-generated entity tool. |
| `search_research_runs` | n/a | Auto-generated entity tool. |
| `delete_research_run` | n/a | Auto-generated entity tool (soft delete). |

## UI resource

`ui://research/main` — served by `research_ui()` in `server.py`. Reads `ui/dist/index.html` (the Vite single-file bundle) and returns it as an MCP resource. Hosts that support ext-apps render it in an iframe.

## Data layout

One entity: `research_run`. Lives under:
```
$UPJACK_ROOT/apps/research/data/research_runs/{id}.json
```

Root resolution priority:
1. `UPJACK_ROOT` env var
2. `MPAK_WORKSPACE` env var
3. `~/.synapse-research` (fallback)

Each workspace gets its own root — no cross-workspace state inside the server.

## Tests

All tests are **keyless** — `tests/conftest.py` provides a `fake_researcher` fixture that monkeypatches `mcp_research.worker.GPTResearcher` with a scripted `FakeGPTR`. The `mcp` and `app_and_mcp` fixtures depend on it so spec-compliance tests never hit real providers.

- `tests/test_worker.py` — direct worker tests (happy path, cancel, failure, monotonic progress, source streaming)
- `tests/test_spec_compliance.py` — MCP tasks draft 2025-11-25 compliance (tool registration, task-augmented dispatch, `tasks/get|result|cancel|list`, TTL, workspace isolation)

CI runs the full suite with `ANTHROPIC_API_KEY`, `TAVILY_API_KEY`, `OPENAI_API_KEY` unset to catch any accidental real-provider call.

## Build pipeline

`make bundle` produces a `.mcpb` archive:
1. `rm -rf deps/ && uv pip install --target ./deps .` — vendors Python deps (including `gpt-researcher` and `langchain-anthropic`)
2. `mcpb validate manifest.json`
3. `mcpb pack` — creates `nimblebraininc-research-{version}-{os}-{arch}.mcpb`

`.github/workflows/release.yml` runs this on `release: published`.

## Conventions

- **Python:** `uv` for package management, `ruff` for lint + format, `ty` for type checking
- **UI:** TypeScript strict mode, React 19, Vite 6
- **Tests:** pytest-asyncio, keyless (FakeGPTR monkeypatch); never import the real `gpt_researcher` in test code
- **Versioning:** `make bump VERSION=x.y.z` keeps `manifest.json`, `server.json`, `pyproject.toml`, and `src/mcp_research/__init__.py` in sync
