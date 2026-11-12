/**
 * Which view a run page opens on when the URL names none (#2509).
 *
 * Opening a run always landed on Overview. For a CONFIGURE-ONLY run the
 * configure log is almost always what the reader came for, so it cost an extra
 * click every time -- including straight after queuing the configure from the
 * workspace page, which is the most common way of getting here at all.
 *
 * ## Why this is a function in `lib/` and not three ternaries in the page
 *
 * The same reason `run-kinds.ts` gives: every file in `web/tests/` imports from
 * `src/lib/*.ts` and none imports a `.tsx`, because the runner is bare
 * `node --test` with no transpiler. A rule with four branches and a
 * decide-once constraint is worth being able to drive directly.
 *
 * ## It reads `run-kind`, and that is the point
 *
 * The server now says what a run IS in one word (#2507), so this asks the run
 * rather than deriving "is this configure-only" from `is-configure`,
 * `configure-after-apply` and the workspace's `plan-apply-enabled`. Three
 * surfaces each writing that predicate is how the run list came to badge a
 * configure-only run "plan + apply" AND "configure" at once, and no gate can
 * see three copies of a predicate disagreeing.
 *
 * A build talking to a server too old to send `run-kind` gets `undefined`,
 * which falls through to Overview -- exactly today's behaviour, which is the
 * right failure for a convenience.
 *
 * ## What it deliberately does NOT do
 *
 * **It does not send a live plan or apply to its log.** The issue's own table
 * offers that as an alternative for a composite ("the phase it is in ... or
 * Overview (unchanged)") and mandates Overview for everything else, and
 * Overview is where the Confirm & Apply button, the policy verdict and the
 * security gate are. Since the choice is made ONCE, moving a live run to its
 * plan log would leave the reader sitting there when it reaches `planned` and
 * the decision appears somewhere they are not looking.
 *
 * **It does not open an errored run on the phase that errored.** The run list
 * already offers that, deliberately and separately: its status badge links to
 * the explaining phase while the ROW links to the overview (#2413 F16), and a
 * default that overrode the row would retire that distinction rather than add
 * to it.
 */

// With the `.ts` extension, deliberately: `web/tests` runs under bare
// `node --test` with no bundler, so an extensionless specifier is not
// resolvable there and the test that drives this would die on an import error
// rather than on an assertion. `allowImportingTsExtensions` is on in
// `tsconfig.json` and Next resolves it the same way.
import { explainingPhase } from './run-phases.ts'

/** The run attributes this rule reads. A structural type, so the page's own
 *  `Run` interface satisfies it without either importing the other. */
export interface InitialViewInput {
  /** `plan_apply` | `configure` | `full`, or absent on an older server. */
  runKind?: string
  status?: string
  /** `status-timestamps`, for the phase a finished composite got to. */
  statusTimestamps?: Record<string, unknown> | null
}

/** Terminal in the sense this rule needs: the run is not going to move again,
 *  so the phase it reached is the phase it has. `canceling` is excluded on
 *  purpose -- the Job is still winding down, so the configure may still be
 *  producing output and `status === 'configuring'` has already matched. */
const FINISHED = new Set(['applied', 'errored', 'canceled', 'discarded'])

/**
 * The view to open, or `null` to keep the page's existing default.
 *
 * `null` rather than `'overview'` so the caller's fallback stays in one place:
 * this answers "is there a better view than the default", and inventing the
 * default here would be a second copy of it.
 */
export function initialRunView(run: InitialViewInput): 'configure' | null {
  if (run.runKind === 'configure') {
    // A configure and nothing else. There is no plan and no apply, so the
    // configure log is the only log the run has -- whatever its status.
    return 'configure'
  }
  if (run.runKind === 'full') {
    // A composite, which has a plan, an apply AND a configure. Only the
    // configure half earns the jump: while it is running, and once the run has
    // finished having reached it.
    if (run.status === 'configuring') return 'configure'
    if (
      run.status !== undefined &&
      FINISHED.has(run.status) &&
      explainingPhase(run.statusTimestamps) === 'configure'
    ) {
      return 'configure'
    }
  }
  return null
}
