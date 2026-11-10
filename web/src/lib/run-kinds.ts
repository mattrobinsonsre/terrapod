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
  /**
   * The workspace's `default-run-kind`, in the API's spelling (#2479).
   *
   * `''` — the default — keeps the ordering below exactly as it was. Anything
   * else PROMOTES that kind to primary, and only if the ordering already offers
   * it unblocked for this caller.
   */
  defaultRunKind?: string
}

/**
 * The API's spelling of a run kind to this module's.
 *
 * The API stores `plan_apply`, matching `auto_apply_mode`'s `create_update`;
 * this module has always used `planApply`. One mapping, here, rather than the
 * API bending to a frontend convention or the frontend to the wire's — and a
 * value this does not recognise maps to `undefined`, which the preference logic
 * treats as "no preference" rather than as an error. A newer server naming a
 * kind this build has never heard of must not break the button; it falls back
 * to the ordering, which is exactly what `''` does.
 */
export function runKindFromWire(wire: string | undefined): RunKindId | undefined {
  switch (wire) {
    case 'full':
      return 'full'
    case 'plan_apply':
      return 'planApply'
    case 'configure':
      return 'configure'
    case 'plan':
      return 'plan'
    default:
      return undefined
  }
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
  return promote(out, runKindFromWire(ctx.defaultRunKind))
}

/**
 * Move `preferred` to the front, if it is there to move and can run now.
 *
 * An OVERRIDE of the order and nothing else: it cannot add a kind or remove
 * one, so a preference can never surface a button the API would refuse nor hide
 * one it would accept. Three cases fall back to the ordering, deliberately:
 *
 *  - the workspace does not declare that operation — refused at the write, so
 *    only reachable if the setting was made and the operation later turned off;
 *  - this CALLER lacks the capability, which is per-caller and so cannot be
 *    settled at the write at all;
 *  - the kind is BLOCKED right now (a non-default `vcs-ref` forces plan-only).
 *    Promoting a blocked kind would make the primary button the one that cannot
 *    run, which is worse than the ordering it replaced.
 */
function promote(kinds: AvailableRunKind[], preferred: RunKindId | undefined): AvailableRunKind[] {
  if (!preferred) return kinds
  const i = kinds.findIndex((k) => k.id === preferred && !k.blocked)
  if (i <= 0) return kinds
  return [kinds[i], ...kinds.slice(0, i), ...kinds.slice(i + 1)]
}
