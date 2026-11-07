/**
 * Which run kinds a workspace offers, most complete first (#2413 F12).
 *
 * In `lib/` rather than beside the component because that is where this tree
 * puts logic a test can import: every file in `web/tests/` imports from
 * `src/lib/*.ts` and none imports a `.tsx`, since the runner is bare
 * `node --test` with no transpiler.
 *
 * The ORDER is the whole output. The caller renders the first as the primary
 * button and the rest behind a caret, so "which is primary" is decided here,
 * once, from the workspace and the caller's own capabilities — never remembered
 * from the last press.
 *
 * ## Absent versus blocked, which are different and look alike
 *
 * A kind the workspace does not DECLARE, or the caller may not run, is ABSENT.
 * That is the existing treatment of a configure — *"data-gated: a workspace
 * that declares no configure operation shows no button"* — and of a capability,
 * where showing a button the API refuses is the #1634 defect. It is also the
 * other half of #2413 F20's split: engine/permission applicability hides, a
 * gate disables.
 *
 * A kind that is declared and permitted but cannot run RIGHT NOW is present and
 * BLOCKED, with the reason. Today there is one: a non-default `vcs-ref` makes
 * the server force the run plan-only, so offering an apply would promise one
 * and quietly deliver a plan.
 *
 * `locked` is deliberately NOT here. It blocks every kind equally, so it is a
 * blanket disable at the call site rather than a property of each kind — and
 * folding it in would make this function answer two questions.
 *
 * `destroy` is not a kind at all: it carries its own grant
 * (`run:apply-destroy`) and keeps its own inline two-step confirmation.
 */

/** Ordered by how much of the pipeline the kind runs. */
export type RunKindId = 'full' | 'planApply' | 'configure' | 'plan'

export interface RunKindContext {
  /** The workspace declares a plan/apply operation (#1966). */
  planApplyEnabled: boolean
  /** The workspace declares an Ansible configure operation. */
  configureEnabled: boolean
  /** `can-queue-run` — enough for a speculative plan. */
  canQueueRun: boolean
  /** `can-queue-apply` — needed by an apply AND by a configure, which is
   *  apply-equivalent. */
  canQueueApply: boolean
  /** A non-default ref is selected, which the server forces plan-only. */
  vcsRefSelected: boolean
}

export interface AvailableRunKind {
  id: RunKindId
  /** Present but not runnable now. `undefined` means it can run. */
  blocked?: 'vcsRef'
}

/**
 * The kinds to offer, MOST COMPLETE FIRST.
 *
 * Completeness is how many phases the kind runs: `full` (plan, apply and
 * configure) beats `planApply`, which beats the single phases. `configure`
 * precedes `plan` among those because it is the consequential one — it changes
 * real hosts where a plan changes nothing — so when it is the only thing a
 * workspace offers it is the primary, which is what "the most complete option
 * the workspace declares" means on a configure-only workspace.
 */
export function availableRunKinds(ctx: RunKindContext): AvailableRunKind[] {
  const out: AvailableRunKind[] = []
  const blockedByRef = ctx.vcsRefSelected ? ('vcsRef' as const) : undefined

  if (ctx.planApplyEnabled && ctx.configureEnabled && ctx.canQueueApply) {
    out.push({ id: 'full', blocked: blockedByRef })
  }
  if (ctx.planApplyEnabled && ctx.canQueueApply) {
    out.push({ id: 'planApply', blocked: blockedByRef })
  }
  if (ctx.configureEnabled && ctx.canQueueApply) {
    // NOT blocked by a ref: a configure runs the playbook from the workspace's
    // config directory against its inventory, and the ref forcing applies to
    // the plan/apply half. A standalone configure has no plan to be forced.
    out.push({ id: 'configure' })
  }
  if (ctx.planApplyEnabled && ctx.canQueueRun) {
    out.push({ id: 'plan' })
  }
  return out
}
