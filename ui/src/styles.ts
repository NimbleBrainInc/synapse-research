import type { CSSProperties } from "react";

// Design tokens resolve from CSS variables the host injects (see
// theme-utils.ts). Each var() falls back to a sensible light-mode default so
// static/standalone renders still look sane.

const FONT_SANS =
  "var(--font-sans, 'Inter', system-ui, -apple-system, BlinkMacSystemFont, sans-serif)";
const FONT_HEADING =
  "var(--nb-font-heading, 'Erode', Georgia, 'Times New Roman', serif)";

const TEXT_XS = "var(--font-text-xs-size, 0.75rem)";
const TEXT_XS_LH = "var(--font-text-xs-line-height, 1rem)";
const TEXT_SM = "var(--font-text-sm-size, 0.875rem)";
const TEXT_SM_LH = "var(--font-text-sm-line-height, 1.25rem)";
const TEXT_BASE = "var(--font-text-base-size, 1rem)";
const TEXT_BASE_LH = "var(--font-text-base-line-height, 1.5rem)";
const HEADING_LG = "var(--font-heading-lg-size, 1.5rem)";
const HEADING_LG_LH = "var(--font-heading-lg-line-height, 2rem)";

const WEIGHT_NORMAL = "var(--font-weight-normal, 400)";
const WEIGHT_MEDIUM = "var(--font-weight-medium, 500)";
const WEIGHT_SEMIBOLD = "var(--font-weight-semibold, 600)";

const BG_PRIMARY = "var(--color-background-primary, #faf9f7)";
const TEXT_PRIMARY = "var(--color-text-primary, #171717)";
const TEXT_SECONDARY = "var(--color-text-secondary, #737373)";
const TEXT_ACCENT = "var(--color-text-accent, #0055FF)";
const DANGER = "var(--nb-color-danger, #dc2626)";

// Status colors — filled dots only; no badges, no fills elsewhere.
export const STATUS_COLORS = {
  working: "var(--nb-color-info, #3b82f6)",
  completed: "var(--nb-color-success, #10b981)",
  failed: DANGER,
  cancelled: TEXT_SECONDARY,
} as const;

// The single column width for the reading composition. Chosen to feel like a
// magazine page — wide enough for long queries on one line, narrow enough that
// meta stays near the title it describes. Shared between list, pagination,
// and detail so the three views feel like variations of one layout.
const COLUMN_MAX = 720;

// Responsive — injected once by App. Below 640 px the column collapses to
// the iframe width; horizontal breathing room is reduced proportionally.
export const RESPONSIVE_STYLES = `
@media (max-width: 640px) {
  .rs-column-pad {
    padding-left: 1rem !important;
    padding-right: 1rem !important;
  }
  .rs-row-inner {
    padding-left: 0.875rem !important;
    padding-right: 0.875rem !important;
  }
  .rs-header {
    padding-top: 1.5rem !important;
  }
  .rs-search {
    min-width: 0 !important;
    max-width: 40% !important;
  }
  .rs-detail-body {
    padding-top: 1.25rem !important;
    padding-bottom: 3rem !important;
  }
  .rs-report {
    font-size: ${TEXT_SM} !important;
  }
}
`;

