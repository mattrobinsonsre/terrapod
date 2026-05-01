// Single source of truth for workspace status definitions.
//
// Both the workspace-row status pill and the status filter dropdown derive
// their lists from `WORKSPACE_STATUSES`. Adding a new status only requires
// editing this file — `resolveStatus` produces the right pill, the dropdown
// auto-renders the new option, and the kebab-case `filter` value is
// authoritative for `status:value` predicates in the workspace filter.
//
// Order matters: `resolveStatus` walks workspace-level conditions first
// (`state-diverged`, `vcs-error`, `drifted`) then run statuses; the filter
// dropdown shows the same order so the most operationally interesting
// statuses surface at the top.

export type StatusColor = 'red' | 'amber' | 'blue' | 'green' | 'slate' | 'gray'

export interface WorkspaceStatusDef {
  /** Kebab-case lowercase token used in the `status:value` filter syntax. */
  filter: string
  /** Display label for the pill / dropdown row. */
  label: string
  /** Logical colour family for the pill background. */
  color: StatusColor
  /** Tailwind class for the small coloured dot in dropdown rows. */
  dot: string
  /** Sort priority for the workspace list (lower = more urgent). */
  priority: number
  /** True when the status is derived from workspace-level signals
   *  (state-diverged, vcs-error, drifted) rather than from a run. The pill
   *  for a workspace-level status doesn't link to any specific run. */
  isWorkspaceLevel?: boolean
}

export const WORKSPACE_STATUSES: ReadonlyArray<WorkspaceStatusDef> = [
  // Workspace-level conditions — most urgent.
  { filter: 'state-diverged', label: 'State Diverged', color: 'red', dot: 'bg-red-400', priority: 0, isWorkspaceLevel: true },
  { filter: 'vcs-error', label: 'VCS Error', color: 'red', dot: 'bg-red-400', priority: 0, isWorkspaceLevel: true },
  { filter: 'drifted', label: 'Drifted', color: 'amber', dot: 'bg-amber-400', priority: 1, isWorkspaceLevel: true },
  // Run-level statuses, in workflow order.
  { filter: 'errored', label: 'Errored', color: 'red', dot: 'bg-red-400', priority: 2 },
  { filter: 'needs-confirm', label: 'Needs Confirm', color: 'amber', dot: 'bg-amber-400', priority: 3 },
  { filter: 'planning', label: 'Planning', color: 'blue', dot: 'bg-blue-400', priority: 4 },
  { filter: 'applying', label: 'Applying', color: 'blue', dot: 'bg-blue-400', priority: 4 },
  { filter: 'confirmed', label: 'Confirmed', color: 'blue', dot: 'bg-blue-400', priority: 4 },
  { filter: 'queued', label: 'Queued', color: 'blue', dot: 'bg-blue-400', priority: 4 },
  { filter: 'pending', label: 'Pending', color: 'slate', dot: 'bg-slate-500', priority: 5 },
  { filter: 'applied', label: 'Applied', color: 'green', dot: 'bg-green-400', priority: 6 },
  { filter: 'planned', label: 'Planned', color: 'green', dot: 'bg-green-400', priority: 7 },
  { filter: 'canceled', label: 'Canceled', color: 'slate', dot: 'bg-slate-500', priority: 8 },
  { filter: 'discarded', label: 'Discarded', color: 'slate', dot: 'bg-slate-500', priority: 8 },
]

const STATUS_BY_FILTER: ReadonlyMap<string, WorkspaceStatusDef> = new Map(
  WORKSPACE_STATUSES.map(s => [s.filter, s] as const),
)

interface ResolveInput {
  attributes: {
    'state-diverged': boolean
    'vcs-last-error': string | null
    'drift-status': string
    'latest-run': {
      id: string
      status: string
      'plan-only': boolean
    } | null
  }
}

export interface ResolvedStatus {
  /** Status definition, or null if no run / no signals. */
  def: WorkspaceStatusDef | null
  /** Run id for run-level statuses; null for workspace-level conditions. */
  runId: string | null
}

/** Resolve a workspace's effective status from its signals.
 *
 * Workspace-level conditions (`state-diverged`, `vcs-error`, `drifted`)
 * take precedence over run statuses — they reflect divergence between the
 * desired state and reality, which is more urgent than any in-flight run.
 *
 * Returns `def: null, runId: null` when there's no run and no condition
 * (typically a freshly-created workspace with no plan yet).
 */
export function resolveStatus(ws: ResolveInput): ResolvedStatus {
  const a = ws.attributes
  if (a['state-diverged']) return { def: STATUS_BY_FILTER.get('state-diverged') ?? null, runId: null }
  if (a['vcs-last-error']) return { def: STATUS_BY_FILTER.get('vcs-error') ?? null, runId: null }
  if (a['drift-status'] === 'drifted') return { def: STATUS_BY_FILTER.get('drifted') ?? null, runId: null }
  const run = a['latest-run']
  if (!run) return { def: null, runId: null }
  const planOnly = run['plan-only']
  // Map run.status → status filter token. `planned` is special: a non
  // plan-only planned run is awaiting confirmation; a plan-only one is
  // a successful read of the world.
  let filter: string
  switch (run.status) {
    case 'planned':
      filter = planOnly ? 'planned' : 'needs-confirm'
      break
    default:
      filter = run.status
  }
  return { def: STATUS_BY_FILTER.get(filter) ?? null, runId: run.id }
}
