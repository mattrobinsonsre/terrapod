import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it } from 'node:test'

import {
  HOST_LOG_DELIMITER,
  HOST_LOG_HEADER,
  HOST_LOG_HEADER_V1,
  HOST_LOG_HOSTLESS,
  filterHostLog,
  parseHostLog,
  pickerHostList,
  stripHostTags,
  unescapeHost,
} from '../src/lib/host-log.ts'

/**
 * The per-host log parser (#2144, #2513).
 *
 * The format is written by an ansible callback plugin running under the
 * execution environment's Python, so the two sides cannot share a constant and
 * the literals are restated in TypeScript. That restatement is the risk, which
 * is why one block here reads the plugin's own source — including a complete
 * example block, which this parser has to accept.
 *
 * Both formats are covered. v2 is what the runner writes; v1 is what a stored
 * log written by an older runner image still carries, and a stored log outlives
 * the image that wrote it.
 */

const PLUGIN = join(
  import.meta.dirname,
  '..',
  '..',
  'services',
  'terrapod',
  'runner',
  'ansible_callback',
  'terrapod_stdout.py',
)

const H = HOST_LOG_HEADER
const H1 = HOST_LOG_HEADER_V1

/** One v2 block header, in the shape the callback writes. */
const block = (host: string, status: string, label: string, stamp = '[10:28:30 +2.9s]') =>
  `${stamp.padEnd(16)}  ${host.padEnd(16)}  ${status.padEnd(7)}  ${label}`.trimEnd()

/** One v2 body line: indented, so it belongs to the block above it. */
const body = (text: string) => `                  ${text}`

/** One v1 line: the superseded per-line host prefix. */
const tag = (host: string, text: string) => `${host.padEnd(10)}${HOST_LOG_DELIMITER}${text}`

describe('parseHostLog, v2 blocks', () => {
  it('attributes every line of a block, not just its header', () => {
    // The whole reason this is a callback rather than a parser over ansible's
    // own output: the dumped result's lines carry no host of their own, and in
    // v2 they carry no prefix either — the block they are indented under is
    // what attributes them.
    const log = [
      H,
      block('web1', 'ok', 'platform : say hello'),
      body('{'),
      body('    "msg": "hi"'),
      body('}'),
    ].join('\n')
    const parsed = parseHostLog(log)
    assert.equal(parsed.tagged, true)
    assert.equal(parsed.version, 2)
    assert.deepEqual(
      parsed.lines.map(l => l.host),
      ['web1', 'web1', 'web1', 'web1'],
    )
    assert.deepEqual(parsed.hosts, ['web1'])
  })

  it('treats the marker as belonging to no single host', () => {
    const log = [
      H,
      block(HOST_LOG_HOSTLESS, 'play', 'PLAY [configure]', '[2026-11-11 10:28:27]'),
      block('web1', 'changed', 'platform : deploy'),
    ].join('\n')
    const parsed = parseHostLog(log)
    assert.deepEqual(
      parsed.lines.map(l => l.host),
      [null, 'web1'],
    )
    assert.deepEqual(parsed.hosts, ['web1'], 'the marker is not a host')
  })

  it('closes a block when something unframed follows it', () => {
    // Another callback's line — `ansible.posix.profile_tasks` prints one at the
    // start of every task — is emitted exactly as it was written. It belongs to
    // no host, and it must not be swallowed by the block above it either.
    const profile = 'Wednesday 11 November 2026  10:28:27 +0000 (0:00:00.018) ****'
    const log = [H, block('web1', 'ok', 'a task'), body('{}'), profile, 'plain line'].join('\n')
    const parsed = parseHostLog(log)
    assert.deepEqual(
      parsed.lines.map(l => l.host),
      ['web1', 'web1', null, null],
    )
  })

  it('does not invent a host from a line that merely opens with a bracket', () => {
    // Pass-through output is emitted exactly as it was written, so a line of it
    // can start with anything. A loose `^\[[^\]]*\]\s+` grammar reads the second
    // word of `[custom] WARN disk is filling` as a host and the third as a
    // result, and the picker gains an entry called `WARN`. Requiring the
    // bracket to hold a TIME is what closes that, and it costs nothing: our own
    // producer always writes one.
    //
    // `[WARNING]:` and `[ERROR]:` are in here too — they are what ansible
    // itself prints outside a task — but they are not the hard case: the colon
    // already stops them.
    const log = [
      H,
      '[custom] WARN disk is filling on the build host',
      '[WARNING]: something about a python interpreter',
      '[ERROR]: boom',
    ].join('\n')
    const parsed = parseHostLog(log)
    assert.deepEqual(parsed.hosts, [], 'no host may be invented from a bracketed word')
    assert.equal(parsed.lines.length, 3, 'and nothing may be dropped either')
    assert.ok(
      parsed.lines.every(l => l.host === null),
      'every unframed line belongs to no host, so it shows in every view',
    )
  })

  it('keeps the lines that precede the header', () => {
    // The collection install writes galaxy output to the same artifact first,
    // and the orchestrator's own lines are rolled in around it. Dropping them
    // would lose real output.
    const log = ['Installing collection a.b | version 1.0', H, block('web1', 'ok', 't')].join('\n')
    const parsed = parseHostLog(log)
    assert.deepEqual(parsed.lines[0], {
      host: null,
      text: 'Installing collection a.b | version 1.0',
    })
    assert.deepEqual(parsed.hosts, ['web1'])
  })

  it('returns an empty result for no input rather than throwing', () => {
    for (const input of [null, undefined, '']) {
      const parsed = parseHostLog(input)
      assert.equal(parsed.tagged, false)
      assert.equal(parsed.version, null)
      assert.deepEqual(parsed.hosts, [])
    }
  })

  it('lists hosts in first-seen order and never twice', () => {
    const log = [
      H,
      block('web2', 'ok', 'a'),
      block('web1', 'ok', 'b'),
      block('web2', 'ok', 'c'),
    ].join('\n')
    assert.deepEqual(parseHostLog(log).hosts, ['web2', 'web1'])
  })

  it('reports an unframed log as unframed rather than as empty', () => {
    // An older runner image emits ansible's plain output. The viewer keys the
    // per-host switch on this, so reporting it as tagged-but-hostless would
    // offer a picker with nothing in it.
    const parsed = parseHostLog('PLAY [x] ***\nok: [web1]\n')
    assert.equal(parsed.tagged, false)
    assert.deepEqual(parsed.hosts, [])
    assert.ok(parsed.lines.length >= 2, 'the lines are still there to render')
  })
})

