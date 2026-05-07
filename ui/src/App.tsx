import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useCallToolAsTask, useDataSync, useSynapse } from "@nimblebrain/synapse/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { ANIMATION_STYLES, RESPONSIVE_STYLES, STATUS_COLORS, s } from "./styles";
import { useInjectThemeTokens } from "./theme-utils";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type RunStatus = "working" | "completed" | "failed" | "cancelled";

interface Source {
  url: string;
  title: string;
  snippet?: string | null;
}

interface ResearchRun {
  id: string;
  query: string;
  // Short human label (3–8 words) generated server-side a moment after
  // the entity is created, or supplied by the calling agent. May be
  // null/absent on legacy entities and during the brief window before
  // server-side generation lands; render falls back to a truncated
  // query in that state.
  title?: string | null;
  run_status: RunStatus;
  progress: number;
  status_message?: string | null;
  report?: string | null;
  error_message?: string | null;
  sources?: Source[];
  started_at?: string;
  completed_at?: string | null;
  // The worker writes this on every signal — phase transition AND
  // liveness heartbeat. The UI uses it (independently of `progress`) to
  // detect runs that are silently hung. Optional for backward compat
  // with entities written before the four-signal model landed.
  last_heartbeat_at?: string | null;
  // Stamped by the worker on each phase entry; cleared on terminal
  // transition. The UI derives `elapsed = now - current_phase_started_at`
  // every second client-side and appends it to the status line. This
  // decouples the display clock (smooth, 1s) from the worker write clock
  // (5s heartbeat), so we don't need to either pay for 5x the entity
  // writes or watch the displayed time tick in 5-second chunks.
  current_phase_started_at?: string | null;
  created_at: string;
  updated_at: string;
}

// Liveness thresholds in seconds. The worker heartbeats every 5s, so
// 10s comfortably catches "no signal at all". 30s is the "actually
// stalled" threshold — beyond a typical LLM stream chunk gap.
const STALE_THRESHOLD_S = 10;
const HUNG_THRESHOLD_S = 30;

type Liveness = "live" | "stale" | "hung";

// Compute the liveness state for a run. Only meaningful for `working`
// runs; everything else returns "live" (no staleness signal needed).
// Returns "live" when `last_heartbeat_at` is missing — backward compat
// with legacy entities that don't carry the field yet.
function computeLiveness(run: ResearchRun): Liveness {
  if (run.run_status !== "working") return "live";
  if (!run.last_heartbeat_at) return "live";
  const age = (Date.now() - Date.parse(run.last_heartbeat_at)) / 1000;
  if (age >= HUNG_THRESHOLD_S) return "hung";
  if (age >= STALE_THRESHOLD_S) return "stale";
  return "live";
}

// Format a duration in milliseconds as a short, scannable string. Tuned
// for the phase-timeline labels: sub-minute durations stay in seconds
// (no decimals), longer ones use `Nm Ns`. Negative or NaN → empty
// string so missing/malformed timestamps don't blow up the UI.
function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "";
  const totalSeconds = Math.round(ms / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return s === 0 ? `${m}m` : `${m}m ${s}s`;
}

// Re-render at a fixed cadence. Used to keep relative-time displays
// (staleness counters, running-phase elapsed) accurate between the data
// poll cycles. Only ticks while `active` is true so unmounted / list-view
// renders don't churn.
function useTick(active: boolean, periodMs: number): void {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setTick((t) => t + 1), periodMs);
    return () => window.clearInterval(id);
  }, [active, periodMs]);
}

const PAGE_SIZE = 10;

// The reading column — every section wraps itself in one of these so the
// layout stays anchored to a single centered measure instead of floating
// across the iframe. Kept here (not in styles.ts) so the className is visible
// when debugging in the inspector.
const COLUMN_CLASS = "rs-column-pad";

