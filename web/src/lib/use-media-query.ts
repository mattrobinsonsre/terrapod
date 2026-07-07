import { useCallback, useSyncExternalStore } from 'react'

/**
 * SSR-safe viewport/container adaptation primitive.
 *
 * The UI is one DRY implementation that adapts on **actual available
 * width** — never on the user agent, and never as two forked component
 * trees (#719). CSS is the primary tool: prefer Tailwind responsive
 * utilities (`sm:`/`md:`/`lg:`) and CSS container queries (`@container`),
 * which need no JS and cannot cause a hydration mismatch. Reach for this
 * hook ONLY where behaviour (not just styling) must branch on width — e.g.
 * mounting a bottom-sheet vs an inline panel.
 *
 * SSR safety: the server has no viewport, so the hook returns `false` on
 * the first (server + initial client) render and corrects after mount.
 * Because CSS handles the visual adaptation, that first frame is already
 * laid out correctly; the hook only flips JS-level behaviour a tick later,
 * so there is no hydration mismatch and no layout flash for CSS-driven UI.
 */
export function useMediaQuery(query: string): boolean {
  // useSyncExternalStore is the hydration-safe way to read an external
  // (browser) value: the server snapshot is always `false`, the client
  // subscribes to matchMedia. No cascading renders, no hydration mismatch.
  const subscribe = useCallback(
    (onStoreChange: () => void) => {
      const mql = window.matchMedia(query)
      mql.addEventListener('change', onStoreChange)
      return () => mql.removeEventListener('change', onStoreChange)
    },
    [query],
  )
  const getSnapshot = () => window.matchMedia(query).matches
  const getServerSnapshot = () => false
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)
}

// Breakpoints mirror Tailwind's defaults so JS branches align with the CSS
// utilities used everywhere else. "Mobile" is below `md` (the same boundary
// the nav switches at).
export const BREAKPOINTS = {
  sm: 640,
  md: 768,
  lg: 1024,
  xl: 1280,
} as const

/**
 * True when the viewport is narrower than the `md` breakpoint (the phone /
 * small-tablet range). Keyed to the same 768px boundary as Tailwind's `md:`
 * and the nav's mobile switch, so CSS and JS agree.
 */
export function useIsMobile(): boolean {
  return useMediaQuery(`(max-width: ${BREAKPOINTS.md - 0.02}px)`)
}
