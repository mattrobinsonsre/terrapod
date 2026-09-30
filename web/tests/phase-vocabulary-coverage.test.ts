// COVERAGE, not mechanism (#1911).
//
// `phase-vocabulary.test.ts` proves the vocabulary is well-formed and that every
// name a component asks for exists. Neither of those notices the actual defect
// here, which is a call site that never asked: a page reading
// `t('badge.planOnly')` instead of the engine's word is perfectly valid,
// type-checks, resolves in all 32 catalogues, and renders "plan only" on a
// Pulumi run. That is precisely the bug — a mechanism wired into two surfaces
// out of a dozen — so it needs a test that fails on the omission rather than on
// the machinery.
//
// So: each surface that names a run phase declares the flat keys it has GIVEN UP
// and the vocabulary name each became. This asserts the file no longer CALLS the
// flat key, and that the replacement exists for both engines. Point a call site
// back at the old key and this goes red.
//
// Scoped per file on purpose. Several of these keys are still read, legitimately,
// by cross-workspace admin pages — the autodiscovery rule form offers
// `fields.driftIgnoreRules` for a rule that has no engine in view yet. A global
// ban would fail on those and be deleted within a week.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

/** file -> { flat message key it gave up: the vocabulary name it became }. */
const SURRENDERED: Record<string, Record<string, string>> = {
  'src/app/workspaces/[id]/runs/[runId]/page.tsx': {
    'activity.plannedConfirmable': 'activityPlannedConfirmable',
    'activity.plannedSpeculative': 'activityPlannedSpeculative',
    'activity.plannedComplete': 'activityPlannedComplete',
    'activity.confirmed': 'activityConfirmed',
    'activity.applied': 'activityApplied',
    'activity.discarded': 'activityDiscarded',
    'tabs.planLabel': 'tabPlanLabel',
    'tabs.applyLabel': 'tabApplyLabel',
    'tabs.planFull': 'tabPlanFull',
    'tabs.applyFull': 'tabApplyFull',
    'log.planEmpty': 'logPlanEmpty',
    'log.applyEmpty': 'logApplyEmpty',
    'log.applySkippedNoChanges': 'logApplySkipped',
    'log.index.summary': 'logIndexSummary',
    'log.index.title': 'logIndexTitle',
    'changes.planning': 'changesPlanning',
    'badge.planOnly': 'badgePlanOnly',
    'banner.planOnlyCli': 'bannerPlanOnlyCli',
    discardedHeading: 'discardedHeading',
    'details.planOnly': 'detailsPlanOnly',
    'actions.confirmShort': 'actionConfirmShort',
    'actions.confirmFull': 'actionConfirmFull',
    'confirm.apply': 'confirmApply',
    'confirm.discard': 'confirmDiscard',
    'timeline.planningStarted': 'timelinePlanningStarted',
    'timeline.planComplete': 'timelinePlanComplete',
    'timeline.applyingStarted': 'timelineApplyingStarted',
    'timeline.applied': 'timelineApplied',
    'drift.remediate': 'driftRemediate',
    'drift.remediateTitle': 'driftRemediateTitle',
    'drift.remediateConfirm': 'driftRemediateConfirm',
    'policyPanel.blockedMessage': 'policyBlockedMessage',
  },
  'src/app/workspaces/[id]/page.tsx': {
    // The runs tab.
    'runs.queuePlan': 'queuePlan',
    'runs.queueRun': 'queueRun',
    'runs.queuePlanConfirm': 'queuePlanConfirm',
    'runs.queueApplyConfirm': 'queueApplyConfirm',
    'runs.queueApplyTitle': 'queueApplyTitle',
    'runs.queueApplyRefBlocked': 'queueApplyRefBlocked',
    'runs.queueDestroyTitle': 'queueDestroyTitle',
    'runs.planOptions': 'planOptions',
    'runs.typePlanOnly': 'badgePlanOnly',
    'runs.typePlanApply': 'typePlanApply',
    'runs.nonDefaultRefNote': 'nonDefaultRefNote',
    // The cards above it, which were still speaking Terraform beside it.
    'lock.lockedDesc': 'lockLockedDesc',
    'drift.checkNowTitle': 'driftCheckNowTitle',
    'planExpiry.title': 'planExpiryTitle',
    'planExpiry.description': 'planExpiryDescription',
    'aiSummary.title': 'aiSummaryTitle',
    'aiSummary.description': 'aiSummaryDescription',
    'errors.queuePlan': 'errorsQueuePlan',
    'errors.queuePlanStatus': 'errorsQueuePlanStatus',
    'configurations.empty': 'configurationsEmpty',
    'fields.autoMerge': 'fieldsAutoMerge',
    'vcsWorkflowWarning.rbac': 'vcsWorkflowWarningRbac',
    'vcsWorkflowWarning.recommended': 'vcsWorkflowWarningRecommended',
    'runTriggers.description': 'runTriggersDescription',
  },
  // The create form, where the engine is chosen rather than already fixed. Only
  // the HINT moves: the four mode values beside it render the literal wire
  // strings (`never`, `always`, `create`, `create/update`) that
  // `auto-apply-mode` accepts, so they are identifiers and stay in `common`.
  'src/app/workspaces/page.tsx': {
    'form.autoApplyModeHint': 'formAutoApplyModeHint',
  },
  'src/components/plan-ai-summary.tsx': {
    'heading.planSummary': 'planSummaryHeading',
    'pending.summarisingPlan': 'planSummarySummarising',
    'regenerate.tooltip': 'planSummaryRegenerateTooltip',
  },
  'src/components/plan-summary-chat.tsx': { 'chat.placeholder': 'planSummaryChatPlaceholder' },
  'src/components/plan-summary-badges.tsx': {
    'badges.noChangesTitle': 'planSummaryNoChangesTitle',
  },
  'src/components/cost-panel.tsx': { 'cost.noState': 'costNoState' },
  'src/components/ai-policy-panel.tsx': {
    'aiPolicyPanel.overrideConfirm': 'aiPolicyOverrideConfirm',
  },
}

