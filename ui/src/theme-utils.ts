import { useEffect } from "react";
import { useTheme } from "@nimblebrain/synapse/react";

/**
 * Copies the host's theme tokens onto :root as CSS custom properties on every
 * change. Stylesheets and inline styles then consume values via var(--token).
 * Synapse re-emits the full token map when the host flips modes, so dark-mode
 * support is automatic — no data-attribute toggling required.
 *
 * Pattern mirrored from synapse-collateral/ui/src/theme-utils.ts.
 */
export function useInjectThemeTokens(): {
  mode: "light" | "dark";
  accent: string;
} {
  const theme = useTheme();

  useEffect(() => {
    const root = document.documentElement;
    for (const [key, value] of Object.entries(theme.tokens)) {
      root.style.setProperty(key, value);
    }
    root.style.colorScheme = theme.mode;
  }, [theme.tokens, theme.mode]);

  return {
    mode: theme.mode,
    accent: theme.tokens["--color-text-accent"] || theme.primaryColor || "#0055FF",
  };
}
