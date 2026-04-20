import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useDataSync, useSynapse } from "@nimblebrain/synapse/react";
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
  run_status: RunStatus;
  progress: number;
  status_message?: string | null;
  report?: string | null;
  error_message?: string | null;
  sources?: Source[];
  started_at?: string;
  completed_at?: string | null;
  created_at: string;
  updated_at: string;
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
  style,
}: {
  status: RunStatus;
  size?: number;
  glow?: boolean;
  /** Layout overrides for the call site's container (grid / flex). The dot's
   *  own visual styling lives below — callers should only pass positioning. */
  style?: React.CSSProperties;
}) {
  const isAlive = glow && status === "working";
  // `rs-pulse` drives both the ring (box-shadow) and a subtle scale. When
  // reduced-motion is on, `rs-dot-alive-static` provides a non-animated glow
  // fallback so the state remains legible. Both are CSS classes — no inline
  // box-shadow, which would beat the keyframe's specificity and flatten the
  // animation to a static ring.
  const className = isAlive ? "rs-pulse rs-dot-alive-static" : undefined;
  return (
    <span
      aria-label={status}
      className={className}
      style={{
        width: size,
        height: size,
        borderRadius: 999,
        flexShrink: 0,
        transition: "box-shadow 240ms ease",
        background: STATUS_COLORS[status],
        ...style,
      }}
    />
  );
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
    case "working":
      return run.status_message
        ? `${run.status_message.toLowerCase()} · ${run.progress}%`
        : `working · ${run.progress}%`;
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
      <StatusDot status={run.run_status} glow style={s.statusDotRowPosition} />

      <div style={s.rowQuery}>{run.query}</div>

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
}: {
  run: ResearchRun;
  onBack: () => void;
  onDelete: (id: string) => void;
  onRetry: (query: string) => Promise<void>;
}) {
  const [backHover, setBackHover] = useState(false);
  const [delHover, setDelHover] = useState(false);
  const [retryHover, setRetryHover] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const isTerminalFailure = run.run_status === "failed" || run.run_status === "cancelled";
  const canRetry = isTerminalFailure && typeof run.query === "string" && run.query.length > 0;

  const handleRetry = useCallback(async () => {
    if (!canRetry || retrying) return;
    setRetrying(true);
    try {
      await onRetry(run.query);
    } finally {
      setRetrying(false);
    }
  }, [canRetry, retrying, onRetry, run.query]);

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === "Escape") onBack();
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onBack]);

  const statusLabel =
    run.run_status === "working"
      ? run.status_message
        ? `${run.status_message.toLowerCase()} · ${run.progress}%`
        : `working · ${run.progress}%`
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
            {run.query}
          </h2>

          <div
            className="rs-enter"
            style={{ ...s.detailStatusLine, animationDelay: "60ms" }}
          >
            <StatusDot status={run.run_status} glow />
            <span>{statusLabel}</span>
          </div>

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
            <div
              className="rs-enter"
              style={{ ...s.retrySection, animationDelay: "220ms" }}
            >
              <button
                type="button"
                onClick={handleRetry}
                onMouseEnter={() => setRetryHover(true)}
                onMouseLeave={() => setRetryHover(false)}
                disabled={retrying}
                style={{
                  ...s.retryLink,
                  ...(retryHover && !retrying ? s.retryLinkHover : null),
                  ...(retrying ? s.retryLinkDisabled : null),
                }}
                aria-label={`Retry research with query "${run.query}"`}
              >
                {retrying ? "Starting retry…" : "↻  Retry with same query"}
              </button>
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
// App
// ---------------------------------------------------------------------------

export default function App() {
  useInjectThemeTokens();
  useEffect(() => {
    injectStyles();
  }, []);

  const { runs, loading, error, deleteRun } = useRuns();
  const synapse = useSynapse();
  const [route, navigate] = useRoute();

  const selectedRun = useMemo(() => {
    if (route.view !== "detail") return null;
    return runs.find((r) => r.id === route.id) ?? null;
  }, [route, runs]);

  // Retry: fire start_research and navigate to the list. The new run surfaces
  // at the top via useDataSync; one extra click to open it.
  //
  // We do NOT await the callTool Promise — Synapse's `callTool` resolves only
  // when the tool reaches its terminal state, and `start_research` is
  // task-augmented (30s–3min). Awaiting would freeze the UI on the failed
  // page until completion. When https://github.com/NimbleBrainInc/synapse/issues/3
  // lands (task-aware callTool that returns CreateTaskResult early), we can
  // upgrade to navigate straight to the new run's detail page.
  const handleRetry = useCallback(
    async (query: string) => {
      void synapse.callTool("start_research", { query });
      navigate({ view: "list" });
    },
    [synapse, navigate],
  );

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
          onRetry={handleRetry}
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