/**
 * Whether `src` still CALLS `key` through next-intl.
 *
 * Matches the call, not the bare quoted string: several vocabulary names are
 * spelled exactly like the flat key they replaced (`discardedHeading`), so a
 * bare substring search reports the fix as the bug.
 */
function stillCalls(src: string, key: string): boolean {
  const k = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(String.raw`\bt(?:Ws|Mode|Status)?(?:\.rich|\.has)?\(\s*['\`]${k}['\`]`).test(src)
}

for (const [file, moved] of Object.entries(SURRENDERED)) {
  test(`${file} reads its phase words from the engine, not the flat namespace`, () => {
    const src = readFileSync(file, 'utf8')
    const back = Object.keys(moved).filter((k) => stillCalls(src, k))
    assert.deepEqual(
      back,
      [],
      `these name a run phase and must come from phaseKey(engine, 'words', …): ${back.join(', ')}`,
    )
  })
}

test('every surrendered key has a replacement carried by both engines', () => {
  // The other half of the contract. Without it the guard above is satisfied by
  // deleting the string rather than moving it, which is not the same thing.
  const en = JSON.parse(readFileSync('messages/en.json', 'utf8')) as {
    phases: Record<string, Record<string, Record<string, string>>>
  }
  const missing: string[] = []
  for (const moved of Object.values(SURRENDERED)) {
    for (const [key, name] of Object.entries(moved)) {
      if (!(name in en.phases.terraform.words) || !(name in en.phases.pulumi.words)) {
        missing.push(`${key} -> ${name}`)
      }
    }
  }
  assert.deepEqual(missing, [], 'surrendered keys whose vocabulary replacement does not exist')
})

test('the guard can actually see a reverted call site', () => {
  // A self-test, because every assertion above is a NEGATIVE one: if the
  // matcher silently stopped matching, all eight would still pass and the file
  // would be worthless. This pins that it fires on the shape it is looking for.
  assert.equal(stillCalls("foo(t('badge.planOnly'))", 'badge.planOnly'), true)
  assert.equal(stillCalls('foo(t.rich(`banner.planOnlyCli`, {}))', 'banner.planOnlyCli'), true)
  assert.equal(stillCalls("foo(t.has('confirm.apply'))", 'confirm.apply'), true)
  // …and NOT on the vocabulary call that replaced it, even where the name is
  // spelled identically to the key.
  assert.equal(stillCalls("foo(word('discardedHeading'))", 'discardedHeading'), false)
  assert.equal(stillCalls("foo(phaseWord('queuePlan'))", 'runs.queuePlan'), false)
})

test('the mode VALUES are deliberately not vocabulary, and stay where they are', () => {
  // The counterpart to the guard above, so the line between the two is written
  // down rather than remembered. `never` / `always` / `create` / `create-update`
  // render the literal values `auto-apply-mode` accepts; they are identifiers,
  // and swapping them per engine would make the label disagree with the
  // attribute, the column, the provider field and the MCP field — a wider
  // inconsistency than the one the vocabulary exists to remove.
  const en = JSON.parse(readFileSync('messages/en.json', 'utf8')) as {
    common: { autoApplyMode: Record<string, string> }
    phases: Record<string, Record<string, Record<string, string>>>
  }
  assert.deepEqual(en.common.autoApplyMode, {
    never: 'never',
    always: 'always',
    create: 'create',
    createUpdate: 'create/update',
  })
  for (const engine of ['terraform', 'pulumi']) {
    for (const name of Object.keys(en.phases[engine].words)) {
      assert.ok(
        !/^autoApplyMode/.test(name),
        `${name} moves an auto-apply MODE value into the vocabulary; those are wire values`,
      )
    }
  }
})
