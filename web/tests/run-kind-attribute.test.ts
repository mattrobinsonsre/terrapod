import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it } from 'node:test'

import { primaryRunBadge, showsConfigureBadge } from '../src/lib/run-badges.ts'
import { initialRunView } from '../src/lib/run-initial-view.ts'

/**
 * The run document says what KIND of run it is, and the UI reads it (#2507, #2509).
 *
 * A configure-only run was badged **plan + apply** *and* **configure** at once,
 * and the run page opened on Overview when the configure log was the only log
 * the run had. One cause: three surfaces each DERIVED "is this configure-only"
 * from `is-configure`, `configure-after-apply` and the workspace's
 * `plan-apply-enabled`, and one of the three derivations was wrong.
 *
 * So the fix is a derived server attribute, `run-kind`, and the gate has two
 * halves — neither of which the other can see:
 *
 *  1. the two rules BEHAVE correctly, driven directly (the `describe` blocks);
 *  2. **nothing re-derives the kind.** A behavioural test cannot see a second
 *     copy of the predicate appearing beside the helper that replaced it, and a
 *     second copy is exactly how the three got out of step. That is a source
 *     check, and it is the half that keeps the fix fixed.
 */

const SRC = join(import.meta.dirname, '..', 'src')

/** Reading `configure-after-apply` off a RUN, however the variable is named. */
const RUN_FLAG_READ = /\b(run|item|r)\.attributes\[['"]configure-after-apply['"]\]/

/** A comparison against a run-kind literal, either way round. */
const KIND_LITERAL =
  /['"](?:plan_apply|configure|full)['"]\s*===|===\s*['"](?:plan_apply|configure|full)['"]/

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry)
    // `readdirSync`, not `grep`: the `Node checks` CI job runs on Alpine, where
    // BusyBox grep has neither `--include` nor `--exclude-dir` and exits 2 — so
    // a shelled-out matcher dies rather than reporting, which reads as a broken
    // suite rather than a missing flag.
    if (statSync(p).isDirectory()) walk(p, out)
    else if (p.endsWith('.ts') || p.endsWith('.tsx')) out.push(p)
  }
  return out
}

describe('primaryRunBadge picks which plan/apply badge a run gets', () => {
  it('gives a configure-only run NO plan/apply badge', () => {
    // The defect, pinned: this returned `planApply` because a configure-only
    // run is neither a destroy nor plan-only and fell through to the default.
    assert.equal(primaryRunBadge({ runKind: 'configure' }), null)
    // And it stays null even when the run looks like something else, because
    // the kind is the answer and the modifiers are not.
    assert.equal(primaryRunBadge({ runKind: 'configure', planOnly: true }), null)
    assert.equal(primaryRunBadge({ runKind: 'configure', isDestroy: true }), null)
  })

  it('keeps destroy and plan-only as modifiers of the plan/apply half', () => {
    assert.equal(primaryRunBadge({ runKind: 'plan_apply', isDestroy: true }), 'destroy')
    assert.equal(primaryRunBadge({ runKind: 'plan_apply', planOnly: true }), 'planOnly')
    assert.equal(primaryRunBadge({ runKind: 'plan_apply' }), 'planApply')
    // A composite HAS a plan/apply half, so it keeps its badge — one replacing
    // the other would hide half of what the run does.
    assert.equal(primaryRunBadge({ runKind: 'full' }), 'planApply')
    assert.equal(primaryRunBadge({ runKind: 'full', isDestroy: true }), 'destroy')
  })

  it('falls back to the old behaviour when the server sends no kind', () => {
    // A build can outlive the server it talks to. Without `run-kind` this is
    // exactly the pre-#2507 rule, which is the right failure for a presentation
    // fix: the badge is slightly wrong rather than absent.
    assert.equal(primaryRunBadge({}), 'planApply')
    assert.equal(primaryRunBadge({ planOnly: true }), 'planOnly')
  })
})