describe('parseHostLog, v1 prefixes (a stored log outlives its runner)', () => {
  it('still attributes every line of a multi-line result', () => {
    const log = [H1, tag('web1', 'ok: [web1] => {'), tag('web1', '    "msg": "hi"')].join('\n')
    const parsed = parseHostLog(log)
    assert.equal(parsed.tagged, true)
    assert.equal(parsed.version, 1)
    assert.deepEqual(parsed.hosts, ['web1'])
    assert.equal(parsed.lines[1].text, '    "msg": "hi"', 'the prefix is removed, as it was')
  })

  it('still treats the marker as belonging to no single host', () => {
    const log = [H1, tag(HOST_LOG_HOSTLESS, 'TASK [x] ***'), tag('web1', 'ok: [web1]')].join('\n')
    const parsed = parseHostLog(log)
    assert.deepEqual(
      parsed.lines.map(l => l.host),
      [null, 'web1'],
    )
    assert.deepEqual(parsed.hosts, ['web1'])
  })

  it('does not drop a line it cannot parse', () => {
    const log = [H1, 'something printed straight to the terminal', tag('web1', 'ok')].join('\n')
    const parsed = parseHostLog(log)
    assert.equal(parsed.lines.length, 2)
    assert.equal(parsed.lines[0].host, null, 'unparsed is context, never hidden')
  })
})

describe('unescapeHost', () => {
  it('reverses the escaping of every character the format reserves', () => {
    // The callback escapes `%`, `|`, `~` and whitespace, because
    // `validate_host_name` does not refuse `|` and a host from a git inventory
    // source never passes through it at all.
    assert.equal(unescapeHost('we%7Cird'), 'we|ird')
    assert.equal(unescapeHost('ti%7Elde'), 'ti~lde')
    assert.equal(unescapeHost('per%25cent'), 'per%cent')
    assert.equal(unescapeHost('a%20b'), 'a b')
  })

  it('leaves an ordinary name untouched', () => {
    assert.equal(unescapeHost('web1.example.com'), 'web1.example.com')
  })

  it('recovers a host whose name carries whitespace without splitting the header', () => {
    // The one v2 actually rests on: the host is the first whitespace-delimited
    // field, so an unescaped space would be read as a host and a result.
    const log = [H, block('spa%20ced', 'ok', 'a task')].join('\n')
    const parsed = parseHostLog(log)
    assert.deepEqual(parsed.hosts, ['spa ced'])
    assert.equal(parsed.lines[0].host, 'spa ced')
  })
})

