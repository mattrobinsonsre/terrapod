// Which bulk run actions a selection can take. The server's answer (`actions`) decides;
// this adds only the lock and the workspace's declared operations.

import { isCompositeRun, isConfigureOnlyRun, isPlanApplyRun } from './run-kinds.ts'

export type BulkActionId = 'plan' | 'retry' | 'cancel' | 'apply' | 'discard'

export interface BulkRunActions {
  confirmable: boolean
  discardable: boolean
  cancelable: boolean
  retryable: boolean
}

/**
 * The workspace's published `permissions` flags, as the list returns them.
 *
 * These answer "may THIS USER do it", which is a different question from
 * `BulkRunActions` above ("is the run in a state where it is possible"). The
 * menu needs both, and read only the second until #2579: a read-only user was
 * offered Apply on any `planned` run and got a 403 on press. The server's own
 * comment beside `can-queue-apply` names that failure mode (#1340).
 */
export interface BulkPermissions {
  canQueueRun: boolean
  canQueueApply: boolean
  canQueueDestroy: boolean
}

export interface BulkWorkspace {
  id: string
  name: string
  locked: boolean
  planApplyEnabled: boolean
  configureEnabled: boolean
  /** All-false when the API does not publish them: a control nobody can press is worse. */
  permissions: BulkPermissions
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

/**
 * Whether the user MAY take `action`, mirroring the server's own producers.
 *
 * Each arm names the function it mirrors, because the mapping is not
 * guessable and getting it wrong fails in two opposite directions -- hiding a
 * capability from someone who holds it, or offering a control that 403s. The
 * server stays authoritative either way; this only decides what to offer.
 *
 * The one approximation is deliberate and is Matt's call (#2621): `cancel` and
 * `discard` both require `run:cancel`, for which NO flag is published, so
 * `can-queue-run` answers for it -- exact for a role level, since `run:cancel`
 * and `run:plan` sit in the same `plan` tier, and approximate for a
 * hand-written role that lists one without the other. The cost of that case is
 * a button offered to someone the server then refuses, which is where every
 * selected row already was before this change.
 */
function permitted(action: BulkActionId, w: BulkWorkspace): boolean {
  const p = w.permissions
  const run = w.latestRun
  const kind = { runKind: run?.runKind ?? undefined }
  switch (action) {
    case 'plan':
      // create_run_capability(plan_only=true, ...) -> RUN_PLAN
      return p.canQueueRun
    case 'cancel':
    case 'discard':
      // cancel_run / discard_run both require RUN_CANCEL. See the note above.
      return p.canQueueRun
    case 'apply':
      // confirm_capability(is_destroy) -> RUN_APPLY_DESTROY if destroy else RUN_APPLY
      return run?.isDestroy === true ? p.canQueueDestroy : p.canQueueApply
    case 'retry':
      // create_run_capability(plan_only, is_destroy, is_configure), whose arms
      // are tested in that order -- is_configure first, and a configure is
      // apply-equivalent at the write tier with no destroy analogue.
      if (isConfigureOnlyRun(kind) || isCompositeRun(kind)) return p.canQueueApply
      if (run?.planOnly === true) return p.canQueueRun
      return run?.isDestroy === true ? p.canQueueDestroy : p.canQueueApply
  }
}

function qualifies(action: BulkActionId, w: BulkWorkspace): boolean {
  // Permission first, and once rather than per arm: a `switch` with five cases
  // is exactly where one arm comes to be missing the check.
  if (!permitted(action, w)) return false
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