describe('showsConfigureBadge says whether the configure badge appears', () => {
  it('shows it for a configure and for a composite, and not for a plan/apply', () => {
    assert.equal(showsConfigureBadge({ runKind: 'configure' }), true)
    assert.equal(showsConfigureBadge({ runKind: 'full' }), true)
    assert.equal(showsConfigureBadge({ runKind: 'plan_apply' }), false)
  })

  it('prefers the kind over the legacy flag, and uses the flag without one', () => {
    // The kind WINS where it is present: a server that says `plan_apply` is
    // authoritative over a stale flag.
    assert.equal(showsConfigureBadge({ runKind: 'plan_apply', isConfigure: true }), false)
    // And the flag carries it where the kind is absent, so an older server does
    // not silently lose the one badge that says a playbook ran on real hosts.
    assert.equal(showsConfigureBadge({ isConfigure: true }), true)
    assert.equal(showsConfigureBadge({}), false)
  })

  it('treats a kind this build has never heard of as not-a-configure', () => {
    // A newer server naming a fourth kind must not make the badge appear on a
    // run this build cannot describe. `undefined` is the fallback path; an
    // unknown STRING is a decision the server made and this build cannot read.
    assert.equal(showsConfigureBadge({ runKind: 'something_new' }), false)
    assert.equal(primaryRunBadge({ runKind: 'something_new' }), 'planApply')
  })
})

describe('initialRunView picks the view a run page opens on', () => {
  it('opens a configure-only run on its configure log, whatever its status', () => {
    for (const status of ['queued', 'configuring', 'applied', 'errored']) {
      assert.equal(initialRunView({ runKind: 'configure', status }), 'configure')
    }
  })

  it('opens a composite on its configure only while or after it configures', () => {
    assert.equal(initialRunView({ runKind: 'full', status: 'configuring' }), 'configure')
    // Finished HAVING reached the configure: `configuring-at` is present, so
    // `explainingPhase` answers `configure`.
    assert.equal(
      initialRunView({
        runKind: 'full',
        status: 'applied',
        statusTimestamps: { 'planning-at': 'x', 'applying-at': 'y', 'configuring-at': 'z' },
      }),
      'configure',
    )
    // Errored in its APPLY: no `configuring-at`, so the configure never ran and
    // its log would be empty. Overview, which is where the error is explained.
    assert.equal(
      initialRunView({
        runKind: 'full',
        status: 'errored',
        statusTimestamps: { 'planning-at': 'x', 'applying-at': 'y' },
      }),
      null,
    )
    // Still planning: Overview, because that is where Confirm & Apply, the
    // policy verdict and the security gate are — and the choice is made ONCE,
    // so sending it to a log would leave the reader there when the decision
    // appears somewhere they are not looking.
    assert.equal(initialRunView({ runKind: 'full', status: 'planning' }), null)
  })

  it('leaves a plain plan/apply run on the page default', () => {
    for (const status of ['queued', 'planning', 'planned', 'applying', 'applied', 'errored']) {
      assert.equal(initialRunView({ runKind: 'plan_apply', status }), null)
    }
  })

  it('leaves the default alone when the server sends no kind', () => {
    assert.equal(initialRunView({ status: 'configuring' }), null)
    assert.equal(initialRunView({}), null)
  })
})

