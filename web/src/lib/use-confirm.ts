import { useIsTouch } from './use-media-query'

/**
 * The #719 two-tier confirm() policy as a hook (see AGENTS.md → Responsive →
 * Touch model). `confirmDelete` prompts in BOTH modes (irreversible deletes);
 * `confirmTouchMutation` prompts on touch only (other single-tap mutations).
 * Both return true to proceed / false to abort.
 */
export function useConfirm() {
  const isTouch = useIsTouch()
  return {
    confirmDelete: (message: string) => window.confirm(message),
    confirmTouchMutation: (message: string) => !isTouch || window.confirm(message),
  }
}