describe('stripHostTags', () => {
  it('yields the whole v2 log without its header', () => {
    // There is no second rendering to fall back to: the blocks ARE the log.
    const log = [H, block('web1', 'ok', 'a task'), body('{}')].join('\n')
    assert.equal(stripHostTags(log), [block('web1', 'ok', 'a task'), body('{}')].join('\n'))
  })

  it('yields ansible’s own words for a v1 log', () => {
    const log = [H1, tag(HOST_LOG_HOSTLESS, 'TASK [x] ***'), tag('web1', 'ok: [web1]')].join('\n')
    assert.equal(stripHostTags(log), 'TASK [x] ***\nok: [web1]')
  })

  it('is a no-op on an unframed log', () => {
    const plain = 'PLAY [x] ***\nok: [web1]\n'
    assert.equal(stripHostTags(plain), plain)
  })
})

describe('filterHostLog', () => {
  const log = [
    H,
    block(HOST_LOG_HOSTLESS, 'play', 'PLAY [configure]', '[2026-11-11 10:28:27]'),
    block('web1', 'ok', 'platform : first'),
    body('  "msg": "from web1"'),
    block('web2', 'FAILED', 'platform : first'),
    body('  "msg": "from web2"'),
    block(HOST_LOG_HOSTLESS, 'recap', 'PLAY RECAP', '[2026-11-11 10:29:01]'),
    body('web1 : ok=1'),
  ].join('\n')

  it('keeps one host’s whole block, body and all', () => {
    const out = filterHostLog(parseHostLog(log), 'web1')
    assert.match(out, /from web1/)
    assert.doesNotMatch(out, /from web2/, 'a body line must go with its own block')
    assert.doesNotMatch(out, /FAILED/)
  })

  it('keeps the shared context, because a result with no play is unreadable', () => {
    const out = filterHostLog(parseHostLog(log), 'web1')
    assert.match(out, /PLAY \[configure\]/, 'the banner says which play this is')
    assert.match(out, /PLAY RECAP/)
    assert.match(out, /web1 : ok=1/, 'the recap is one host-less block, body included')
  })

  it('yields only context for a host that produced nothing', () => {
    // A host in the inventory that the run never reached is exactly what
    // someone looks for, so selecting it must not look like an error.
    const out = filterHostLog(parseHostLog(log), 'db1')
    assert.match(out, /PLAY \[configure\]/)
    assert.doesNotMatch(out, /from web1/)
    assert.doesNotMatch(out, /from web2/)
  })
})

describe('the format agrees with the plugin that writes it', () => {
  const src = readFileSync(PLUGIN, 'utf8')
  const literal = (name: string) => {
    const m = src.match(new RegExp(`^${name} = "([^"]*)"`, 'm'))
    assert.ok(m, `could not read ${name} from the plugin — has it been renamed?`)
    return m![1]
  }

  it('uses the plugin’s header', () => {
    assert.equal(
      HOST_LOG_HEADER,
      literal('HEADER'),
      'the viewer would stop recognising a framed log and silently drop the per-host switch',
    )
  })

  it('uses the plugin’s host-less marker', () => {
    assert.equal(
      HOST_LOG_HOSTLESS,
      literal('HOSTLESS'),
      'play-level output would be attributed to a host literally named as the marker',
    )
  })

  it('parses the plugin’s own example block', () => {
    // The strongest tie available across the process boundary: the plugin
    // carries one complete block header in the exact shape it writes, and this
    // parser has to agree about which field is the host. A grammar that
    // narrowed, or a column that moved, fails here rather than in production.
    const example = literal('EXAMPLE_BLOCK')
    const parsed = parseHostLog([HOST_LOG_HEADER, example].join('\n'))
    assert.equal(parsed.tagged, true)
    assert.deepEqual(parsed.hosts, ['web1'], `the example did not parse: ${example}`)
    assert.equal(parsed.lines[0].text, example, 'and the line itself is returned untouched')
  })

  it('reads the plugin’s indent as a body line', () => {
    const indent = literal('INDENT')
    assert.equal(indent.trim(), '', 'a body line’s indent must be whitespace')
    const parsed = parseHostLog(
      [HOST_LOG_HEADER, literal('EXAMPLE_BLOCK'), `${indent}"msg": "hi"`].join('\n'),
    )
    assert.equal(parsed.lines[1].host, 'web1', 'the body line lost its block')
  })
})

