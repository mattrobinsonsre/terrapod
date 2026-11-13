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

import { RUN_STATUSES } from '../src/lib/workspace-status.ts'
import {
  FLAG_CODES,
  FLAG_VALUES,
  countOfferedTerms,
  flagValueForCode,
  matchWorkspace,
  migrateLegacyFilter,
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

describe('an unrecognised status value matches nothing', () => {
  it('a typo next to a valid status does not widen to that status', () => {
    assert.equal(match('status:erored status:applied', ws('a'), 'applied'), false)
    assert.equal(match('status:applied status:erored', ws('a'), 'applied'), false)
    assert.equal(match('status:erored', ws('a'), 'errored'), false)
  })

  it('the removed spellings are unknown values when typed by hand', () => {
    assert.equal(match('status:locked status:applied', ws('a', { locked: true }), 'applied'), false)
    assert.equal(match('status:unhealthy status:applied', ws('a', { conditions: ['drifted'] }), 'applied'), false)
    assert.equal(match('status:drifted status:applied', ws('a', { conditions: ['drifted'] }), 'applied'), false)
  })

  it('every value the dropdown offers still matches', () => {
    for (const s of RUN_STATUSES) assert.equal(match(`status:${s.filter}`, ws('a'), s.filter), true, s.filter)
  })
})

describe('an unrecognised flag value matches nothing', () => {
  it('a typo alone matches nothing, even on a workspace with a condition', () => {
    assert.equal(match('flag:nope', ws('a', { conditions: ['drifted'] })), false)
  })

  it('a typo next to a valid flag does not drop out of the either-set', () => {
    const drifted = ws('a', { conditions: ['drifted'] })
    assert.equal(match('flag:nope flag:drifted', drifted), false)
    assert.equal(match('flag:drifted flag:nope', drifted), false)
  })

  it('two valid flags still union', () => {
    assert.equal(match('flag:drifted flag:vcs-error', ws('a', { conditions: ['vcs_error'] })), true)
  })

  it('every value in the flag list is recognised, and a removed spelling is not', () => {
    for (const v of FLAG_VALUES) assert.equal(match(`flag:${v}`, ws('a', { conditions: [v.replaceAll('-', '_')] })), true, v)
    assert.equal(match('flag:unhealthy', ws('a', { conditions: ['drifted'] })), false)
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

describe('migrateLegacyFilter', () => {
  const allFlags = FLAG_VALUES.map(v => `flag:${v}`).join(' ')

  it('rewrites each removed status spelling', () => {
    assert.equal(migrateLegacyFilter('status:locked'), 'locked:true')
    assert.equal(migrateLegacyFilter('status:unhealthy'), allFlags)
    assert.equal(migrateLegacyFilter('status:drifted'), 'flag:drifted')
    assert.equal(migrateLegacyFilter('status:state-diverged'), 'flag:state-diverged')
    assert.equal(migrateLegacyFilter('status:vcs-error'), 'flag:vcs-error')
  })

  it('treats = like :', () => {
    assert.equal(migrateLegacyFilter('status=locked'), 'locked:true')
    assert.equal(migrateLegacyFilter('status=drifted'), 'flag:drifted')
    assert.equal(migrateLegacyFilter('status=unhealthy'), allFlags)
  })

  it('keeps every other token exactly, in place', () => {
    assert.equal(migrateLegacyFilter('env:prod status:unhealthy eu1'), `env:prod ${allFlags} eu1`)
    assert.equal(migrateLegacyFilter('env=prod  status:locked\teu1'), 'env=prod  locked:true\teu1')
    assert.equal(migrateLegacyFilter(' status:locked status:errored '), ' locked:true status:errored ')
  })

  it('returns the identical string when nothing is legacy', () => {
    for (const q of [
      '',
      '   ',
      'env=prod',
      'env:prod  eu1 ',
      'status:errored status:planned',
      'locked:true flag:drifted',
      'status:typo',
      'status:',
    ]) {
      assert.equal(migrateLegacyFilter(q), q, JSON.stringify(q))
    }
  })

  it('matches case and quoting the way the parser does', () => {
    // The parser reads `status` and its values case-sensitively and has no quoting.
    for (const q of ['Status:locked', 'status:Locked', 'STATUS:UNHEALTHY', '"status:locked"', 'status:locked,', 'status:locked:x', 'status:constructor', 'status:toString']) {
      assert.equal(migrateLegacyFilter(q), q, q)
    }
  })

  it('adds no term the filter already has', () => {
    assert.equal(migrateLegacyFilter('status:locked locked:true'), 'locked:true')
    assert.equal(migrateLegacyFilter('locked:true status:locked'), 'locked:true')
    assert.equal(migrateLegacyFilter('locked=true status:locked eu1'), 'locked=true eu1')
    assert.equal(migrateLegacyFilter('status:locked status:locked'), 'locked:true')
    const others = FLAG_VALUES.filter(v => v !== 'drifted').map(v => `flag:${v}`).join(' ')
    assert.equal(migrateLegacyFilter('status:unhealthy flag:drifted'), `${others} flag:drifted`)
    assert.equal(migrateLegacyFilter('status:drifted flag=drifted'), 'flag=drifted')
    assert.equal(migrateLegacyFilter('status:drifted status:unhealthy'), `flag:drifted ${others}`)
  })

  it('leaves no stray whitespace when a term is dropped as a duplicate', () => {
    assert.equal(migrateLegacyFilter('locked:true status:locked status:locked'), 'locked:true')
    assert.equal(migrateLegacyFilter('locked:true status:locked'), 'locked:true')
    assert.equal(migrateLegacyFilter('eu1 locked:true status:locked'), 'eu1 locked:true')
    assert.equal(migrateLegacyFilter('status:locked locked:true eu1'), 'locked:true eu1')
    assert.equal(migrateLegacyFilter('eu1  status:locked locked:true  '), 'eu1  locked:true  ')
  })

  it('keeps status:locked status:errored meaning locked AND errored', () => {
    const q = migrateLegacyFilter('status:locked status:errored')
    assert.equal(q, 'locked:true status:errored')
    const parsed = parseFilterQuery(q)
    assert.equal(matchWorkspace(ws('a', { locked: true }), parsed, 'errored'), true)
    assert.equal(matchWorkspace(ws('a', { locked: false }), parsed, 'errored'), false)
    assert.equal(matchWorkspace(ws('a', { locked: true }), parsed, 'applied'), false)
  })

  it('keeps status:unhealthy status:errored meaning unhealthy AND errored', () => {
    const q = migrateLegacyFilter('status:unhealthy status:errored')
    assert.equal(q, `${allFlags} status:errored`)
    const parsed = parseFilterQuery(q)
    for (const code of FLAG_CODES) {
      assert.equal(matchWorkspace(ws('a', { conditions: [code] }), parsed, 'errored'), true, code)
    }
    assert.equal(matchWorkspace(ws('a'), parsed, 'errored'), false)
    assert.equal(matchWorkspace(ws('a', { conditions: ['drifted'] }), parsed, 'applied'), false)
  })

  it('maps status:drifted onto the server condition, not the badge', () => {
    const parsed = parseFilterQuery(migrateLegacyFilter('status:drifted'))
    assert.equal(matchWorkspace(ws('a', { conditions: ['drifted'] }), parsed, 'needs-confirm'), true)
    assert.equal(matchWorkspace(ws('a'), parsed, 'applied'), false)
  })
})

describe('countOfferedTerms', () => {
  const statuses = RUN_STATUSES.map(s => s.filter)
  const count = (q: string, kind: 'status' | 'flag') =>
    countOfferedTerms(parseFilterQuery(q), kind, kind === 'status' ? statuses : FLAG_VALUES)

  it('counts distinct offered values, not every term', () => {
    assert.equal(count('status:errored status:typo status:errored', 'status'), 1)
    assert.equal(count('flag:drifted flag:nope', 'flag'), 1)
    assert.equal(count('flag:drifted flag=drifted', 'flag'), 1)
  })

  it('counts only terms of its own kind', () => {
    assert.equal(count('status:errored flag:drifted locked:true env:prod', 'status'), 1)
    assert.equal(count('status:errored flag:drifted locked:true env:prod', 'flag'), 1)
  })

  it('is 0 for an empty filter, and for a legacy spelling the dropdown no longer offers', () => {
    assert.equal(count('', 'status'), 0)
    assert.equal(count('', 'flag'), 0)
    assert.equal(count('status:locked status:drifted', 'status'), 0)
  })

  it('counts all seven flags when every one is ticked', () => {
    assert.equal(FLAG_VALUES.length, 7)
    assert.equal(count(FLAG_VALUES.map(v => `flag:${v}`).join(' '), 'flag'), 7)
  })
})