function Column({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <div className={COLUMN_CLASS} style={{ ...s.column, ...s.columnPad, ...style }}>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Inject responsive styles once
// ---------------------------------------------------------------------------

let stylesInjected = false;
function injectStyles() {
  if (stylesInjected) return;
  stylesInjected = true;
  const el = document.createElement("style");
  el.textContent = `${RESPONSIVE_STYLES}\n${ANIMATION_STYLES}`;
  document.head.appendChild(el);
}

// ---------------------------------------------------------------------------
// Hash-based routing
//   #/            list
//   #/r/<id>      detail
// ---------------------------------------------------------------------------

type Route = { view: "list" } | { view: "detail"; id: string };

function parseHash(hash: string): Route {
  const h = hash.replace(/^#/, "");
  if (h === "" || h === "/") return { view: "list" };
  const match = h.match(/^\/r\/(.+)$/);
  if (match?.[1]) return { view: "detail", id: match[1] };
  return { view: "list" };
}

function formatHash(route: Route): string {
  return route.view === "detail" ? `#/r/${route.id}` : "#/";
}

function useRoute(): [Route, (r: Route) => void] {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const onHashChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);
  const navigate = useCallback((r: Route) => {
    window.location.hash = formatHash(r);
  }, []);
  return [route, navigate];
}

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------

function useRuns() {
  const synapse = useSynapse();
  const [runs, setRuns] = useState<ResearchRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const callId = useRef(0);

  const refresh = useCallback(async () => {
    const id = ++callId.current;
    setError(null);
    try {
      const result = await synapse.callTool<
        Record<string, unknown>,
        ResearchRun[] | { entities: ResearchRun[] }
      >("list_research_runs", { limit: 500 });
      if (id !== callId.current) return;
      if (result.isError) {
        setError(String(result.data));
        return;
      }
      const data = result.data;
      const entities: ResearchRun[] = Array.isArray(data)
        ? data
        : Array.isArray((data as { entities?: ResearchRun[] })?.entities)
          ? (data as { entities: ResearchRun[] }).entities
          : [];
      entities.sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""));
      setRuns(entities);
    } catch (err) {
      if (id !== callId.current) return;
      setError(err instanceof Error ? err.message : "Failed to fetch runs");
    } finally {
      if (id === callId.current) setLoading(false);
    }
  }, [synapse]);

  useEffect(() => {
    synapse.ready.then(() => refresh());
  }, [synapse, refresh]);

  useDataSync(() => {
    refresh();
  });

  const hasWorking = runs.some((r) => r.run_status === "working");

  // Refresh while any run is in flight — smooths the progress bar between
  // data.changed events. Not a safety net, just cadence alignment: the
  // server's taskStatus pollInterval is 5 s, so we fetch more frequently to
  // keep the bar moving between SSE ticks.
  useEffect(() => {
    if (!hasWorking) return;
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, [hasWorking, refresh]);

  const deleteRun = useCallback(
    async (runId: string) => {
      // Upjack's auto-generated delete tool names the id param after the
      // entity, not a generic "id". For entity `research_run` it expects
      // `research_run_id`. See upjack/server.py::`id_param = f"{name}_id"`.
      setRuns((prev) => prev.filter((r) => r.id !== runId));
      try {
        const result = await synapse.callTool("delete_research_run", {
          research_run_id: runId,
        });
        if (result.isError) {
          setError(`Failed to delete run: ${String(result.data)}`);
          refresh();
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to delete run");
        refresh();
      }
    },
    [synapse, refresh],
  );

  return { runs, loading, error, refresh, deleteRun } as const;
}

// ---------------------------------------------------------------------------
// Tiny primitives
// ---------------------------------------------------------------------------

function StatusDot({
  status,
  size = 8,
  glow = false,
  liveness = "live",
  style,
}: {
  status: RunStatus;
  size?: number;
  glow?: boolean;
  /** Liveness state for working runs. "live" → pulsing green. "stale" →
   *  no pulse, color shifts toward yellow (something to investigate).
   *  "hung" → amber static. Ignored for non-working statuses. */
  liveness?: Liveness;
  /** Layout overrides for the call site's container (grid / flex). The dot's
   *  own visual styling lives below — callers should only pass positioning. */
  style?: React.CSSProperties;
}) {
  // Pulse only when the run is genuinely working AND fresh. A stale or
  // hung working run drops the pulse — the missing animation is itself
  // a diagnostic signal (something is wrong even though run_status is
  // still "working").
  const isAlive = glow && status === "working" && liveness === "live";
  const className = isAlive ? "rs-pulse rs-dot-alive-static" : undefined;

  // For working runs, swap the green for staleness colors. For terminal
  // statuses (completed/failed/cancelled), liveness is irrelevant. Typed
  // as `string` because `STATUS_COLORS` is `as const` — the by-key narrow
  // types don't unify across the working/stale/hung branch.
  let background: string = STATUS_COLORS[status];
  if (status === "working") {
    if (liveness === "hung") background = STATUS_COLORS.hung;
    else if (liveness === "stale") background = STATUS_COLORS.stale;
  }

  return (
    <span
      aria-label={status === "working" && liveness !== "live" ? `${status} (${liveness})` : status}
      className={className}
      style={{
        width: size,
        height: size,
        borderRadius: 999,
        flexShrink: 0,
        transition: "box-shadow 240ms ease, background 240ms ease",
        background,
        ...style,
      }}
    />
  );
}

// `query` is the full research brief (often a paragraph); `title` is the
// short human label (3–8 words) for list rows and headings. The two are
// distinct fields on the entity. The server fills in `title` shortly
// after entity creation; until then — and for any legacy entity that
// predates the field — fall back to a truncated query so the UI never
// renders an empty heading. 60 chars at a word boundary keeps the
// fallback compact enough to read at a glance without distorting the
// layout.
const TITLE_FALLBACK_CHARS = 60;
function displayTitle(run: ResearchRun): string {
  const title = run.title?.trim();
  if (title) return title;
  const q = (run.query ?? "").trim();
  if (q.length <= TITLE_FALLBACK_CHARS) return q;
  // Cut at a word boundary so we don't break a word mid-character.
  const cut = q.slice(0, TITLE_FALLBACK_CHARS);
  const lastSpace = cut.lastIndexOf(" ");
  return `${(lastSpace > 20 ? cut.slice(0, lastSpace) : cut).trimEnd()}…`;
}

function formatAge(iso: string | undefined | null): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const secs = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

function rowMetaLine(run: ResearchRun): string {
  switch (run.run_status) {
    case "working": {
      // Same elapsed-time derivation as the detail view: client-side,
      // off the stable phase-start timestamp. RunRow's `useTick(1s)`
      // keeps this fresh between the 2s data poll cycles.
      const elapsedS =
        run.current_phase_started_at !== null && run.current_phase_started_at !== undefined
          ? Math.max(0, Math.floor((Date.now() - Date.parse(run.current_phase_started_at)) / 1000))
          : null;
      const base = (run.status_message || "working").toLowerCase();
      const parts = [base];
      if (elapsedS !== null) parts.push(`${elapsedS}s`);
      parts.push(`${run.progress}%`);
      return parts.join(" · ");
    }
    case "completed":
      return formatAge(run.completed_at ?? run.updated_at ?? run.created_at);
    case "failed": {
      const reason = run.error_message ? ` — ${run.error_message}` : "";
      return `failed${reason} · ${formatAge(run.completed_at ?? run.updated_at ?? run.created_at)}`;
    }
    case "cancelled":
      return `cancelled · ${formatAge(run.completed_at ?? run.updated_at ?? run.created_at)}`;
  }
}

// ---------------------------------------------------------------------------
// Markdown rendering — maps react-markdown's element output to our token
// styles so the rendered report matches the app's typography.
// ---------------------------------------------------------------------------

function ReportMarkdown({ source }: { source: string }) {
  return (
    <div style={s.report}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ node, ...props }) => <h1 style={s.mdH1} {...props} />,
          h2: ({ node, ...props }) => <h2 style={s.mdH2} {...props} />,
          h3: ({ node, ...props }) => <h3 style={s.mdH3} {...props} />,
          h4: ({ node, ...props }) => <h4 style={s.mdH3} {...props} />,
          h5: ({ node, ...props }) => <h5 style={s.mdH3} {...props} />,
          h6: ({ node, ...props }) => <h6 style={s.mdH3} {...props} />,
          p: ({ node, ...props }) => <p style={s.mdP} {...props} />,
          blockquote: ({ node, ...props }) => <blockquote style={s.mdBlockquote} {...props} />,
          ul: ({ node, ...props }) => <ul style={s.mdUl} {...props} />,
          ol: ({ node, ...props }) => <ol style={s.mdOl} {...props} />,
          li: ({ node, ...props }) => <li style={s.mdLi} {...props} />,
          a: ({ node, ...props }) => (
            <a style={s.mdA} target="_blank" rel="noopener noreferrer" {...props} />
          ),
          strong: ({ node, ...props }) => <strong style={s.mdStrong} {...props} />,
          em: ({ node, ...props }) => <em style={s.mdEm} {...props} />,
          hr: ({ node, ...props }) => <hr style={s.mdHr} {...props} />,
          code: ({ node, inline, className, children, ...props }: any) => {
            if (inline) {
              return (
                <code style={s.mdCodeInline} {...props}>
                  {children}
                </code>
              );
            }
            return (
              <code className={className} {...props}>
                {children}
              </code>
            );
          },
          pre: ({ node, ...props }) => <pre style={s.mdPre} {...props} />,
        }}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sources list — rendered inline in the detail view.
