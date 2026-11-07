import { useMemo } from 'react'

import { useIsTouch } from './use-media-query'

/**
 * The #719 two-tier confirm() policy as a hook (see AGENTS.md → Responsive →
 * Touch model). Use across every surface with mutating actions so the two tiers
 * stay identical everywhere.
 *
 * **The axis is REVERSIBILITY, not whether the word "delete" appears.** That
 * was implicit while the only both-modes callers were deletes, and it stopped
 * being true once a configure and an outstanding-hosts retry needed the same
 * treatment -- neither is a delete and both change real machines. So:
 *
 *   - `confirmIrreversible(msg)` — the action cannot be undone. Prompts in BOTH
 *     modes: losing something on a stray click is a desktop hazard too.
 *   - `confirmDelete(msg)` — the same tier, kept because a delete is the
 *     commonest instance and reads better at the call site.
 *   - `confirmTouchMutation(msg)` — any other single-tap mutation (toggle,
 *     lock, enable/disable, …). Prompts on touch ONLY, where a mis-tap is easy;
 *     a precise pointer proceeds, because there is time to cancel and the
 *     reader is not a child.
 *
 * All three return `true` to proceed / `false` to abort, so the caller does
 * `if (!confirmDelete('Delete X?')) return`.
 *
 * Form *submits* after deliberate data entry (create/edit Save) are not
 * single-tap mutations and don't need a guard.
 *
 * **This is the only place in `web/src` that may call `window.confirm`**, and
 * `web/tests/confirm-policy.test.ts` enforces it (#2413 F13). Before that gate
 * the hook existed and was used by eleven files, while thirteen others
 * hand-rolled one of the two tiers inline -- `isTouch && !window.confirm(...)`
 * nine times over. Every copy agreed at the time, which is exactly the state
 * that precedes drift: one producer, or the tiers diverge surface by surface.
 */
export function useConfirm() {
  const isTouch = useIsTouch()
  // Memoised on `isTouch` so the three functions keep a stable identity across
  // renders. Several callers list their guard in a `useCallback` dependency
  // array, and a fresh arrow per render would invalidate the callback every
  // time -- quietly turning a memoised handler into an unmemoised one.
  return useMemo(() => {
    const both = (message: string) => window.confirm(message)
    return {
      confirmIrreversible: both,
      confirmDelete: both,
      confirmTouchMutation: (message: string) => !isTouch || window.confirm(message),
    }
  }, [isTouch])
}
