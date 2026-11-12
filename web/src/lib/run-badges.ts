/**
 * Which type badges a run gets in the run list (#2507).
 *
 * A configure-only run was badged **plan + apply** *and* **configure**, as if
 * it were a composite, because the cell derived the kind from three attributes
 * and had no case for a run with no plan/apply half:
 *
 *     is-destroy ? destroy : plan-only ? plan only : "plan + apply"   // default
 *     is-configure && configure                                       // additive
 *
 * A configure-only run is neither a destroy nor plan-only, so it fell through
 * to the default. Both badges together say Terraform is changing
 * infrastructure, which is exactly what a configure does not do.
 *
 * The server now answers the question directly -- `run-kind` is `plan_apply`,
 * `configure` or `full` -- so this reads one attribute instead of inferring,
 * and the run page's default view (#2509) reads the same one. Three copies of a
 * three-attribute predicate is how they came to disagree, and no gate can see
 * that.
 *
 * ## Why `destroy` and `plan only` are still their own attributes
 *
 * They are not kinds. A run is a destroy or speculative *as well as* being a
 * plan/apply run, so they are modifiers of the plan/apply half rather than
 * alternatives to it -- which is why the server does not fold them into
 * `run-kind` either (`run_service.run_kind` says so). This picks WHICH
 * plan/apply badge to show; whether there is one at all is the kind's answer.
 *
 * Deliberately NOT in `run-kinds.ts`: that module answers which run kinds a
 * WORKSPACE offers as buttons, which is a different question about a different
 * object, and a module that answers two questions is one nobody can name.
 */

/** The attributes this reads. Structural, so the page's `Run` satisfies it. */
export interface RunBadgeInput {
  /** `plan_apply` | `configure` | `full`, or absent on an older server. */
  runKind?: string
  isDestroy?: boolean
  planOnly?: boolean
  /** Pre-`run-kind` fallback only; see `primaryRunBadge`. */
  isConfigure?: boolean
}

/**
 * The plan/apply badge to render, or `null` when the run has no plan/apply
 * half at all and the configure badge should stand alone.
 */
export function primaryRunBadge(
  run: RunBadgeInput,
): 'destroy' | 'planOnly' | 'planApply' | null {
  if (run.runKind === 'configure') return null
  if (run.isDestroy) return 'destroy'
  if (run.planOnly) return 'planOnly'
  return 'planApply'
}

/**
 * Whether the run is a COMPOSITE -- an apply followed by a configure.
 *
 * The one remaining question that used to be answered by reading
 * `configure-after-apply` off the run: a composite's configure badge explains
 * itself differently from a standalone one's, because what it does differs.
 * Reading the kind instead is what lets the run document's two flags stop
 * being read by any page at all.
 *
 * `false` where the server sends no kind, which is the right failure: the
 * hint then describes the standalone case, and a standalone configure is the
 * run somebody is likelier to be looking at on a configure-only workspace.
 */
export function isCompositeRun(run: RunBadgeInput): boolean {
  return run.runKind === 'full'
}

/**
 * Whether the configure badge is shown.
 *
 * Reads `run-kind` where the server sends it and falls back to `is-configure`
 * where it does not, so a build pointed at an older server keeps the badge it
 * has always had rather than silently losing it -- the half of a version skew
 * that must not degrade, because this badge is the only thing on the row that
 * says a playbook ran against real hosts.
 */
export function showsConfigureBadge(run: RunBadgeInput): boolean {
  if (run.runKind !== undefined) {
    return run.runKind === 'configure' || run.runKind === 'full'
  }
  return Boolean(run.isConfigure)
}