//
// The URL is shown in small monospace beneath the title so the user can see
// where the claim is sourced from without clicking. `noopener noreferrer`
// because the app runs inside an iframe and we never want the opened tab to
// be able to reach back into our window.
// ---------------------------------------------------------------------------

function SourceLink({ source, enterDelayMs = 0 }: { source: Source; enterDelayMs?: number }) {
  const [hover, setHover] = useState(false);
  return (
    <li
      className="rs-enter"
      style={{ ...s.sourceItem, animationDelay: `${enterDelayMs}ms` }}
    >
      <a
        href={source.url}
        target="_blank"
        rel="noopener noreferrer"
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        style={{ ...s.sourceTitle, ...(hover ? s.sourceTitleHover : null) }}
      >
        {source.title}
      </a>
      <span style={s.sourceUrl} title={source.url}>
        {prettyUrl(source.url)}
      </span>
      {source.snippet && <span style={s.sourceSnippet}>{source.snippet}</span>}
    </li>
  );
}

function prettyUrl(url: string): string {
  try {
    const u = new URL(url);
    const host = u.host.replace(/^www\./, "");
    const path = u.pathname.length > 40 ? `${u.pathname.slice(0, 39)}…` : u.pathname;
    return `${host}${path}`;
  } catch {
    return url;
  }
}

