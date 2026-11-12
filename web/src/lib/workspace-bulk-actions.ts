// Which bulk run actions a selection of workspaces can take.
//
// Every action acts on a workspace's LATEST run, which the workspace list already
// carries, except Plan, which queues a new run. An action is enabled only when
// EVERY selected row qualifies: a partial batch is hard to reason about, and the
// menu says how many qualify instead of hiding the reason in a tooltip.
//
// The eligibility rules mirror the server (`run_service.py`): retry needs a
// terminal run, cancel an in-progress one (`CANCELABLE_STATES`), and apply and
// discard a `planned` run that is not plan-only. The server stays authoritative:
// each call can still be refused, and the result says so per workspace.

export type BulkActionId = 'plan' | 'retry' | 'cancel' | 'apply' | 'discard'

export interface BulkWorkspace {
  id: string
  name: string
  locked: boolean
  /** The workspace's latest run, as the list returns it; null when it has none. */
  latestRun: { id: string; status: string; planOnly: boolean } | null
}

export interface BulkAction {
  id: BulkActionId
  /** True for the irreversible tier: prompts in BOTH pointer modes. */
  confirm: boolean
}

export const BULK_ACTIONS: readonly BulkAction[] = [
  { id: 'plan', confirm: false },
  { id: 'retry', confirm: false },
  { id: 'cancel', confirm: false },
  { id: 'apply', confirm: true },
  { id: 'discard', confirm: true },
]

const TERMINAL = new Set(['applied', 'errored', 'discarded', 'canceled'])
const CANCELABLE = new Set(['pending', 'queued', 'planning', 'confirmed', 'applying', 'configuring'])

function qualifies(action: BulkActionId, w: BulkWorkspace): boolean {
  const run = w.latestRun
  switch (action) {
    case 'plan':
      // A new run, so no existing run is needed. A manual lock blocks an apply,
      // not a plan: the server queues a plan on a locked workspace.
      return true
    case 'retry':
      // A plan-only run that has planned is finished, as the server treats it.
      return !!run && (TERMINAL.has(run.status) || (run.planOnly && run.status === 'planned'))
    case 'cancel':
      return !!run && CANCELABLE.has(run.status)
    case 'apply':
      // A manual lock blocks an apply on the server.
      return !!run && run.status === 'planned' && !run.planOnly && !w.locked
    case 'discard':
      return !!run && run.status === 'planned' && !run.planOnly
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