describe('pickerHostList', () => {
  const rows = [
    { host: 'web1', status: 'ok' },
    { host: 'web2', status: 'ignored-failure' },
    { host: 'db2', status: 'not-reached' },
  ]

  it('offers a targeted host that has produced no output at all', () => {
    // The requirement, and the reason the picker cannot be built from the log:
    // `db2` appears in no block, and an inventory host the run has not reached
    // is exactly what someone goes looking for. A picker built from the log
    // alone omits the one entry the control exists to provide.
    const out = pickerHostList(['web1', 'web2'], rows)
    assert.deepEqual(
      out.map(h => h.host),
      ['web1', 'web2', 'db2'],
    )
  })

  it('keeps the log’s first-seen order and appends the silent hosts after it', () => {
    // First-seen is meaningful — it is the order they began working — and
    // appending keeps it stable as output arrives: a host moves out of the tail
    // into its first-seen position exactly once, never shuffling the rest.
    const out = pickerHostList(['web2', 'web1'], rows)
    assert.deepEqual(
      out.map(h => h.host),
      ['web2', 'web1', 'db2'],
    )
  })

  it('attaches the server’s status and never invents one', () => {
    const out = pickerHostList(['web1', 'web9'], rows)
    assert.deepEqual(out, [
      { host: 'web1', status: 'ok' },
      // Named in the log, absent from the recap: no status, not a guess. A dead
      // host reports `ok` for any task that needs no connection, so status is a
      // property of the tasks that touched it rather than of the host (#1928).
      { host: 'web9', status: '' },
      // Both recap hosts the log has not named are appended, not just one.
      { host: 'web2', status: 'ignored-failure' },
      { host: 'db2', status: 'not-reached' },
    ])
  })

  it('never lists a host twice when it is in both sources', () => {
    const out = pickerHostList(['web1', 'web2', 'db2'], rows)
    assert.equal(out.length, 3)
    assert.equal(new Set(out.map(h => h.host)).size, 3)
  })

  it('degrades to the log’s hosts when the targeted read gave nothing', () => {
    // An unreachable `/host-results` must not remove a control that works.
    assert.deepEqual(pickerHostList(['web1'], []), [{ host: 'web1', status: '' }])
  })

  it('offers the targeted hosts even before any output has arrived', () => {
    // Mid-run, before the first block: the list is already complete, because
    // `/host-results` is complete from run creation.
    assert.deepEqual(
      pickerHostList([], rows).map(h => h.host),
      ['web1', 'web2', 'db2'],
    )
  })
})

// ── #2413 F18 / #2513: the framing must survive a COLOURED stream ─────────

