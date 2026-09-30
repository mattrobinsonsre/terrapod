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
const KNOWN = new Set([DEFAULT_ENGINE, 'pulumi'])

export function vocabularyFor(engine: Engine | null | undefined): string {
  const key = (engine ?? '').trim().toLowerCase()
  return KNOWN.has(key) ? key : DEFAULT_ENGINE
}

/**
 * Build the message key for a phase word.
 *
 * `group` selects which flavour of the word is wanted — the short status label,
 * the longer activity line, the workspace-list badge, or one of the many other
 * places the same run is described — because the same phase reads differently
 * in each.
 */
export function phaseKey(
  engine: Engine | null | undefined,
  group: 'runStatus' | 'activity' | 'status' | 'words',
  state: string,
): string {
  return `phases.${vocabularyFor(engine)}.${group}.${state}`
}

/**
 * Run statuses that name a *phase*, and so belong to the engine's vocabulary.
 *
 * Everything else a run can be — pending, queued, confirmed, errored, canceled,
 * discarded — is the platform's own and reads the same whatever engine produced
 * it. Only these four have a per-engine word, so only these four are redirected.
 */
export const PHASE_STATUSES = new Set(['planning', 'planned', 'applying', 'applied'])

/**
 * The engine's word for a label, button, heading, empty state or confirm prompt
 * that names a phase.
 *
 * `runStatus`/`activity`/`status` cover the four phase states themselves. The
 * `words` group covers everywhere *else* the same run is described — "Plan +
 * apply", "Plan log", "Confirm & Apply", "Discard this plan?", "plan only".
 * Those were filed under `runDetail.*` / `workspaceDetail.runs.*` as Terraform
 * wording with no engine dimension at all, which is how a Pulumi run came to
 * show "Previewed" in its status card and "Planned" in the pill beside it: one
 * surface had been given the engine's vocabulary and a dozen had not.
 *
 * Each engine holds the FULL string rather than composing one from a shared
 * noun, because "{plan} complete" cannot be translated — the agreement of the
 * rest of the sentence depends on the word substituted into it, and an ICU
 * placeholder cannot express that.
 *
 * @param t a ROOT `useTranslations()` with no namespace — these keys live at the
 *          top level, not inside whichever namespace the caller opened.
 */
export function engineWord(
  t: (key: string) => string,
  engine: Engine | null | undefined,
  name: string,
): string {
  return t(phaseKey(engine, 'words', name))
}
