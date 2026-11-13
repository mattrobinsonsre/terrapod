/**
 * How the Workspaces filter combines terms.
 *
 * The two multi-select dropdowns (Status, Flags) union their own selections;
 * everything typed intersects. `status:` carries only the run's status, so two
 * of them can only mean "either"; `flag:` is one health condition each, and a
 * workspace can carry several. `locked:` is a separate key so that
 * `locked:true flag:drifted` still means "locked AND drifted".
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  FLAG_CODES,
  flagValueForCode,
  matchWorkspace,
  parseFilterQuery,
  serializeFilter,
  toggleFlagTerm,
  toggleLockedTerm,
  toggleStatusTerm,
} from '../src/lib/workspace-filter.ts'

function ws(
  name: string,
  opts: { labels?: Record<string, string>; conditions?: string[]; locked?: boolean } = {},
) {
  return {
    attributes: {
      name,
      labels: opts.labels ?? {},
      'health-conditions': (opts.conditions ?? []).map((code) => ({ code })),
      locked: opts.locked ?? false,
    },
  }
}

function match(query: string, w: ReturnType<typeof ws>, status?: string) {
  return matchWorkspace(w, parseFilterQuery(query), status)
}

describe('status terms union', () => {
  it('status:planned status:applied is planned OR applied', () => {
    assert.equal(match('status:planned status:applied', ws('a'), 'planned'), true)
    assert.equal(match('status:planned status:applied', ws('a'), 'applied'), true)
    assert.equal(match('status:planned status:applied', ws('a'), 'errored'), false)
  })

  it('a single status behaves as before', () => {
    assert.equal(match('status:errored', ws('a'), 'errored'), true)
    assert.equal(match('status:errored', ws('a'), 'applied'), false)
  })

  it('status is only the run status: locked and unhealthy are no longer values of it', () => {
    // Neither spelling is special any more, so each is just an unknown status.
    assert.equal(match('status:locked', ws('a', { locked: true }), 'applied'), false)
    assert.equal(match('status:unhealthy', ws('a', { conditions: ['drifted'] }), 'applied'), false)
  })
})

describe('flag terms union', () => {
  it('flag:drifted flag:no-agent-pool is drifted OR no agent pool', () => {
    assert.equal(match('flag:drifted flag:no-agent-pool', ws('a', { conditions: ['drifted'] })), true)
    assert.equal(match('flag:drifted flag:no-agent-pool', ws('a', { conditions: ['no_agent_pool'] })), true)
    assert.equal(match('flag:drifted flag:no-agent-pool', ws('a', { conditions: ['vcs_error'] })), false)
    assert.equal(match('flag:drifted flag:no-agent-pool', ws('a')), false)
  })

  it('a workspace with several conditions matches any one of them', () => {
    const many = ws('a', { conditions: ['drifted', 'vcs_error'] })
    assert.equal(match('flag:vcs-error', many), true)
    assert.equal(match('flag:drifted', many), true)
  })

  it('derives the value from the condition code, kebab-cased', () => {
    assert.equal(flagValueForCode('state_diverged'), 'state-diverged')
    assert.equal(flagValueForCode('no_live_agent_pool'), 'no-live-agent-pool')
    assert.equal(flagValueForCode('drift_errored'), 'drift-errored')
  })

  // DERIVED FROM THE SERVER, not written down twice. `FLAG_CODES` describes
  // itself as "the health-condition codes the server emits in
  // `_compute_health_conditions`", and until this read that function the claim
  // was a ledger with nothing keeping it true: a condition added server-side
  // was simply absent from the `flag:` values, the Flags dropdown and the
  // Health Issues expansion, all three of which derive from this one list.
  //
  // That is not hypothetical -- `run_errored` was added to the server and the
  // only thing that noticed was the hand-written list below, which had to be
  // edited by hand to agree again. Deriving it means the next one cannot be
  // forgotten, and the failure names the missing code rather than a count.
  const serverConditionCodes = (): string[] => {
    const here = path.dirname(fileURLToPath(import.meta.url))
    const src = readFileSync(
      path.join(here, '..', '..', 'services', 'terrapod', 'api', 'routers', 'tfe_v2.py'),
      'utf8',
    )
    // Python bodies are indentation-delimited, so the function runs to the next
    // line that starts a top-level `def`. Slicing rather than scanning the whole
    // file matters: `tfe_v2.py` carries `"code":` keys for other things too.
    const start = src.indexOf('def _compute_health_conditions(')
    assert.ok(start > 0, '_compute_health_conditions is gone from tfe_v2.py — has it moved?')
    const after = src.slice(start + 1)
    const end = after.search(/\ndef /)
    const body = end < 0 ? after : after.slice(0, end)
    const codes = [...body.matchAll(/"code":\s*"([a-z_]+)"/g)].map(m => m[1])
    return [...new Set(codes)].sort()
  }

  it('reads a real function, so it cannot pass on an empty subject', () => {
    // The floor the slice needs: a matcher that stops matching would otherwise
    // make both sides empty and agree perfectly.
    assert.ok(
      serverConditionCodes().length >= 6,
      `only found ${serverConditionCodes().length} codes in _compute_health_conditions`,
    )
  })

  it('carries exactly the conditions the server emits', () => {
    assert.deepEqual([...FLAG_CODES].sort(), serverConditionCodes())
  })

  it('every condition code is reachable by its derived value', () => {
    for (const code of FLAG_CODES) {
      assert.equal(match(`flag:${flagValueForCode(code)}`, ws('a', { conditions: [code] })), true, code)
    }
  })
})

describe('locked is its own key', () => {
  it('locked:true and locked:false', () => {
    assert.equal(match('locked:true', ws('a', { locked: true })), true)
    assert.equal(match('locked:true', ws('a', { locked: false })), false)
    assert.equal(match('locked:false', ws('a', { locked: false })), true)
    assert.equal(match('locked:false', ws('a', { locked: true })), false)
  })

  it('ANDs with flag and status: locked and drifted, locked and errored', () => {
    const lockedDrifted = ws('a', { locked: true, conditions: ['drifted'] })
    assert.equal(match('locked:true flag:drifted', lockedDrifted), true)
    assert.equal(match('locked:true flag:drifted', ws('a', { conditions: ['drifted'] })), false)
    assert.equal(match('locked:true flag:drifted', ws('a', { locked: true })), false)
    assert.equal(match('locked:true status:errored', ws('a', { locked: true }), 'errored'), true)
    assert.equal(match('locked:true status:errored', ws('a', { locked: false }), 'errored'), false)
  })

  it('treats an unrecognised value as matching nothing, not everything', () => {
    assert.equal(match('locked:maybe', ws('a', { locked: true })), false)
  })
})

describe('typed terms still intersect', () => {
  it('status AND a label: status:errored env:prod', () => {
    assert.equal(match('status:errored env:prod', ws('a', { labels: { env: 'prod' } }), 'errored'), true)
    assert.equal(match('status:errored env:prod', ws('a', { labels: { env: 'dev' } }), 'errored'), false)
    assert.equal(match('status:errored env:prod', ws('a', { labels: { env: 'prod' } }), 'applied'), false)
  })

  it('label terms still AND: env:prod env:staging matches nothing, unchanged', () => {
    assert.equal(match('env:prod env:staging', ws('a', { labels: { env: 'prod' } })), false)
    assert.equal(match('env:prod team:core', ws('a', { labels: { env: 'prod', team: 'core' } })), true)
  })

  it('name terms still AND: eu1 data', () => {
    assert.equal(match('eu1 data', ws('eu1-data-pipeline')), true)
    assert.equal(match('eu1 data', ws('eu1-network')), false)
  })

  it('an OR-ed status group ANDs with a name, a label and a flag', () => {
    const w = ws('eu1-net', { labels: { env: 'prod' }, conditions: ['drifted'] })
    assert.equal(match('eu1 env:prod flag:drifted status:planned status:applied', w, 'applied'), true)
    assert.equal(match('eu1 env:prod flag:drifted status:planned status:applied', w, 'errored'), false)
    assert.equal(match('us1 env:prod flag:drifted status:planned status:applied', w, 'applied'), false)
  })

  it('matches everything for an empty filter', () => {
    assert.equal(match('', ws('a')), true)
  })
})

describe('parsing and serialising the new keys', () => {
  it('round-trips flag and locked terms', () => {
    const q = 'flag:drifted flag:vcs-error locked:true status:errored'
    assert.equal(serializeFilter(parseFilterQuery(q)), q)
  })

  it('drops a flag: or locked: with no value, like status:', () => {
    assert.equal(serializeFilter(parseFilterQuery('flag: locked: status:')), '')
  })
})

describe('toggle helpers', () => {
  it('toggleFlagTerm adds and removes a flag', () => {
    const on = toggleFlagTerm(parseFilterQuery(''), 'drifted')
    assert.equal(serializeFilter(on), 'flag:drifted')
    assert.equal(serializeFilter(toggleFlagTerm(on, 'drifted')), '')
  })

  it('toggleLockedTerm sets locked:true and a second toggle clears it', () => {
    const on = toggleLockedTerm(parseFilterQuery('status:errored'))
    assert.equal(serializeFilter(on), 'status:errored locked:true')
    assert.equal(serializeFilter(toggleLockedTerm(on)), 'status:errored')
  })

  it('toggleStatusTerm still works for a run status', () => {
    const on = toggleStatusTerm(parseFilterQuery(''), 'planned')
    assert.equal(serializeFilter(on), 'status:planned')
  })
})