describe('a coloured configure log', () => {
  // The configure runs with `ANSIBLE_FORCE_COLOR=1`, to match what terraform
  // and tofu already emit, so every line this parses can carry SGR sequences.
  //
  // In v2 the WHOLE BLOCK is coloured by its result, so colour appears at the
  // START of every line rather than after a delimiter — the inverse of v1,
  // where it could only ever appear after the first ` | `. That is why the
  // parser strips a LEADING run of SGR before matching, and these pin it.
  const ESC = '\u001b'
  const red = (text: string) => `${ESC}[0;31m${text}${ESC}[0m`
  const green = (text: string) => `${ESC}[0;32m${text}${ESC}[0m`

  it('keeps the host, and leaves the colour in the text', () => {
    const line = green(block('web1', 'ok', 'platform : say hello'))
    const parsed = parseHostLog([H, line].join('\n'))
    assert.equal(parsed.tagged, true)
    assert.deepEqual(parsed.hosts, ['web1'])
    assert.equal(parsed.lines[0].text, line, 'colour is preserved byte for byte')
  })

  it('attributes a coloured body line to its coloured block', () => {
    const parsed = parseHostLog(
      [H, red(block('web2', 'FAILED', 'platform : check')), red(body('"msg": "boom"'))].join('\n'),
    )
    assert.deepEqual(
      parsed.lines.map(l => l.host),
      ['web2', 'web2'],
    )
  })

  it('does not invent a host from a colour code on a host-less block', () => {
    // The failure worth ruling out: if an SGR sequence ever defeated the
    // marker test, the picker would gain a host whose name is an escape
    // sequence.
    const parsed = parseHostLog(
      [H, green(block(HOST_LOG_HOSTLESS, 'play', 'PLAY [x]', '[2026-11-11 10:28:27]'))].join('\n'),
    )
    assert.equal(parsed.lines[0].host, null)
    assert.deepEqual(parsed.hosts, [], 'no host may be invented from a colour code')
  })

  it('keeps the host-less blocks when filtered, colour and all', () => {
    const log = [
      H,
      green(block(HOST_LOG_HOSTLESS, 'play', 'PLAY [x]', '[2026-11-11 10:28:27]')),
      green(block('web1', 'changed', 'platform : one')),
      green(block('web2', 'changed', 'platform : one')),
    ].join('\n')
    const only = filterHostLog(parseHostLog(log), 'web1')
    assert.match(only, /PLAY \[x\]/, 'a banner is the context a block attaches to')
    assert.match(only, /web1/)
    assert.doesNotMatch(only, /web2/)
    assert.ok(only.includes(ESC), 'colour is preserved through the filter')
  })
})

// ---------------------------------------------------------------------------
// The filter has an address (#2413 F17).
//
// `hostFilter` was `useState`-only, so "the configure log, filtered to this
// host" could not be linked to, survive a reload, or be shared. AGENTS.md
// forbids that outright for view state — "URL is the source of truth for
// tab/view state … no `useState`-only tabs" — and this is view state by the
// panel's own description: it is the one control there that changes what the
// pane CONTAINS rather than how it is displayed.
//
// The behaviour is proven in `e2e/tests/configure-log.spec.ts`, which drives a
// real tagged log through a real browser. What is asserted here is the shape
// that behaviour rests on, because the defect form is a one-word edit —
// `useState(initialHost)` back to `useState('')` — and it would leave every
// e2e passing on a page that silently stopped writing the URL.

const RUN_PAGE = readFileSync(
  join(
    import.meta.dirname,
    '..',
    'src',
    'app',
    'workspaces',
    '[id]',
    'runs',
    '[runId]',
    'page.tsx',
  ),
  'utf8',
)

describe('the per-host filter is URL state, not component state', () => {
  it('initialises from the param rather than from an empty literal', () => {
    assert.ok(
      !/const \[hostFilter, setHostFilter\] = useState\(\s*''\s*\)/.test(RUN_PAGE),
      'hostFilter is back to useState(\'\') — the filter has no address again',
    )
    assert.match(
      RUN_PAGE,
      /const \[hostFilter, setHostFilter\] = useState\(initialHost\)/,
      'the filter must start from the URL',
    )
  })

  it('reads the param on the page and hands it to every log panel', () => {
    assert.match(
      RUN_PAGE,
      /searchParams\.get\('host'\)/,
      'nothing reads the host param, so a deep link cannot arrive filtered',
    )
    // All three panels, deliberately: the picker and the filter are both gated
    // on the LOG carrying host tags, so a phase that gains tagging later needs
    // no change at the call site. A count below three means one was missed.
    const wired = (RUN_PAGE.match(/initialHost=\{hostParam\}/g) ?? []).length
    assert.equal(wired, 3, `only ${wired} of 3 log panels take the host param`)
    assert.equal(
      (RUN_PAGE.match(/onHostChange=\{switchHost\}/g) ?? []).length,
      3,
      'a panel that cannot write the param gives a filter with no address',
    )
  })

  it('writes the param when a host is picked, and removes it for All hosts', () => {
    // Removing it matters as much as setting it: a stale `host=` in a shared
    // link would filter someone else's first view of the log to one host.
    assert.match(RUN_PAGE, /onHostChange\?\.\(e\.target\.value\)/, 'the picker must write back')
    const writer = RUN_PAGE.match(
      /const switchHost = useCallback\(\(host: string\) => \{[\s\S]*?\}, \[\]\)/,
    )
    assert.ok(writer, 'switchHost is gone')
    assert.match(writer![0], /searchParams\.set\('host', host\)/)
    assert.match(writer![0], /searchParams\.delete\('host'\)/)
  })
})
