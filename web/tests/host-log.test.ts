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
