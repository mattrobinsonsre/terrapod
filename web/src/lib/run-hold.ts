/**
 * A run held after its plan, in either of the API's vocabularies (#1704, #1725).
 *
 * When a mandatory run task, a mandatory policy set, an enforced security scan
 * or a mandatory AI policy gate stops a run, the API reports it one of two
 * ways. The 1.x vocabulary
 * keeps `status: planning` and names the gate in `blocked-by`; the Terraform
 * Enterprise vocabulary reports `post_plan_running`,
 * `post_plan_awaiting_decision` or `policy_override`, and still sets
 * `blocked-by`. The page must behave the same either way, so logic reads the
 * phase (`phaseOf`) and the display reads the gate (`gateOf`).
 */

export type Gate = 'run-task' | 'policy' | 'security-scan' | 'ai-policy'

const TFE_HELD = new Set(['post_plan_running', 'post_plan_awaiting_decision', 'policy_override'])
const GATES = new Set<string>(['run-task', 'policy', 'security-scan', 'ai-policy'])

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
 *
 * The AI policy gate splits the same way, and it is the one gate where waiting
 * is routine rather than exceptional: its verdict is produced in the API after
 * the plan lands, so a mandatory gate holds the run until it arrives. Saying
 * "awaiting your decision" then would be wrong — there is nothing yet to
 * decide. `post_plan_running` is the API telling us which of the two it is.
 */
export function holdActivityKey(gate: Gate, status: string): string {
  if (gate === 'policy') return 'heldByPolicy'
  if (gate === 'security-scan') return 'heldBySecurityScan'
  if (gate === 'ai-policy') {
    return status === 'post_plan_running' ? 'awaitingAiPolicy' : 'heldByAiPolicy'
  }
  return status === 'post_plan_awaiting_decision' ? 'heldByRunTask' : 'runTasksRunning'
}
