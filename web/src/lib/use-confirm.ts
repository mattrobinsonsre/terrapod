import { useIsTouch } from './use-media-query'

/**
 * The #719 two-tier confirm() policy as a hook (see AGENTS.md → Responsive →
 * Touch model). Use across every surface with mutating actions so the two tiers
 * stay identical everywhere:
 *
 *   - `confirmDelete(msg)` — an irreversible delete/remove. Prompts in BOTH
 *     modes (touch and precise pointer): losing data on a stray click is a
 *     desktop hazard too.
 *   - `confirmTouchMutation(msg)` — any other single-tap mutation (toggle,
 *     lock, enable/disable, …). Prompts on touch ONLY, where a mis-tap is easy;
 *     a precise pointer proceeds.
 *
 * Both return `true` to proceed / `false` to abort, so the caller does
 * `if (!confirmDelete('Delete X?')) return`.
 *
 * Form *submits* after deliberate data entry (create/edit Save) are not
 * single-tap mutations and don't need a guard.
 */
export function useConfirm() {
  const isTouch = useIsTouch()
  return {
    confirmDelete: (message: string) => window.confirm(message),
    confirmTouchMutation: (message: string) => !isTouch || window.confirm(message),
  }
}
