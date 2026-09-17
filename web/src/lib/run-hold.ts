/**
 * A run held after its plan, in either of the API's vocabularies (#1704, #1725).
 *
 * When a mandatory run task, a mandatory policy set or an enforced security
 * scan stops a run, the API reports it one of two ways. The 1.x vocabulary
 * keeps `status: planning` and names the gate in `blocked-by`; the Terraform
 * Enterprise vocabulary reports `post_plan_running`,
 * `post_plan_awaiting_decision` or `policy_override`, and still sets
 * `blocked-by`. The page must behave the same either way, so logic reads the
 * phase (`phaseOf`) and the display reads the gate (`gateOf`).
 */

export type Gate = 'run-task' | 'policy' | 'security-scan'

const TFE_HELD = new Set(['post_plan_running', 'post_plan_awaiting_decision', 'policy_override'])
const GATES = new Set<string>(['run-task', 'policy', 'security-scan'])

/** The run's phase whichever vocabulary reported it: a held run is `planning`. */
export function phaseOf(status: string): string {
  return TFE_HELD.has(status) ? 'planning' : status
}

/** The gate holding the run, or null when nothing holds it. */
export function gateOf(attributes: { 'blocked-by'?: unknown }): Gate | null {
  const value = attributes['blocked-by']
  return typeof value === 'string' && GATES.has(value) ? (value as Gate) : null
}

/**
 * What to tell a person about a held run, as a `runDetail.activity` key.
 *
 * A run-task hold is either tasks still running or a failed mandatory task. The
 * Terraform Enterprise vocabulary says which; the 1.x one only ever holds a run
 * while its tasks run, because a failed task errors the run there.
 */
export function holdActivityKey(gate: Gate, status: string): string {
  if (gate === 'policy') return 'heldByPolicy'
  if (gate === 'security-scan') return 'heldBySecurityScan'
  return status === 'post_plan_awaiting_decision' ? 'heldByRunTask' : 'runTasksRunning'
}