describe('nothing re-derives the run kind', () => {
  /**
   * The source half, and the reason it exists: the three derivations that
   * disagreed each read `configure-after-apply` off a RUN, and no behavioural
   * test can see a fourth appearing beside the helper that replaced them.
   *
   * **Scoped to the RUN's attribute, deliberately.** `configure-after-apply`
   * is ALSO a workspace setting -- the standing "a configure follows an apply
   * here" declaration, with its own control on the Configuration tab -- so a
   * gate banning the string anywhere flagged the workspace page for OFFERING
   * that setting, which is a finding about the matcher rather than about the
   * code. The first version of this did exactly that.
   *
   * So the subject is the receiver: a run, however the variable is named. The
   * run pages name theirs `run`, the sort key's `item`, and the loaders `r`.
   * There is no exemption list, because nothing needs one -- the question
   * those reads answered is `isCompositeRun`'s now.
   */
  it('no component reads configure-after-apply off a run', () => {
    const offenders: string[] = []
    for (const file of walk(SRC)) {
      const text = readFileSync(file, 'utf8')
      for (const line of text.split('\n')) {
        if (RUN_FLAG_READ.test(line)) {
          offenders.push(`${file.slice(SRC.length + 1)}: ${line.trim()}`)
        }
      }
    }
    assert.deepEqual(
      offenders,
      [],
      'these lines read `configure-after-apply` off a run, which is the derivation ' +
        '`run-kind` replaced -- ask `lib/run-badges.ts` instead (`isCompositeRun`, ' +
        `\`primaryRunBadge\`, \`showsConfigureBadge\`): ${offenders.join(' | ')}`,
    )
  })

  it('that matcher sees the read it bans, and leaves the workspace setting alone', () => {
    // The matcher's own fixtures, because a coverage count over the tree cannot
    // tell a page dropping its last read from the matcher losing its pattern --
    // and it is the second that matters. Both directions are pinned, including
    // the workspace setting the first version of this gate wrongly flagged.
    assert.ok(RUN_FLAG_READ.test("title={run.attributes['configure-after-apply'] ? a : b}"))
    assert.ok(RUN_FLAG_READ.test('const x = item.attributes["configure-after-apply"]'))
    assert.ok(RUN_FLAG_READ.test("if (r.attributes['configure-after-apply']) {"))
    // The workspace's own setting, in the three shapes this tree writes it in.
    // None of them is a run.
    assert.ok(
      !RUN_FLAG_READ.test(
        "setEditConfigureAfterApply(workspace.attributes['configure-after-apply'])",
      ),
    )
    assert.ok(!RUN_FLAG_READ.test("'configure-after-apply': editConfigureAfterApply,"))
    assert.ok(!RUN_FLAG_READ.test('attr="configure-after-apply"'))
  })

  it('the helpers are the only place the kind literals appear outside a type', () => {
    // A floor AND a scope check. Without it a page could switch on
    // `=== 'configure'` itself and this file would never notice; with it, the
    // vocabulary lives in two modules and the pages ask them.
    const offenders: string[] = []
    for (const file of walk(SRC)) {
      const rel = file.slice(SRC.length + 1)
      if (rel === join('lib', 'run-badges.ts') || rel === join('lib', 'run-initial-view.ts')) continue
      // `run-kinds.ts` maps the API's spelling to the workspace BUTTON ids
      // (#2479) -- a different question about a different object, and its own
      // `runKindFromWire` is the one producer for that mapping.
      if (rel === join('lib', 'run-kinds.ts')) continue
      const src = readFileSync(file, 'utf8')
      for (const line of src.split('\n')) {
        if (!line.includes('run-kind') && !line.includes('runKind')) continue
        if (KIND_LITERAL.test(line)) offenders.push(`${rel}: ${line.trim()}`)
      }
    }
    assert.deepEqual(
      offenders,
      [],
      `these lines compare a run kind outside the two helpers: ${offenders.join(' | ')}`,
    )
  })

  it('that matcher sees a comparison either way round', () => {
    // Pinned by literal, because the FIRST version omitted `configure` from
    // the right-hand side and a page writing
    // `run.attributes['run-kind'] === 'configure'` passed -- the most likely
    // shape of a fourth derivation, through the gate written to stop it.
    assert.ok(KIND_LITERAL.test("const isCfg = run.attributes['run-kind'] === 'configure'"))
    assert.ok(KIND_LITERAL.test("if (runKind === 'full') {"))
    assert.ok(KIND_LITERAL.test("if ('plan_apply' === runKind) {"))
    // And it is the LINE FILTER that keeps a view comparison out, not the
    // pattern: `view === 'configure'` matches this expression and never
    // reaches it, because that line mentions no run kind.
    assert.ok(KIND_LITERAL.test("if (view === 'configure') {"))
    assert.ok(!"if (view === 'configure') {".includes('runKind'))
    assert.ok(!"if (view === 'configure') {".includes('run-kind'))
  })
})
