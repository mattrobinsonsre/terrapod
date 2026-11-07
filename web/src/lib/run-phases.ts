/**
 * Which phase explains a run's outcome (#2413 F16).
 *
 * A result indicator should carry you to what explains it: clicking `errored`
 * on a workspace should land you on the log of the phase that errored, not on
 * an overview you then have to navigate from. The maintainer's framing — *"this
 * is important in general for ergonomics"* — is why this is a derivation rather
 * than a special case for one status.
 *
 * ## No server attribute is needed, and the obvious rule is a defect
 *
 * `_run_json` already emits every phase boundary on the native surface
 * (`plan-queued-at`, `planning-at`, `planned-at`, `applying-at`, `applied-at`,
 * and natively `configuring-at` / `configured-at`), and the run LIST carries
 * them too — so this is computable client-side with nothing added.
 *
 * The rule is **the last phase whose START timestamp is present**, and it has
 * to be the start rather than a missing end. Measured against real runs:
 *
 * | n | status | last started | ends present |
 * |---|---|---|---|
 * | 6 | `planned` | `planning` | `planned-at` |
 * | 1 | `applied` | `configuring` | `applied-at`, `configured-at` |
 * | 3 | **`errored`** | `configuring` | **`applied-at`, `configured-at`** |
 *
 * **An errored configure carries `configured-at`.** So "the phase whose END
 * timestamp is missing" — the first rule anyone writes — identifies nothing on
 * three of three errored runs, because `configure_finished_at` is set when the
 * configure finishes whether or not it succeeded.
 *
 * Two shapes the sample did not contain are correct by construction rather than
 * by measurement, and that distinction is deliberate: a run that errored in its
 * plan never entered `applying`, so `applying-at` is absent; the same holds for
 * a plan-only run.
 *
 * ## The configure-only shape
 *
 * A standalone configure has **no `planning-at` at all** and still reports
 * `applied-at`, because that key is `configure_finished_at or apply_finished_at`
 * (#1988). A derivation that assumed a plan had run would mis-read every
 * configure-only run, which is why this walks the phases backwards rather than
 * forwards from `plan`.
 */

/** The phase views the run page serves, in the order a run passes through them. */
export type RunPhase = 'plan' | 'apply' | 'configure'

/** Ordered newest-last, so the search below reads as "the furthest it got". */
const PHASE_START: ReadonlyArray<readonly [RunPhase, string]> = [
  ['plan', 'planning-at'],
  ['apply', 'applying-at'],
  ['configure', 'configuring-at'],
]

/**
 * The phase whose log explains this run, or `null` when it never started one.
 *
 * `null` is a real answer, not a fallback: a `queued` or `pending` run has no
 * phase to show, and linking it to a plan log that does not exist yet would be
 * worse than not linking it.
 */
export function explainingPhase(
  statusTimestamps: Record<string, unknown> | null | undefined,
): RunPhase | null {
  if (!statusTimestamps) return null
  // Backwards: the LAST phase that started is the one that explains the result.
  for (let i = PHASE_START.length - 1; i >= 0; i--) {
    const [phase, key] = PHASE_START[i]
    // The server omits a falsy timestamp (`if v`), so presence is the signal —
    // but an empty string would still be falsy here, which is the same test.
    if (statusTimestamps[key]) return phase
  }
  return null
}

/** The run link, pointed at the view that explains the status wherever we can
 *  tell which that is (#2413 F16).
 *
 *  `?view=` is the run page's own deep-link parameter, already accepting
 *  eleven views -- so this needs no new mechanism, only a caller. Falls back
 *  to the bare run link rather than guessing: a wrong view is worse than the
 *  page's default, because it asserts that the phase it opened is the one that
 *  explains the outcome. */
export function runHref(
  workspaceId: string,
  runId: string,
  phaseStarts?: Record<string, unknown> | null,
): string {
  const base = `/workspaces/${workspaceId}/runs/${runId}`
  const phase = explainingPhase(phaseStarts)
  return phase ? `${base}?view=${phase}` : base
}