function SourcesSection({
  sources,
  heading = "Sources",
}: {
  sources: Source[];
  heading?: string;
}) {
  if (!sources.length) return null;
  return (
    <section style={s.sourcesSection}>
      <h3 style={s.sourcesHeading}>{heading}</h3>
      <ul style={s.sourcesList}>
        {sources.map((src, i) => (
          // 40 ms stagger — sources streaming in during the gathering phase
          // land one after another, feels like results "landing" rather than
          // all appearing at once.
          <SourceLink key={src.url} source={src} enterDelayMs={Math.min(i, 8) * 40} />
        ))}
      </ul>
    </section>
  );
}

// Note on phase visualization: removed deliberately. A bar visualization
// of phase durations conflates two different signals — "is this phase
// complete?" (binary) and "how long did it take?" (continuous) — and
// every user reads the bar as the first. The single global progress bar
// + status_message + liveness signal already answer the questions a
// running run needs to answer ("is it working", "what is it doing", "is
// it stuck"). `phase_history` continues to be recorded in the entity
// (it's cheap diagnostic data) but is not rendered. If we want a
// post-run timing breakdown later, render it as plain text after
// completion — not as bars.

// ---------------------------------------------------------------------------
// Row
// ---------------------------------------------------------------------------

function RunRow({
  run,
  onOpen,
  onDelete,
  enterDelayMs = 0,
}: {
  run: ResearchRun;
  onOpen: (r: ResearchRun) => void;
  onDelete: (id: string) => void;
  enterDelayMs?: number;
}) {
  const [hover, setHover] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [delHover, setDelHover] = useState(false);
  // `exiting` drives the row's collapse/fade animation before the actual
  // delete call. Keeping the row mounted during the animation means
  // adjacent rows see the height collapse smoothly rather than snapping up.
  const [exiting, setExiting] = useState(false);

  // Tick once per second while this row's run is working — keeps the dot
  // colour in sync with `last_heartbeat_at` between the 2s data poll
  // cycles. No-op on terminal runs (the indicator has nothing to update).
  useTick(run.run_status === "working", 1000);
  const liveness = computeLiveness(run);

  const askDelete = (e: React.MouseEvent) => {
    e.stopPropagation();
    setConfirming(true);
  };
  const confirmDelete = (e: React.MouseEvent) => {
    e.stopPropagation();
    setExiting(true);
    // Must match the keyframe duration in ANIMATION_STYLES (`rs-row-exit`).
    window.setTimeout(() => onDelete(run.id), 220);
  };
  const cancelDelete = (e: React.MouseEvent) => {
    e.stopPropagation();
    setConfirming(false);
  };

  const handleClick = () => {
    if (!confirming && !exiting) onOpen(run);
  };

  return (
    <div
      className={`rs-row rs-row-inner ${exiting ? "rs-exit" : "rs-enter"}`}
      onClick={handleClick}
      onMouseEnter={() => !exiting && setHover(true)}
      onMouseLeave={() => {
        setHover(false);
        setDelHover(false);
      }}
      style={{
        ...s.row,
        ...(hover && !confirming && !exiting ? s.rowHover : null),
        // Stagger entrance for the first several rows so the list composes
        // itself on the page. After position 9 we cap the delay so pages
        // with many rows don't feel sluggish.
        animationDelay: exiting ? undefined : `${enterDelayMs}ms`,
      }}
    >
      <StatusDot
        status={run.run_status}
        glow
        liveness={liveness}
        style={s.statusDotRowPosition}
      />

      <div style={s.rowQuery} title={run.query}>
        {displayTitle(run)}
      </div>

      <div style={s.rowRight} onClick={(e) => confirming && e.stopPropagation()}>
        {confirming ? (
          <span style={s.confirmInline}>
            <span>delete?</span>
            <button type="button" onClick={confirmDelete} style={s.confirmYes}>
              yes
            </button>
            <button type="button" onClick={cancelDelete} style={s.confirmNo}>
              no
            </button>
          </span>
        ) : (
          <button
            type="button"
            onClick={askDelete}
            onMouseEnter={() => setDelHover(true)}
            onMouseLeave={() => setDelHover(false)}
            aria-label="Delete run"
            style={{
              ...s.deleteLink,
              ...(hover ? s.deleteLinkVisible : null),
              ...(delHover ? s.deleteLinkHover : null),
            }}
          >
            delete
          </button>
        )}
      </div>

      <div style={s.rowMeta}>{rowMetaLine(run)}</div>

      {run.run_status === "working" && (
        <div style={s.rowProgressTrack} aria-hidden>
          <div
            className="rs-progress-alive"
            style={{
              ...s.rowProgressFill,
              width: `${Math.min(100, Math.max(0, run.progress))}%`,
            }}
          />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// List view
// ---------------------------------------------------------------------------

function ListView({
  runs,
  loading,
  error,
  onOpen,
  onDelete,
}: {
  runs: ResearchRun[];
  loading: boolean;
  error: string | null;
  onOpen: (r: ResearchRun) => void;
  onDelete: (id: string) => void;
}) {
  const [search, setSearch] = useState("");
  const [searchFocused, setSearchFocused] = useState(false);
  const [page, setPage] = useState(0);

  const filtered = useMemo(() => {
    if (!search.trim()) return runs;
    const needle = search.trim().toLowerCase();
    return runs.filter((r) => r.query.toLowerCase().includes(needle));
  }, [runs, search]);

  useEffect(() => {
    setPage(0);
  }, []);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const clampedPage = Math.min(page, totalPages - 1);
  const start = clampedPage * PAGE_SIZE;
  const end = Math.min(filtered.length, start + PAGE_SIZE);
  const visible = filtered.slice(start, end);

  const countLabel =
    runs.length === 0
      ? ""
      : filtered.length === runs.length
        ? `${runs.length} ${runs.length === 1 ? "run" : "runs"}`
        : `${filtered.length} of ${runs.length}`;

  return (
    <>
      <header className="rs-header" style={s.headerSection}>
        <Column>
          <div style={s.header}>
            <h1 style={s.logo}>Research</h1>
            {countLabel && (
              <span style={s.headerEmDash}>
                <span style={{ marginRight: "0.35em" }}>—</span>
                {countLabel}
              </span>
            )}
            <input
              type="search"
              className="rs-search"
              placeholder="Search"
              value={search}
              onFocus={() => setSearchFocused(true)}
              onBlur={() => setSearchFocused(false)}
              onChange={(e) => {
                setSearch(e.target.value);
                setPage(0);
              }}
              style={{ ...s.search, ...(searchFocused || search ? s.searchFocused : null) }}
            />
          </div>
        </Column>
      </header>

      <main style={s.main}>
        <Column>
          {loading && runs.length === 0 && <div style={s.loading}>Loading…</div>}
          {error && <div style={s.errorText}>Error: {error}</div>}
          {!loading && runs.length === 0 && !error && (
            <div style={s.empty}>Ask the assistant to research something.</div>
          )}
          {!loading && runs.length > 0 && filtered.length === 0 && (
            <div style={s.empty}>No runs match "{search}".</div>
          )}
          {visible.map((run, i) => (
            <RunRow
              key={run.id}
              run={run}
              onOpen={onOpen}
              onDelete={onDelete}
              // 18 ms per row, capped at position 9 so the tail of a page
              // doesn't feel slow to land.
              enterDelayMs={Math.min(i, 9) * 18}
            />
          ))}
        </Column>
      </main>

      {filtered.length > PAGE_SIZE && (
        <footer style={s.paginationSection}>
          <Column>
            <div style={s.pagination}>
              <span style={s.pageCounter}>
                {start + 1}–{end} of {filtered.length}
              </span>
              <span style={s.paginationControls}>
                <button
                  type="button"
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                  disabled={clampedPage === 0}
                  style={{
                    ...s.pageLink,
                    ...(clampedPage === 0 ? s.pageLinkDisabled : null),
                  }}
                >
                  ← Newer
                </button>
                <span style={s.pageCounter}>
                  {clampedPage + 1} / {totalPages}
                </span>
                <button
                  type="button"
                  onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                  disabled={clampedPage >= totalPages - 1}
                  style={{
                    ...s.pageLink,
                    ...(clampedPage >= totalPages - 1 ? s.pageLinkDisabled : null),
                  }}
                >
                  Older →
                </button>
              </span>
            </div>
          </Column>
        </footer>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Detail view
// ---------------------------------------------------------------------------

function DetailView({
  run,
  onBack,
  onDelete,
  onRetry,
  onCancelRetry,
  retrying,
  retryError,
}: {
  run: ResearchRun;
  onBack: () => void;
  onDelete: (id: string) => void;
  onRetry: (query: string) => void;
  onCancelRetry: () => void;
  /** True between `onRetry` firing and the new research_run entity surfacing. */
  retrying: boolean;
  /** Non-null when the retry failed to start (e.g., host rejected the task). */
  retryError: string | null;
}) {
  const [backHover, setBackHover] = useState(false);
  const [delHover, setDelHover] = useState(false);
  const [retryHover, setRetryHover] = useState(false);
  const [confirming, setConfirming] = useState(false);

  // Re-render every second while the run is working so:
  //   1. the staleness counter updates between data poll cycles
  //   2. the running phase's bar grows in real time as the phase elapses
  // No-op once the run is terminal — the timeline + status freeze.
  useTick(run.run_status === "working", 1000);
  const liveness = computeLiveness(run);
  const heartbeatAgeS = run.last_heartbeat_at
    ? Math.max(0, Math.floor((Date.now() - Date.parse(run.last_heartbeat_at)) / 1000))
    : null;

  const isTerminalFailure = run.run_status === "failed" || run.run_status === "cancelled";
  const canRetry = isTerminalFailure && typeof run.query === "string" && run.query.length > 0;

  const handleRetry = useCallback(() => {
    if (!canRetry) return;
    // Second click while "starting…" acts as cancel — the task handle's
    // `cancel()` propagates through the platform to the MCP tool. Until the
    // new entity surfaces the user has only the button to act on, so
    // double-duty it.
    if (retrying) {
      onCancelRetry();
      return;
    }
    onRetry(run.query);
  }, [canRetry, retrying, onRetry, onCancelRetry, run.query]);

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === "Escape") onBack();
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onBack]);

  // Derive the elapsed-time display from `current_phase_started_at`,
  // not from the worker's status_message. The worker writes the phase
  // start once; the UI re-renders every 1s via `useTick` above and the
  // counter ticks smoothly. This is the right separation: stable input
  // (timestamp) → smooth output (relative-time). Skip when terminal,
  // when the field is missing (legacy entities), or when the timestamp
  // doesn't parse — fall back to the bare status_message.
  const phaseElapsedS =
    run.run_status === "working" && run.current_phase_started_at
      ? Math.max(0, Math.floor((Date.now() - Date.parse(run.current_phase_started_at)) / 1000))
      : null;

  const workingLabel = (() => {
    const base = (run.status_message || "working").toLowerCase();
    const parts = [base];
    if (phaseElapsedS !== null) parts.push(`${phaseElapsedS}s`);
    parts.push(`${run.progress}%`);
    return parts.join(" · ");
  })();

  const statusLabel =
    run.run_status === "working"
      ? workingLabel
      : run.run_status === "completed"
        ? formatAge(run.completed_at ?? run.updated_at ?? run.created_at)
        : run.run_status === "failed"
          ? `failed · ${formatAge(run.completed_at ?? run.updated_at ?? run.created_at)}`
          : `cancelled · ${formatAge(run.completed_at ?? run.updated_at ?? run.created_at)}`;

  return (
    <>
      <header style={s.detailHeaderSection}>
        <Column>
          <div style={s.detailHeader}>
            <button
              type="button"
              onClick={onBack}
              onMouseEnter={() => setBackHover(true)}
              onMouseLeave={() => setBackHover(false)}
              style={{ ...s.backLink, ...(backHover ? s.backLinkHover : null) }}
              aria-label="Back to research"
            >
              ← Research
            </button>
            <div style={s.detailDeleteWrap}>
              {confirming ? (
                <span style={s.confirmInline}>
                  <span>delete this?</span>
                  <button
                    type="button"
                    onClick={() => {
                      onDelete(run.id);
                      onBack();
                    }}
                    style={s.confirmYes}
                  >
                    yes
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirming(false)}
                    style={s.confirmNo}
                  >
                    no
                  </button>
                </span>
              ) : (
                <button
                  type="button"
                  onClick={() => setConfirming(true)}
                  onMouseEnter={() => setDelHover(true)}
                  onMouseLeave={() => setDelHover(false)}
                  style={{
                    ...s.deleteLink,
                    ...s.deleteLinkVisible,
                    ...(delHover ? s.deleteLinkHover : null),
                  }}
                  aria-label="Delete run"
                >
                  delete
                </button>
              )}
            </div>
          </div>
        </Column>
      </header>

      <main className="rs-detail-body" style={s.detailBody}>
        <Column>
          {/* Staggered reveal: title first, then status, then progress, then
              the report body. Each element fades up in sequence so the page
              composes itself rather than pop-rendering all at once. Delays
              tuned so total reveal stays under ~500 ms. */}
          <h2 className="rs-enter" style={s.detailTitle}>
            {displayTitle(run)}
          </h2>

          <div
            className="rs-enter"
            style={{ ...s.detailStatusLine, animationDelay: "60ms" }}
          >
            <StatusDot status={run.run_status} glow liveness={liveness} />
            <span>{statusLabel}</span>
          </div>

          {/* Full research brief. Distinct from the heading — the heading
              is a short human label, this is the actual prompt that was
              dispatched to the researcher. Rendered as labeled prose so
              long multi-sentence queries read naturally instead of being
              styled like a wall-sized heading. Hidden when the title and
              query are effectively the same (short query, no separate
              title) — showing both would just duplicate text. */}
          {run.query && run.query.trim() !== displayTitle(run) && (
            <section
              className="rs-enter"
              style={{ ...s.queryBlock, animationDelay: "100ms" }}
            >
              <h3 style={s.queryEyebrow}>Query</h3>
              <p style={s.queryBody}>{run.query}</p>
            </section>
          )}

          {run.run_status === "working" && (
            // Track + fill — mirrors the list row's progress bar structure
            // so the two surfaces feel like the same component at different
            // scales. The fill's motion (breathe + sweep) signals "running
            // now" without needing a separate entry animation.
            <div style={s.detailProgressTrack} aria-hidden>
              <div
                className="rs-progress-alive"
                style={{
                  ...s.detailProgressLine,
                  width: `${Math.min(100, Math.max(0, run.progress))}%`,
                }}
              />
            </div>
          )}

          {/* Staleness indicator — appears ONLY when the heartbeat has
              fallen behind. Its presence is itself the diagnostic signal:
              a healthy run shows nothing here. The text intensifies from
              "last signal Ns" (10–30s) to "no signal for Ns — may be
              stalled" (30s+). The line vanishes the moment a fresh
              heartbeat arrives. */}
          {run.run_status === "working" && liveness !== "live" && heartbeatAgeS !== null && (
            <div
              className="rs-enter"
              style={{
                ...s.stalenessLine,
                ...(liveness === "hung" ? s.stalenessLineHung : null),
              }}
              role="status"
              aria-live="polite"
            >
              {liveness === "hung"
                ? `No signal for ${heartbeatAgeS}s — may be stalled`
                : `Last signal ${heartbeatAgeS}s ago`}
            </div>
          )}

          {run.run_status === "failed" && run.error_message && (
            <div
              className="rs-enter"
              style={{ ...s.errorProse, animationDelay: "120ms" }}
            >
              {run.error_message}
            </div>
          )}

          {/* While still gathering, surface the sources above the (absent)
              report so the user sees work landing in real time. */}
          {run.run_status === "working" &&
            run.sources &&
            run.sources.length > 0 &&
            !run.report && (
              <div
                className="rs-enter"
                style={{ ...s.sourcesPreview, animationDelay: "160ms" }}
              >
                found {run.sources.length}{" "}
                {run.sources.length === 1 ? "source" : "sources"} so far…
              </div>
            )}

          {run.report ? (
            <div className="rs-enter" style={{ animationDelay: "180ms" }}>
              <ReportMarkdown source={run.report} />
            </div>
          ) : (
            <div
              className="rs-enter"
              style={{ ...s.reportEmpty, animationDelay: "180ms" }}
            >
              {run.run_status === "working"
                ? "Report will appear when the run completes."
                : "No report."}
            </div>
          )}

          {canRetry && (
            // Placed at the end of the page for terminal-failure runs so the
            // story reads: error → empty-report note → action. Follows the
            // same bare-text-link pattern as `← Research` and `delete` so
            // no foreign styling intrudes on the reading composition.
            //
            // Dual-channel contract (see CLAUDE.md → "task-augmented tools"):
            //   1. `onRetry` fires `callToolAsTask` which returns a taskId
            //      promptly (<1s).
            //   2. The server creates the new `research_run` entity as its
            //      first action; Synapse's entity sync delivers it to the
            //      list; App.tsx notices the new id and navigates.
            //   3. Between (1) and (2) we show "Starting…" and repurpose the
            //      button as a cancel control so the user isn't stranded if
            //      something wedges.
            <div
              className="rs-enter"
              style={{ ...s.retrySection, animationDelay: "220ms" }}
            >
              <button
                type="button"
                onClick={handleRetry}
                onMouseEnter={() => setRetryHover(true)}
                onMouseLeave={() => setRetryHover(false)}
                style={{
                  ...s.retryLink,
                  ...(retryHover ? s.retryLinkHover : null),
                }}
                aria-label={
                  retrying
                    ? "Cancel retry"
                    : `Retry research with query "${run.query}"`
                }
              >
                {retrying ? "Starting… (click to cancel)" : "↻  Retry with same query"}
              </button>
              {retryError && (
                <div style={s.errorProse} role="alert">
                  {retryError}
                </div>
              )}
            </div>
          )}

          {run.sources && run.sources.length > 0 && run.report && (
            <SourcesSection sources={run.sources} />
          )}

          {/* If still working but sources have arrived, render them live.
              Once the report lands, the block above takes over. */}
          {run.run_status === "working" &&
            !run.report &&
            run.sources &&
            run.sources.length > 0 && (
              <SourcesSection sources={run.sources} heading="Sources (so far)" />
            )}
        </Column>
      </main>
    </>
  );
}

// ---------------------------------------------------------------------------
// Retry flow — dual-channel task-augmented tool call
// ---------------------------------------------------------------------------
//
// The retry button migrates from a fire-and-forget `callTool` to a
// task-augmented `callToolAsTask` per the MCP 2025-11-25 tasks utility
// (https://github.com/NimbleBrainInc/synapse/issues/3). Two channels, in
// lockstep, drive the UI:
//
//   Task channel (MCP tasks/result):
//     `callToolAsTask("start_research", {query})` returns a `CreateTaskResult`
//     in <1s. The tool itself takes 30s–3min — but the taskId arrives
//     promptly, and the resulting handle is what drives the "Starting…"
//     state and the cancel path.
//
//   Entity channel (Upjack useDataSync):
//     The server's `start_research` handler creates the `research_run`
//     entity as its first action (before any GPT-Researcher work). The
//     entity shows up in our `useRuns()` list via `useDataSync`. Navigation
//     to the new run's detail page is driven by this — NOT by the task's
//     terminal result — because the `run_id` lives on the entity, not on
//     the CreateTaskResult. Task result arrives minutes later.
//
// Fallback: older platform deploys that don't advertise
// `tasks.requests.tools.call` cause `callToolAsTask` to throw. We catch that
// specific case and fall back to the old fire-and-forget `callTool`
// behavior so the feature doesn't break against legacy hosts.

function isHostMissingTasksCapability(err: unknown): boolean {
  if (!(err instanceof Error)) return false;
  return err.message.includes("tasks.requests.tools.call");
}

interface RetryState {
  /** True from `start()` call until the new run_id is observed on the entity list. */
  starting: boolean;
  /** Non-null when the retry failed to start. */
  error: string | null;
  /** Kick off a retry for the given query. */
  start(query: string): void;
  /** Cancel a running retry (both task and UI state). */
  cancel(): void;
}

function useRetryFlow(
  runIds: readonly string[],
  onNewRun: (id: string) => void,
): RetryState {
  const synapse = useSynapse();
  const { fire, cancel: cancelTask } = useCallToolAsTask<{ query: string }, { run_id: string }>(
    "start_research",
  );

  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Snapshot the run-id set at the moment `start()` fires so the effect
  // below can diff and detect "a new run has appeared". Storing in a ref
  // (not state) — the set is read-only once fire is called and doesn't
  // need to drive re-renders of its own.
  const baselineIdsRef = useRef<Set<string> | null>(null);

  // Latest runIds in a ref so the navigation effect can diff without
  // closing over a stale snapshot.
  const runIdsRef = useRef<readonly string[]>(runIds);
  runIdsRef.current = runIds;

  // Watch for a new id showing up. When we see one that wasn't in the
  // baseline, navigate to it and exit the "starting" state.
  useEffect(() => {
    if (!starting) return;
    const baseline = baselineIdsRef.current;
    if (!baseline) return;
    const newId = runIds.find((id) => !baseline.has(id));
    if (newId) {
      baselineIdsRef.current = null;
      setStarting(false);
      onNewRun(newId);
    }
  }, [starting, runIds, onNewRun]);

  const start = useCallback(
    (query: string) => {
      setError(null);
      baselineIdsRef.current = new Set(runIdsRef.current);
      setStarting(true);

      // Fire-and-forget at the promise level — the hook drives `task`
      // state internally and the entity-channel effect above drives
      // navigation. We only catch here to surface start-up errors
      // (e.g., host lacks tasks capability → graceful fallback).
      fire({ query }).catch((err: unknown) => {
        if (isHostMissingTasksCapability(err)) {
          // Legacy host: fall back to plain callTool (fire-and-forget).
          // We don't get a task handle but the entity will still appear
          // via useDataSync, so navigation still works.
          void synapse
            .callTool<{ query: string }, unknown>("start_research", { query })
            .catch((fallbackErr: unknown) => {
              baselineIdsRef.current = null;
              setStarting(false);
              setError(
                fallbackErr instanceof Error
                  ? fallbackErr.message
                  : "Failed to start research.",
              );
            });
          return;
        }
        baselineIdsRef.current = null;
        setStarting(false);
        setError(err instanceof Error ? err.message : "Failed to start research.");
      });
    },
    [fire, synapse],
  );

  const cancel = useCallback(() => {
    baselineIdsRef.current = null;
    setStarting(false);
    // No-op if the hook has no active handle (e.g., fallback path). Errors
    // are swallowed by the hook and surface via its own `error` state,
    // which we don't need here — the UI just wants to exit "starting".
    void cancelTask();
  }, [cancelTask]);

  return { starting, error, start, cancel };
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

export default function App() {
  useInjectThemeTokens();
  useEffect(() => {
    injectStyles();
  }, []);

  const { runs, loading, error, deleteRun } = useRuns();
  const [route, navigate] = useRoute();

  const selectedRun = useMemo(() => {
    if (route.view !== "detail") return null;
    return runs.find((r) => r.id === route.id) ?? null;
  }, [route, runs]);

  // Memoize the id list so `useRetryFlow`'s diff effect doesn't re-run on
  // every progress update (entities mutate in place on each tick).
  const runIds = useMemo(() => runs.map((r) => r.id), [runs]);

  const {
    starting: retrying,
    error: retryError,
    start: startRetry,
    cancel: cancelRetry,
  } = useRetryFlow(runIds, (newId) => navigate({ view: "detail", id: newId }));

  useEffect(() => {
    if (route.view === "detail" && !loading && !selectedRun && runs.length > 0) {
      navigate({ view: "list" });
    }
  }, [route, loading, selectedRun, runs.length, navigate]);

  return (
    <div style={s.root}>
      {route.view === "list" && (
        <ListView
          runs={runs}
          loading={loading}
          error={error}
          onOpen={(r) => navigate({ view: "detail", id: r.id })}
          onDelete={deleteRun}
        />
      )}
      {route.view === "detail" && selectedRun && (
        <DetailView
          run={selectedRun}
          onBack={() => navigate({ view: "list" })}
          onDelete={deleteRun}
          onRetry={startRetry}
          onCancelRetry={cancelRetry}
          retrying={retrying}
          retryError={retryError}
        />
      )}
      {route.view === "detail" && !selectedRun && loading && (
        <Column>
          <div style={s.loading}>Loading…</div>
        </Column>
      )}
    </div>
  );
}
