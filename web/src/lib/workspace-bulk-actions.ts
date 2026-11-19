// Which bulk run actions a selection can take. The server's answer (`actions`) decides;
// this adds only the lock and the workspace's declared operations.

import { isCompositeRun, isConfigureOnlyRun, isPlanApplyRun } from './run-badges.ts'

export type BulkActionId = 'plan' | 'retry' | 'cancel' | 'apply' | 'discard'

export interface BulkRunActions {
  confirmable: boolean
  discardable: boolean
  cancelable: boolean
  retryable: boolean
}

export interface BulkWorkspace {
  id: string
  name: string
  locked: boolean
  planApplyEnabled: boolean
  configureEnabled: boolean
  /** The latest run as the list returns it; `actions` is null from an older API. */
  latestRun: { id: string; runKind: string | null; planOnly?: boolean; isDestroy?: boolean; actions: BulkRunActions | null } | null
}

export interface BulkAction {
  id: BulkActionId
  /** `irreversible` prompts in both pointer modes; `touch` only on a coarse pointer. */
  confirm: 'irreversible' | 'touch'
}

// Retry's tier here is the cautious default; `confirmTier` relaxes it when nothing can apply.
export const BULK_ACTIONS: readonly BulkAction[] = [
  { id: 'plan', confirm: 'touch' },
  { id: 'retry', confirm: 'irreversible' },
  { id: 'cancel', confirm: 'touch' },
  { id: 'apply', confirm: 'irreversible' },
  { id: 'discard', confirm: 'irreversible' },
]

// A retry repeats the run's own plan-only flag, so a plan-only plan/apply run applies nothing.
// A destroy, or a run that does not say it is not one, is never read-only.
const readOnlyRetry = (w: BulkWorkspace) =>
  w.latestRun?.planOnly === true &&
  w.latestRun.isDestroy === false &&
  isPlanApplyRun({ runKind: w.latestRun.runKind ?? undefined })

/** Which pointers confirm `action`: Retry is touch-only only when every selected retry is read-only. */
export function confirmTier(action: BulkActionId, selection: readonly BulkWorkspace[]): BulkAction['confirm'] {
  const tier = BULK_ACTIONS.find((a) => a.id === action)?.confirm ?? 'irreversible'
  if (action !== 'retry') return tier
  return selection.length > 0 && selection.every(readOnlyRetry) ? 'touch' : tier
}

function qualifies(action: BulkActionId, w: BulkWorkspace): boolean {
  const run = w.latestRun
  const actions = run?.actions
  switch (action) {
    case 'plan':
      return w.planApplyEnabled
    case 'retry': {
      if (!actions?.retryable) return false
      // The server retries with the workspace's current settings; a lock refuses a configure.
      const kind = { runKind: run?.runKind ?? undefined }
      if (isConfigureOnlyRun(kind)) return w.configureEnabled && !w.locked
      if (isCompositeRun(kind)) return w.planApplyEnabled && w.configureEnabled && !w.locked
      return w.planApplyEnabled
    }
    case 'cancel':
      return !!actions?.cancelable
    case 'apply':
      return !!actions?.confirmable && !w.locked
    case 'discard':
      return !!actions?.discardable
  }
}

export function eligibleFor(action: BulkActionId, selection: readonly BulkWorkspace[]): BulkWorkspace[] {
  return selection.filter((w) => qualifies(action, w))
}

export interface BulkSummary {
  eligible: number
  total: number
  /** True only when every selected row qualifies and something is selected. */
  enabled: boolean
}

export function summarise(action: BulkActionId, selection: readonly BulkWorkspace[]): BulkSummary {
  const eligible = eligibleFor(action, selection).length
  const total = selection.length
  return { eligible, total, enabled: total > 0 && eligible === total }
}
