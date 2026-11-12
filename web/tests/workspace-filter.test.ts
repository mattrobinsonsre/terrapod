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

  it('knows the six conditions the server emits, in one list', () => {
    assert.deepEqual([...FLAG_CODES].sort(), [
      'drift_errored',
      'drifted',
      'no_agent_pool',
      'no_live_agent_pool',
      'state_diverged',
      'vcs_error',
    ])
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