// Motion — keyframes + utility classes for entrances, exits, and the living
// "working" signal. Kept in one global stylesheet so inline styles can
// reference them by name without bundling a motion library.
//
// Motion language:
//   • 280 ms ease-out (`cubic-bezier(0.2, 0, 0, 1)`) for entrances — the slow
//     ease-out "arriving" curve used by Linear/iOS.
//   • 200 ms ease-in for exits — faster, more decisive.
//   • 2.4 s breathe for the working dot — slow enough to feel alive, not
//     distracting.
//
// Everything is gated on `prefers-reduced-motion: reduce` so users who opt
// out get instant rendering.
export const ANIMATION_STYLES = `
@keyframes rs-fade-up {
  from { opacity: 0; transform: translateY(4px); }
  to   { opacity: 1; transform: translateY(0); }
}

@keyframes rs-row-exit {
  0%   {
    opacity: 1;
    transform: translateX(0);
    max-height: 96px;
    padding-top: 0.75rem;
    padding-bottom: 0.75rem;
  }
  55%  {
    opacity: 0;
    transform: translateX(-6px);
  }
  100% {
    opacity: 0;
    transform: translateX(-6px);
    max-height: 0;
    padding-top: 0;
    padding-bottom: 0;
    margin-top: 0;
    margin-bottom: 0;
  }
}

/* Ripple pulse on the "working" status dot — a clear alive signal. The ring
   starts tight and opaque, expands outward while fading. Pairs visual motion
   (scale) with color motion (ring) so the dot reads as alive at a glance
   rather than being merely colored. */
@keyframes rs-pulse {
  0% {
    box-shadow: 0 0 0 0 rgba(59, 130, 246, 0.45);
    transform: scale(1);
  }
  70% {
    box-shadow: 0 0 0 9px rgba(59, 130, 246, 0);
    transform: scale(1.18);
  }
  100% {
    box-shadow: 0 0 0 0 rgba(59, 130, 246, 0);
    transform: scale(1);
  }
}

/* A breathing pulse for active progress lines — the indicator reads as
   "alive" rather than static. Range 0.75→1.0 so the bar stays clearly
   visible at the trough rather than fading out. */
@keyframes rs-progress-breathe {
  0%, 100% { opacity: 0.75; }
  50%      { opacity: 1.0; }
}

/* Complementary traveling highlight: a brighter sliver sweeps across the
   filled portion left-to-right. Uses a repeating gradient anchored to the
   fill's width so the sweep stays inside the progress region. */
@keyframes rs-progress-sweep {
  0%   { background-position: -200% 0; }
  100% { background-position: 200% 0; }
}

.rs-enter {
  animation: rs-fade-up 280ms cubic-bezier(0.2, 0, 0, 1) both;
}

.rs-exit {
  animation: rs-row-exit 220ms cubic-bezier(0.4, 0, 1, 1) forwards;
  pointer-events: none;
  overflow: hidden;
}

.rs-pulse {
  /* Faster cycle (1.6 s) reads as "alive", slower feels lethargic. The
     animation drives both box-shadow (ring) and transform (scale); the
     status dot's background color still comes from inline style. */
  animation: rs-pulse 1.6s cubic-bezier(0.4, 0, 0.6, 1) infinite;
}

/* Static fallback glow for the alive dot when motion is disabled. Applied as
   a CSS class (not inline) so when the animation IS active, the keyframe's
   box-shadow takes precedence on animated frames. */
.rs-dot-alive-static {
  box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2);
}

/* Active progress line — applied to the fill element while a run is working.
   Composes two motions: a breath that modulates overall opacity, and a
   bright highlight that sweeps across the fill. Tuned so the effect reads
   on both light and dark backgrounds without being flashy. */
.rs-progress-alive {
  background-image: linear-gradient(
    90deg,
    rgba(255, 255, 255, 0) 0%,
    rgba(255, 255, 255, 0.75) 50%,
    rgba(255, 255, 255, 0) 100%
  );
  background-size: 60% 100%;
  background-repeat: no-repeat;
  animation:
    rs-progress-breathe 1.4s ease-in-out infinite,
    rs-progress-sweep 1.6s linear infinite;
}

@media (prefers-reduced-motion: reduce) {
  .rs-enter,
  .rs-exit,
  .rs-pulse,
  .rs-progress-alive {
    animation: none !important;
  }
  .rs-progress-alive {
    background-image: none !important;
  }
}
`;

