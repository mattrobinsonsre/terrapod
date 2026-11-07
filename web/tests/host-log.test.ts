import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it } from 'node:test'

import {
  HOST_LOG_DELIMITER,
  HOST_LOG_HEADER,
  HOST_LOG_HOSTLESS,
  filterHostLog,
  parseHostLog,
  pickerHostList,
  stripHostTags,
  unescapeHost,
} from '../src/lib/host-log.ts'

/**
 * The per-host log parser (#2144).
 *
 * The format is written by an ansible callback plugin running under the
 * execution environment's Python, so the two sides cannot share a constant and
 * the literals are restated in TypeScript. That restatement is the risk, which
 * is why the last block here reads the plugin's own source.
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
const tag = (host: string, text: string) => `${host.padEnd(10)}${HOST_LOG_DELIMITER}${text}`

describe('parseHostLog', () => {
  it('attributes every line of a multi-line result, not just the first', () => {
    // The whole reason this is a callback rather than a parser over ansible's
    // own output: `ok: [web1] => {` names the host and the lines that follow
    // it do not.
    const log = [H, tag('web1', 'ok: [web1] => {'), tag('web1', '    "msg": "hi"'), tag('web1', '}')].join('\n')
    const parsed = parseHostLog(log)
    assert.equal(parsed.tagged, true)
    assert.deepEqual(
      parsed.lines.map(l => l.host),
      ['web1', 'web1', 'web1'],
    )
    assert.deepEqual(parsed.hosts, ['web1'])
  })

  it('treats the marker as belonging to no single host', () => {
    const log = [H, tag(HOST_LOG_HOSTLESS, 'TASK [x] ***'), tag('web1', 'ok: [web1]')].join('\n')
    const parsed = parseHostLog(log)
    assert.deepEqual(
      parsed.lines.map(l => l.host),
      [null, 'web1'],
    )
    assert.deepEqual(parsed.hosts, ['web1'], 'the marker is not a host')
  })

  it('reports an untagged log as untagged rather than as empty', () => {
    // An older runner image emits ansible's plain output. The viewer keys the
    // per-host switch on this, so reporting it as tagged-but-hostless would
    // offer a picker with nothing in it.
    const parsed = parseHostLog('PLAY [x] ***\nok: [web1]\n')
    assert.equal(parsed.tagged, false)
    assert.deepEqual(parsed.hosts, [])
    assert.ok(parsed.lines.length >= 2, 'the lines are still there to render')
  })

  it('keeps the lines that precede the header', () => {
    // The collection install writes galaxy output to the same artifact first,
    // and the orchestrator's own lines are rolled in around it. Dropping them
    // would lose real output; parsing them would eat the first pipe out of an
    // ordinary line of prose.
    const log = ['Installing collection a.b | version 1.0', H, tag('web1', 'ok: [web1]')].join('\n')
    const parsed = parseHostLog(log)
    assert.deepEqual(parsed.lines[0], {
      host: null,
      text: 'Installing collection a.b | version 1.0',
    })
    assert.deepEqual(parsed.hosts, ['web1'])
  })

  it('does not drop a line it cannot parse', () => {
    const log = [H, 'something printed straight to the terminal', tag('web1', 'ok')].join('\n')
    const parsed = parseHostLog(log)
    assert.equal(parsed.lines.length, 2)
    assert.equal(parsed.lines[0].text, 'something printed straight to the terminal')
    assert.equal(parsed.lines[0].host, null, 'unparsed is context, never hidden')
  })

  it('returns an empty result for no input rather than throwing', () => {
    for (const input of [null, undefined, '']) {
      const parsed = parseHostLog(input)
      assert.equal(parsed.tagged, false)
      assert.deepEqual(parsed.hosts, [])
    }
  })

  it('lists hosts in first-seen order and never twice', () => {
    const log = [H, tag('web2', 'a'), tag('web1', 'b'), tag('web2', 'c')].join('\n')
    assert.deepEqual(parseHostLog(log).hosts, ['web2', 'web1'])
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

  it('recovers a host carrying the delimiter without breaking the split', () => {
    const log = [H, tag('we%7Cird', 'ok: [we|ird]')].join('\n')
    const parsed = parseHostLog(log)
    assert.deepEqual(parsed.hosts, ['we|ird'])
    assert.equal(parsed.lines[0].text, 'ok: [we|ird]')
  })
})

describe('stripHostTags', () => {
  it('yields ansible’s own words', () => {
    const log = [H, tag(HOST_LOG_HOSTLESS, 'TASK [x] ***'), tag('web1', 'ok: [web1]')].join('\n')
    assert.equal(stripHostTags(log), 'TASK [x] ***\nok: [web1]')
  })

  it('is a no-op on an untagged log', () => {
    const plain = 'PLAY [x] ***\nok: [web1]\n'
    assert.equal(stripHostTags(plain), plain)
  })
})

describe('filterHostLog', () => {
  const log = [
    H,
    tag(HOST_LOG_HOSTLESS, 'TASK [first] ***'),
    tag('web1', 'ok: [web1]'),
    tag('web2', 'ok: [web2]'),
    tag(HOST_LOG_HOSTLESS, 'PLAY RECAP ***'),
  ].join('\n')

  it('keeps one host’s lines', () => {
    const out = filterHostLog(parseHostLog(log), 'web1')
    assert.ok(out.includes('ok: [web1]'))
    assert.ok(!out.includes('ok: [web2]'))
  })

  it('keeps the shared context, because a result with no banner is unreadable', () => {
    const out = filterHostLog(parseHostLog(log), 'web1')
    assert.ok(out.includes('TASK [first] ***'), 'the banner says which task this is')
    assert.ok(out.includes('PLAY RECAP ***'))
  })

  it('yields only context for a host that produced nothing', () => {
    // A host in the inventory that the run never reached is exactly what
    // someone looks for, so selecting it must not look like an error.
    const out = filterHostLog(parseHostLog(log), 'db1')
    assert.equal(out, 'TASK [first] ***\nPLAY RECAP ***')
  })
})

describe('the format agrees with the plugin that writes it', () => {
  const src = readFileSync(PLUGIN, 'utf8')
  const literal = (name: string) => {
    const m = src.match(new RegExp(`^${name} = "([^"]+)"`, 'm'))
    assert.ok(m, `could not read ${name} from the plugin — has it been renamed?`)
    return m![1]
  }

  it('uses the plugin’s header', () => {
    assert.equal(
      HOST_LOG_HEADER,
      literal('HEADER'),
      'the viewer would stop recognising a tagged log and silently drop the per-host switch',
    )
  })

  it('uses the plugin’s delimiter', () => {
    assert.equal(HOST_LOG_DELIMITER, literal('DELIMITER'))
  })

  it('uses the plugin’s host-less marker', () => {
    assert.equal(
      HOST_LOG_HOSTLESS,
      literal('HOSTLESS'),
      'banners would be attributed to a host literally named as the marker',
    )
  })

  it('parses a line padded to the plugin’s own width', () => {
    const pad = Number(src.match(/^_PAD = (\d+)/m)![1])
    const line = 'web1'.padEnd(pad) + HOST_LOG_DELIMITER + 'ok: [web1]'
    const parsed = parseHostLog([HOST_LOG_HEADER, line].join('\n'))
    assert.deepEqual(parsed.lines[0], { host: 'web1', text: 'ok: [web1]' })
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
    // `db2` appears in no tagged line, and an inventory host the run has not
    // reached is exactly what someone goes looking for. A picker built from the
    // log alone omits the one entry the control exists to provide.
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
    // Mid-run, before the first tagged line: the list is already complete,
    // because `/host-results` is complete from run creation.
    assert.deepEqual(
      pickerHostList([], rows).map(h => h.host),
      ['web1', 'web2', 'db2'],
    )
  })
})

// ── #2413 F18: the tags must survive a COLOURED stream ────────────────────

describe('a coloured configure log', () => {
  // The configure now runs with `ANSIBLE_FORCE_COLOR=1`, to match what
  // terraform and tofu already emit, so every line this parses can carry SGR
  // sequences.
  //
  // It is safe by CONSTRUCTION rather than by luck, and that is what these pin:
  // the callback builds `prefix + line` (`_tp_tag`), so the host field and the
  // delimiter are always written BEFORE ansible's bytes. Colour can therefore
  // only appear after the first ` | `, where it is opaque text to this parser.
  // If the callback ever colours its own prefix, these fail.
  const ESC = '\u001b'

  it('keeps the host, and leaves the colour in the text', () => {
    const log = [
      HOST_LOG_HEADER,
      `web1       ${HOST_LOG_DELIMITER}${ESC}[0;32mok: [web1]${ESC}[0m`,
    ].join('\n')
    const parsed = parseHostLog(log)
    assert.equal(parsed.tagged, true)
    assert.deepEqual(parsed.hosts, ['web1'])
    assert.equal(parsed.lines[0].host, 'web1')
    assert.equal(parsed.lines[0].text, `${ESC}[0;32mok: [web1]${ESC}[0m`)
  })

  it('does not invent a host from a colour code on a host-less line', () => {
    // The failure worth ruling out: if an SGR sequence ever preceded the
    // marker, `field === '~'` would be false and the picker would gain a host
    // whose name is an escape sequence.
    const log = [
      HOST_LOG_HEADER,
      `${HOST_LOG_HOSTLESS}          ${HOST_LOG_DELIMITER}${ESC}[0;34mTASK [say hello] ***${ESC}[0m`,
    ].join('\n')
    const parsed = parseHostLog(log)
    assert.equal(parsed.lines[0].host, null)
    assert.deepEqual(parsed.hosts, [], 'no host may be invented from a colour code')
  })

  it('keeps the host-less banners when filtered, colour and all', () => {
    const log = [
      HOST_LOG_HEADER,
      `${HOST_LOG_HOSTLESS}          ${HOST_LOG_DELIMITER}${ESC}[0;34mTASK [one] ***${ESC}[0m`,
      `web1       ${HOST_LOG_DELIMITER}${ESC}[0;32mchanged: [web1]${ESC}[0m`,
      `web2       ${HOST_LOG_DELIMITER}${ESC}[0;32mchanged: [web2]${ESC}[0m`,
    ].join('\n')
    const only = filterHostLog(parseHostLog(log), 'web1')
    assert.match(only, /TASK \[one\]/, 'a banner is the context a result attaches to')
    assert.match(only, /changed: \[web1\]/)
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
