import assert from 'node:assert/strict'
import { test } from 'node:test'

import { parseLabelRule, formatLabelRule } from '../src/lib/label-rules.ts'

// #1457. Until 2.0 the roles form collapsed `env=prod, env=stg` to whichever
// value came last, so the role quietly did something other than what was typed.
// 1.6 refused the input outright rather than let that pass unseen; now that the
// SDK and provider carry the shape, it accumulates.
//
// Tested here rather than through the page because a rule that silently drops a
// clause is a permissions bug, and the shape that used to break things is
// exactly the one nothing exercised.

test('a repeated key accumulates instead of overwriting', () => {
  assert.deepEqual(parseLabelRule('env=prod, env=stg'), { env: ['prod', 'stg'] })
})

test('a single value still parses to a one-element list', () => {
  assert.deepEqual(parseLabelRule('team=sre'), { team: ['sre'] })
})

test('several keys each keep their own values', () => {
  assert.deepEqual(parseLabelRule('env=prod, env=stg, team=sre'), {
    env: ['prod', 'stg'],
    team: ['sre'],
  })
})

test('a key repeated with the SAME value stays one value', () => {
  // A typo, not a request for a duplicate clause — and a duplicate would show
  // up in the reach preview as a doubled reason.
  assert.deepEqual(parseLabelRule('env=prod, env=prod'), { env: ['prod'] })
})

test('order within a key is preserved', () => {
  // It is rendered back into the box and shown in the reach preview, so
  // reordering would read as an edit the operator did not make.
  assert.deepEqual(parseLabelRule('env=c, env=a, env=b'), { env: ['c', 'a', 'b'] })
})

test('a bare key matches the empty value', () => {
  assert.deepEqual(parseLabelRule('tier'), { tier: [''] })
})

test('empty and whitespace input yield no rule', () => {
  assert.deepEqual(parseLabelRule(''), {})
  assert.deepEqual(parseLabelRule('   '), {})
  assert.deepEqual(parseLabelRule(' , , '), {})
})

test('surrounding whitespace is trimmed', () => {
  assert.deepEqual(parseLabelRule('  env = prod ,  env = stg  '), {
    env: ['prod', 'stg'],
  })
})

test('formatting is the inverse of parsing', () => {
  const text = 'env=prod, env=stg, team=sre'
  assert.equal(formatLabelRule(parseLabelRule(text)), text)
})

test('formatting tolerates a scalar, which the server may still send', () => {
  assert.equal(formatLabelRule({ team: 'sre', env: ['prod', 'stg'] }), 'team=sre, env=prod, env=stg')
})

test('formatting renders a bare key without an equals sign', () => {
  assert.equal(formatLabelRule({ tier: [''] }), 'tier')
})