export const s: Record<string, CSSProperties> = {
  // ---- Root (fills iframe) ----
  root: {
    display: "flex",
    flexDirection: "column",
    height: "100vh",
    overflow: "hidden",
    fontFamily: FONT_SANS,
    fontSize: TEXT_BASE,
    lineHeight: TEXT_BASE_LH,
    color: TEXT_PRIMARY,
    background: BG_PRIMARY,
  },

  // ---- The reading column — centered, 720 px max, used by every section ----
  column: {
    maxWidth: COLUMN_MAX,
    marginLeft: "auto",
    marginRight: "auto",
    width: "100%",
    boxSizing: "border-box",
  },
  columnPad: {
    paddingLeft: "1.25rem",
    paddingRight: "1.25rem",
  },

  // ---- List header (no border, no chrome) ----
  headerSection: {
    flexShrink: 0,
    paddingTop: "2.5rem",
    paddingBottom: "0.5rem",
  },
  header: {
    display: "flex",
    alignItems: "baseline",
    gap: "0.75rem",
    minHeight: HEADING_LG_LH,
  },
  logo: {
    fontFamily: FONT_HEADING,
    fontWeight: WEIGHT_SEMIBOLD,
    fontSize: HEADING_LG,
    lineHeight: HEADING_LG_LH,
    letterSpacing: "-0.018em",
    color: TEXT_PRIMARY,
    margin: 0,
  },
  headerEmDash: {
    fontSize: TEXT_SM,
    lineHeight: TEXT_SM_LH,
    color: TEXT_SECONDARY,
    fontWeight: WEIGHT_NORMAL,
    letterSpacing: "0.01em",
    // Visually balances the large heading; sits a hair below the baseline so
    // the em-dash reads as editorial metadata rather than a list separator.
    position: "relative",
    top: "-0.05em",
  },

  // Search as an underline-only field. Border-bottom is transparent at rest so
  // the input is invisible; it reveals on focus or when it has content.
  search: {
    marginLeft: "auto",
    padding: "0.3rem 0.1rem",
    border: "none",
    borderBottom: "1px solid transparent",
    background: "transparent",
    color: TEXT_PRIMARY,
    fontSize: TEXT_SM,
    lineHeight: TEXT_SM_LH,
    fontFamily: "inherit",
    outline: "none",
    minWidth: 200,
    maxWidth: 260,
    letterSpacing: "0.01em",
    transition: "border-color 160ms ease",
  },
  searchFocused: {
    borderBottomColor: TEXT_ACCENT,
  },

  // ---- Main (scroll container) ----
  main: {
    flex: 1,
    overflowY: "auto",
    // Small top-pad decouples first row from header visually — more generous
    // than a divider, less chrome.
    paddingTop: "0.75rem",
    paddingBottom: "1rem",
  },
  empty: {
    padding: "4.5rem 1.25rem",
    textAlign: "center",
    color: TEXT_SECONDARY,
    fontSize: TEXT_SM,
    lineHeight: TEXT_SM_LH,
    fontStyle: "italic",
  },
  loading: {
    padding: "2rem 1.25rem",
    color: TEXT_SECONDARY,
    fontSize: TEXT_SM,
  },
  errorText: {
    padding: "2rem 1.25rem",
    color: DANGER,
    fontSize: TEXT_SM,
  },

  // ---- Row (rounded hover tint, no borders) ----
  row: {
    position: "relative",
    padding: "0.75rem 1.25rem",
    cursor: "pointer",
    display: "grid",
    gridTemplateColumns: "10px 1fr auto",
    columnGap: "0.875rem",
    alignItems: "baseline",
    transition: "background 140ms ease",
    background: "transparent",
    // Subtle rounding on the hover tint — the only "chrome" allowed, and only
    // while hovered. Keeps the resting state perfectly flat.
    borderRadius: 6,
  },
  rowHover: {
    background: "rgba(0, 85, 255, 0.045)",
  },
  // Layout-only positioning for the StatusDot when it sits in the list row's
  // grid cell next to multi-line text. The dot's visual styling (size, color,
  // pulse animation, reduced-motion fallback) lives inside the StatusDot
  // component so both the list row and the detail view render identically.
  statusDotRowPosition: {
    marginTop: "0.35rem",
    alignSelf: "start",
    justifySelf: "center",
  },
  rowQuery: {
    fontWeight: WEIGHT_MEDIUM,
    fontSize: TEXT_BASE,
    lineHeight: TEXT_BASE_LH,
    color: TEXT_PRIMARY,
    minWidth: 0,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    letterSpacing: "-0.005em",
  },
  rowRight: {
    display: "inline-flex",
    alignItems: "center",
    gap: "0.75rem",
    fontSize: TEXT_XS,
    color: TEXT_SECONDARY,
    whiteSpace: "nowrap",
  },
  rowMeta: {
    gridColumn: "2",
    marginTop: "0.25rem",
    fontSize: TEXT_XS,
    lineHeight: TEXT_XS_LH,
    color: TEXT_SECONDARY,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    letterSpacing: "0.005em",
  },

  // Progress bar at the bottom of a working row. Sits inside a faint track so
  // the fill reads as a measured line rather than a floating stripe.
  rowProgressTrack: {
    position: "absolute",
    left: "1.25rem",
    right: "1.25rem",
    bottom: "0.35rem",
    height: 3,
    background: "rgba(59, 130, 246, 0.1)",
    pointerEvents: "none",
    borderRadius: 2,
    overflow: "hidden",
  },
  rowProgressFill: {
    height: "100%",
    background: STATUS_COLORS.working,
    opacity: 0.9,
    transition: "width 600ms ease",
    borderRadius: 2,
    boxShadow: "0 0 6px rgba(59, 130, 246, 0.45)",
  },

  // ---- Delete affordance: text-link, hidden until hover ----
  deleteLink: {
    border: "none",
    background: "transparent",
    color: TEXT_SECONDARY,
    cursor: "pointer",
    fontFamily: "inherit",
    fontSize: TEXT_XS,
    padding: 0,
    letterSpacing: "0.02em",
    opacity: 0,
    transition: "opacity 140ms ease, color 140ms ease",
  },
  deleteLinkVisible: {
    opacity: 0.55,
  },
  deleteLinkHover: {
    color: DANGER,
    opacity: 1,
  },
  confirmInline: {
    display: "inline-flex",
    gap: "0.55rem",
    alignItems: "baseline",
    fontSize: TEXT_XS,
    color: TEXT_SECONDARY,
    letterSpacing: "0.01em",
  },
  confirmYes: {
    border: "none",
    background: "transparent",
    color: DANGER,
    cursor: "pointer",
    fontFamily: "inherit",
    fontSize: TEXT_XS,
    padding: 0,
    textDecoration: "underline",
    textDecorationThickness: "1px",
    textUnderlineOffset: "3px",
    fontWeight: WEIGHT_MEDIUM,
  },
  confirmNo: {
    border: "none",
    background: "transparent",
    color: TEXT_SECONDARY,
    cursor: "pointer",
    fontFamily: "inherit",
    fontSize: TEXT_XS,
    padding: 0,
    textDecoration: "underline",
    textDecorationThickness: "1px",
    textUnderlineOffset: "3px",
  },

  // ---- Pagination: text links, no chrome ----
  paginationSection: {
    flexShrink: 0,
    paddingTop: "0.75rem",
    paddingBottom: "1.5rem",
  },
  pagination: {
    display: "flex",
    alignItems: "baseline",
    justifyContent: "space-between",
    gap: "1rem",
    fontSize: TEXT_XS,
    color: TEXT_SECONDARY,
    letterSpacing: "0.02em",
  },
  paginationControls: {
    display: "inline-flex",
    alignItems: "baseline",
    gap: "1rem",
  },
  pageLink: {
    border: "none",
    background: "transparent",
    color: TEXT_PRIMARY,
    cursor: "pointer",
    fontFamily: "inherit",
    fontSize: TEXT_XS,
    padding: 0,
    transition: "color 120ms ease",
  },
  pageLinkDisabled: {
    color: TEXT_SECONDARY,
    opacity: 0.4,
    cursor: "default",
  },
  pageCounter: {
    color: TEXT_SECONDARY,
    fontVariantNumeric: "tabular-nums",
  },

  // ---- Detail page ----
  detailHeaderSection: {
    flexShrink: 0,
    paddingTop: "1.5rem",
    paddingBottom: "0.25rem",
  },
  detailHeader: {
    display: "flex",
    alignItems: "baseline",
    gap: "1rem",
    minHeight: "1.5rem",
  },
  backLink: {
    border: "none",
    background: "transparent",
    color: TEXT_SECONDARY,
    cursor: "pointer",
    fontFamily: "inherit",
    fontSize: TEXT_SM,
    padding: 0,
    letterSpacing: "0.005em",
    transition: "color 140ms ease",
  },
  backLinkHover: {
    color: TEXT_PRIMARY,
  },

  detailBody: {
    flex: 1,
    overflowY: "auto",
    paddingTop: "2rem",
    paddingBottom: "4rem",
  },
  detailTitle: {
    fontFamily: FONT_HEADING,
    fontSize: HEADING_LG,
    lineHeight: HEADING_LG_LH,
    fontWeight: WEIGHT_SEMIBOLD,
    color: TEXT_PRIMARY,
    margin: "0 0 0.75rem",
    letterSpacing: "-0.018em",
    wordWrap: "break-word",
  },
  detailStatusLine: {
    display: "flex",
    alignItems: "center",
    gap: "0.5rem",
    marginBottom: "1.75rem",
    fontSize: TEXT_SM,
    lineHeight: TEXT_SM_LH,
    color: TEXT_SECONDARY,
    letterSpacing: "0.005em",
  },
  // Retry affordance on terminal-failure runs (failed / cancelled). Text
  // link, same visual language as `deleteLink` and `backLink` — no pill,
  // no fill. Fires `start_research` with the original query; the old entity
  // stays on disk as an audit trail.
  retrySection: {
    marginTop: "1.25rem",
  },
  retryLink: {
    display: "inline-flex",
    alignItems: "center",
    gap: "0.35rem",
    border: "none",
    background: "transparent",
    color: TEXT_SECONDARY,
    cursor: "pointer",
    fontFamily: "inherit",
    fontSize: TEXT_XS,
    padding: 0,
    letterSpacing: "0.02em",
    opacity: 0.7,
    transition: "opacity 140ms ease, color 140ms ease",
  },
  retryLinkHover: {
    color: TEXT_ACCENT,
    opacity: 1,
  },
  retryLinkDisabled: {
    opacity: 0.4,
    cursor: "wait",
  },

  // Detail progress — a clearly visible progress bar on a faint track. Height
  // and opacity tuned so it reads as "running now" at a glance rather than
  // requiring the user to look for it.
  detailProgressTrack: {
    height: 3,
    background: "rgba(59, 130, 246, 0.1)",
    marginTop: "-1rem",
    marginBottom: "2rem",
    borderRadius: 2,
    overflow: "hidden",
  },
  detailProgressLine: {
    height: "100%",
    background: STATUS_COLORS.working,
    opacity: 0.9,
    transition: "width 600ms ease",
    borderRadius: 2,
    boxShadow: "0 0 6px rgba(59, 130, 246, 0.45)",
  },

  errorProse: {
    marginBottom: "2rem",
    color: DANGER,
    fontSize: TEXT_SM,
    lineHeight: TEXT_SM_LH,
    whiteSpace: "pre-wrap",
    fontStyle: "italic",
    letterSpacing: "0.005em",
  },

  // Report — container for the rendered markdown. Individual elements are
  // styled below via the `md*` keys so react-markdown can map tag → style.
  report: {
    fontFamily: "inherit",
    fontSize: TEXT_BASE,
    lineHeight: 1.72,
    margin: 0,
    color: TEXT_PRIMARY,
    letterSpacing: "-0.002em",
  },
  reportEmpty: {
    color: TEXT_SECONDARY,
    fontSize: TEXT_SM,
    lineHeight: TEXT_SM_LH,
    fontStyle: "italic",
  },

  // ---- Markdown element styles (react-markdown `components`) ----
  mdH1: {
    fontFamily: FONT_HEADING,
    fontSize: HEADING_LG,
    lineHeight: HEADING_LG_LH,
    fontWeight: WEIGHT_SEMIBOLD,
    letterSpacing: "-0.018em",
    margin: "2rem 0 0.75rem",
    color: TEXT_PRIMARY,
  },
  mdH2: {
    fontFamily: FONT_HEADING,
    fontSize: "1.2rem",
    lineHeight: 1.4,
    fontWeight: WEIGHT_SEMIBOLD,
    letterSpacing: "-0.012em",
    margin: "1.75rem 0 0.5rem",
    color: TEXT_PRIMARY,
  },
  mdH3: {
    fontSize: TEXT_BASE,
    lineHeight: TEXT_BASE_LH,
    fontWeight: WEIGHT_SEMIBOLD,
    margin: "1.5rem 0 0.35rem",
    color: TEXT_PRIMARY,
    letterSpacing: "-0.005em",
  },
  mdP: {
    margin: "0 0 1rem",
  },
  mdBlockquote: {
    margin: "0 0 1rem",
    paddingLeft: "1rem",
    borderLeft: `2px solid ${TEXT_ACCENT}`,
    color: TEXT_SECONDARY,
    fontStyle: "italic",
  },
  mdUl: {
    margin: "0 0 1rem",
    paddingLeft: "1.25rem",
  },
  mdOl: {
    margin: "0 0 1rem",
    paddingLeft: "1.25rem",
  },
  mdLi: {
    margin: "0.25rem 0",
    lineHeight: 1.65,
  },
  mdA: {
    color: TEXT_ACCENT,
    textDecoration: "underline",
    textDecorationThickness: "1px",
    textUnderlineOffset: "2px",
  },
  mdCodeInline: {
    fontFamily:
      "var(--font-mono, 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace)",
    fontSize: "0.88em",
    padding: "0.08rem 0.32rem",
    borderRadius: 3,
    background: "rgba(0, 0, 0, 0.06)",
    color: TEXT_PRIMARY,
  },
  mdPre: {
    fontFamily:
      "var(--font-mono, 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace)",
    fontSize: TEXT_SM,
    lineHeight: 1.55,
    margin: "0 0 1.25rem",
    padding: "0.875rem 1rem",
    background: "rgba(0, 0, 0, 0.035)",
    borderRadius: 4,
    overflowX: "auto",
    whiteSpace: "pre",
    color: TEXT_PRIMARY,
  },
  mdStrong: {
    fontWeight: WEIGHT_SEMIBOLD,
  },
  mdEm: {
    fontStyle: "italic",
  },
  mdHr: {
    border: "none",
    borderTop: `1px solid rgba(0,0,0,0.08)`,
    margin: "2rem 0",
  },

  // ---- Sources section (detail view) ----
  sourcesSection: {
    marginTop: "2.5rem",
  },
  sourcesHeading: {
    fontFamily: FONT_HEADING,
    fontSize: "0.95rem",
    fontWeight: WEIGHT_SEMIBOLD,
    letterSpacing: "0.04em",
    textTransform: "uppercase",
    color: TEXT_SECONDARY,
    margin: "0 0 1rem",
  },
  sourcesList: {
    listStyle: "none",
    padding: 0,
    margin: 0,
    display: "flex",
    flexDirection: "column",
    gap: "1rem",
  },
  sourceItem: {
    display: "flex",
    flexDirection: "column",
    gap: "0.25rem",
  },
  sourceTitle: {
    fontSize: TEXT_SM,
    lineHeight: TEXT_SM_LH,
    fontWeight: WEIGHT_MEDIUM,
    color: TEXT_PRIMARY,
    textDecoration: "none",
    letterSpacing: "-0.005em",
  },
  sourceTitleHover: {
    color: TEXT_ACCENT,
    textDecoration: "underline",
    textDecorationThickness: "1px",
    textUnderlineOffset: "2px",
  },
  sourceUrl: {
    fontFamily:
      "var(--font-mono, 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace)",
    fontSize: "0.72rem",
    lineHeight: 1.3,
    color: TEXT_SECONDARY,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    letterSpacing: "0.01em",
  },
  sourceSnippet: {
    fontSize: TEXT_XS,
    lineHeight: 1.5,
    color: TEXT_SECONDARY,
    marginTop: "0.2rem",
  },

  // Inline "sources so far" preview (while still gathering)
  sourcesPreview: {
    marginBottom: "2rem",
    fontSize: TEXT_XS,
    lineHeight: TEXT_XS_LH,
    color: TEXT_SECONDARY,
    fontStyle: "italic",
  },

  // Detail: delete link in the header row
  detailDeleteWrap: {
    marginLeft: "auto",
  },
};
