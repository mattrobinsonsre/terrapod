import type { JSX } from 'react'

/** Every status the server can report for a pool or a listener.
 *
 * Pools: `online | degraded | offline` (`routers/agent_pools.py` ≈:207-211).
 * Listeners: `online | cert-expired | offline` (≈:177).
 *
 * Exhaustive on purpose. The two hand-written copies this replaced each ended
 * with a catch-all `return 'bg-slate-500'`, so everything that was not
 * `online` or `degraded` rendered as the same grey — offline, cert-expired and
 * anything added later, indistinguishable from one another and from "we do not
 * know" (#2446). The two copies had also already drifted: one learned
 * `cert-expired` and the other never did.
 */
export type EntityStatus = 'online' | 'degraded' | 'cert-expired' | 'offline'

interface StatusDotProps {
  status: string | undefined
  /** The already-translated status text. Becomes the accessible name, so the
   *  dot is never a colour-only signal for a screen reader.
   *
   *  It does NOT discharge WCAG 1.4.11 for a sighted user: an accessible name
   *  is not a visual presentation. Every call site must also render this text
   *  VISIBLY beside the dot, which `tests/status-dot-pairing.test.ts` asserts
   *  — see the note on `FILL` for why the dot cannot carry state on its own. */
  label: string
  className?: string
}

/* Why the 500 stops and not 400.
 *
 * `globals.css`'s light block REMAPS the chromatic scales for text use: on
 * light, `green-400` is `#0d542b` (Tailwind's green-900, a dark forest green),
 * `amber-400` is `#7b3306` (brown) and `red-400` is `#82181a` (maroon). That
 * inversion is right for a text colour on a pale ground and wrong for a FILL —
 * and a status dot is a fill, which is why the green LED read as dark and muddy
 * rather than lit.
 *
 * The 500 stops map to vivid fills on light (`#00a63e`, `#e7000b`, `#e17100`)
 * and are bright enough on dark, so one class works in both themes. Do not
 * "brighten" these by moving to a lower number: on light that makes them
 * darker.
 *
 * And do not chase WCAG 1.4.11's 3:1 by moving to a HIGHER number either. It
 * cannot be reached: measured against both themes' own surfaces, the 500 row
 * clears 3:1 on dark and fails on light (online 2.64, degraded 2.62,
 * cert-expired 2.37) while the 600 row does the exact reverse (online 2.96,
 * degraded 2.91, offline 2.28 on dark). The light ladder sits at luminance
 * .75-.95 and the dark at .01-.05, so a single mid-chroma fill is squeezed from
 * both ends — there is no stop that passes both, and a per-theme remap of
 * `*-500` would move every other consumer of those stops with it.
 *
 * So the dot is deliberately NOT the carrier of state: each call site renders
 * the status word beside it, and 1.4.11 does not bind on a graphic that is not
 * required to understand the content. The 500 row is then chosen purely for hue
 * legibility, which is what a status LED is for.
 */
const FILL: Record<EntityStatus, string> = {
  online: 'bg-green-500',
  // Some listeners in the pool are up, so it is not offline — but it is not well.
  degraded: 'bg-amber-500',
  // Present and unable to authenticate. A different cause from offline and a
  // different fix, so it must not share a colour with it.
  'cert-expired': 'bg-orange-500',
  offline: 'bg-red-500',
}

/**
 * The status LED for an agent pool or a listener.
 *
 * An unrecognised status renders deliberately oddly — a hollow ring rather than
 * a dot — instead of silently borrowing the grey that used to mean "offline".
 * A new server-side status should look wrong here, not look fine and mean
 * something else.
 */
export function StatusDot({ status, label, className }: StatusDotProps): JSX.Element {
  const fill = status && status in FILL ? FILL[status as EntityStatus] : undefined
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className={
        'w-2 h-2 rounded-full flex-shrink-0 ' +
        (fill ?? 'border border-slate-400 bg-transparent') +
        (className ? ' ' + className : '')
      }
    />
  )
}
