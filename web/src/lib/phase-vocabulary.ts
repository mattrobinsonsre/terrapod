/**
 * Per-engine display vocabulary for run phases (#1407 §11, #1521).
 *
 * Internal state names never change — a run is `planning` whatever engine it
 * belongs to, and every API, every state machine and every log key says so. What
 * a *person* is shown differs, because the engines do genuinely different things:
 *
 *   internal            terraform     pulumi      ansible
 *   planning/planned    plan          preview     check
 *   applying/applied    apply         up          run
 *
 * #1407 §3 is explicit that this difference must stay visible in the API and the
 * UI "not smoothed over". Rendering "Running terraform plan" for a Pulumi preview
 * is exactly the smoothing-over it forbids — so the words come from a namespace
 * chosen by the run's engine rather than from a literal.
 *
 * The engine picks a *key namespace*, never a string: these have to survive
 * translation into every offered locale, so the engine cannot hold English.
 */

/** The engine a run or workspace belongs to, as the API reports it. */
export type Engine = string

/**
 * Terraform, matching the column default. Used when a record predates the
 * column or an older API does not send it — the same answer the database would
 * give, rather than a blank label.
 */
export const DEFAULT_ENGINE = 'terraform'

/**
 * Engines whose vocabulary the message catalogues carry.
 *
 * An engine absent here falls back to Terraform's words rather than rendering a
 * raw key. That is deliberate: a missing translation should look like slightly
 * wrong wording, not like a broken page — and the i18n completeness gate is what
 * stops it staying wrong.
 */
const KNOWN = new Set([DEFAULT_ENGINE])

export function vocabularyFor(engine: Engine | null | undefined): string {
  const key = (engine ?? '').trim().toLowerCase()
  return KNOWN.has(key) ? key : DEFAULT_ENGINE
}

/**
 * Build the message key for a phase word.
 *
 * `group` selects which flavour of the word is wanted — the short status label,
 * the longer activity line, or the workspace-list badge — because the same phase
 * reads differently in each place.
 */
export function phaseKey(
  engine: Engine | null | undefined,
  group: 'runStatus' | 'activity' | 'status',
  state: string,
): string {
  return `phases.${vocabularyFor(engine)}.${group}.${state}`
}
