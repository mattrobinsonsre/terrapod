// A run held after its plan, in either of the API's vocabularies (#1704, #1725).
//
// The page must treat `policy_override` exactly as it treats `planning` with a
// `blocked-by`, or turning on the Terraform Enterprise vocabulary would change
// what the UI does (a held run would stop polling its panels, and render its
// raw status string).
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { gateOf, holdActivityKey, phaseOf } from '../src/lib/run-hold.ts'

test('a held run is in the planning phase whichever vocabulary reported it', () => {
  for (const status of ['post_plan_running', 'post_plan_awaiting_decision', 'policy_override']) {
    assert.equal(phaseOf(status), 'planning')
  }
  for (const status of ['planning', 'planned', 'applying', 'errored']) {
    assert.equal(phaseOf(status), status)
  }
})

test('the gate comes from blocked-by and nothing else', () => {
  assert.equal(gateOf({ 'blocked-by': 'policy' }), 'policy')
  assert.equal(gateOf({ 'blocked-by': 'security-scan' }), 'security-scan')
  assert.equal(gateOf({ 'blocked-by': 'run-task' }), 'run-task')
  assert.equal(gateOf({ 'blocked-by': 'ai-policy' }), 'ai-policy')
  assert.equal(gateOf({ 'blocked-by': null }), null)
  assert.equal(gateOf({}), null)
  // An API newer than this page may name a gate it does not know.
  assert.equal(gateOf({ 'blocked-by': 'something-new' }), null)
})

test('a run-task hold says whether tasks are running or one failed', () => {
  assert.equal(holdActivityKey('run-task', 'post_plan_awaiting_decision'), 'heldByRunTask')
  assert.equal(holdActivityKey('run-task', 'post_plan_running'), 'runTasksRunning')
  // 1.x errors a run whose mandatory task failed, so a 1.x hold is tasks running.
  assert.equal(holdActivityKey('run-task', 'planning'), 'runTasksRunning')
  assert.equal(holdActivityKey('policy', 'policy_override'), 'heldByPolicy')
  assert.equal(holdActivityKey('security-scan', 'planning'), 'heldBySecurityScan')
})

test('an AI policy hold distinguishes waiting for a verdict from a denial', () => {
  // The only gate whose evidence is produced AFTER the plan, in the API rather
  // than the runner. So a mandatory gate routinely holds a run before there is
  // anything to decide, and telling someone their decision is awaited then
  // sends them looking for a button that is not there yet.
  assert.equal(holdActivityKey('ai-policy', 'post_plan_running'), 'awaitingAiPolicy')
  assert.equal(holdActivityKey('ai-policy', 'policy_override'), 'heldByAiPolicy')
  // 1.x keeps the run in `planning` and names the gate in blocked-by; a
  // verdict has landed and denied by the time the gate holds it there.
  assert.equal(holdActivityKey('ai-policy', 'planning'), 'heldByAiPolicy')
})
