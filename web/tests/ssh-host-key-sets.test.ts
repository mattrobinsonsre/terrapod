/**
 * The browser's copy of the SSH host-key vocabulary equals the server's (#2592).
 *
 * `SSH_HOST_KEY_TYPES`, `SSH_HOST_KEY_MARKERS` and `SSH_HOST_KEY_POLICIES` are
 * closed sets the API validates against, and there is no endpoint to ask — so
 * the picker on the inventory tab and the policy select on four workspace
 * surfaces restate them. A restated closed set is the drift this
 * repository keeps recording, and the direction of the drift here is the
 * expensive one: a type the server accepts and the picker does not offer is
 * simply unreachable from the UI, while a type the picker offers and the server
 * does not accept is a 422 an operator cannot act on.
 *
 * DERIVED from `db/models.py` rather than listed twice, so an eighth algorithm
 * fails here. The ORDER is deliberately not asserted: the module puts ed25519
 * first and `ssh-rsa` last on purpose, and the server's frozenset has no order
 * to disagree with. The policy set has an order on BOTH sides and it happens to
 * agree, which is still not something to assert: the orders are chosen
 * independently — ours is "dangerous end last", for a picker — so pinning them
 * equal would fail on a correct change to either.
 *
 * The splitter is pinned here too, with literal fixtures, because it is what
 * decides whether a pasted line is reinterpreted — and the one input that must
 * NOT be split (an algorithm field that is not one of the seven) is the one a
 * shape-only check would get wrong.
 *
 * Run with: npm run test:unit   (node:test, no test framework dependency)
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

import {
  DEFAULT_SSH_HOST_KEY_POLICY,
  SSH_HOST_KEY_MARKERS,
  SSH_HOST_KEY_POLICIES,
  SSH_HOST_KEY_TYPES,
  asSshHostKeyPolicy,
  splitKnownHostsEntry,
} from '../src/lib/ssh-host-keys.ts'

const ROOT = join(import.meta.dirname, '..', '..')
const MODELS = readFileSync(join(ROOT, 'services', 'terrapod', 'db', 'models.py'), 'utf8')
const EN = JSON.parse(readFileSync(join(import.meta.dirname, '..', 'messages', 'en.json'), 'utf8'))
const COPY: Record<string, string> = EN.workspaceDetail.inventory.hostKeys

/** The string members of a `frozenset({...})` assigned to `name` in models.py. */
function serverSet(name: string): string[] {
  const at = MODELS.indexOf(`${name}: frozenset[str] = frozenset(`)
  assert.ok(at >= 0, `could not find ${name} in models.py — has it been renamed?`)
  // Brace-matched from the opening `(` rather than read to the first `)`: the
  // declaration spans several lines and a non-greedy match to `)` stops at the
  // inner `frozenset(` call's own close on the single-line form.
  const open = MODELS.indexOf('(', at + name.length)
  let depth = 0
  let end = -1
  for (let i = open; i < MODELS.length; i += 1) {
    if (MODELS[i] === '(') depth += 1
    else if (MODELS[i] === ')') {
      depth -= 1
      if (depth === 0) {
        end = i
        break
      }
    }
  }
  assert.ok(end > open, `unbalanced parentheses reading ${name}`)
  const members = [...MODELS.slice(open, end).matchAll(/"([^"]+)"/g)].map((m) => m[1])
  assert.ok(members.length > 0, `${name} parsed to no members — the matcher has stopped matching`)
  return members
}

/** The string members of a `tuple[str, ...] = (...)` assigned to `name`. */
function serverTuple(name: string): string[] {
  const at = MODELS.indexOf(`${name}: tuple[str, ...] = (`)
  assert.ok(at >= 0, `could not find ${name} in models.py — has it been renamed?`)
  const open = MODELS.indexOf('(', at + name.length)
  const end = MODELS.indexOf(')', open)
  assert.ok(end > open, `unbalanced parentheses reading ${name}`)
  const members = [...MODELS.slice(open, end).matchAll(/"([^"]+)"/g)].map((m) => m[1])
  assert.ok(members.length > 0, `${name} parsed to no members — the matcher has stopped matching`)
  return members
}

/** The `value=` of every `<option>` in a named `<select>` of a page source. */
function optionValues(source: string, id: string): string[] {
  const at = source.indexOf(`id="${id}"`)
  assert.ok(at >= 0, `no <select id="${id}"> in that source — has it been renamed?`)
  const end = source.indexOf('</select>', at)
  assert.ok(end > at, `the <select id="${id}"> never closes`)
  return [...source.slice(at, end).matchAll(/<option value="([^"]*)"/g)].map((m) => m[1])
}

describe('the host-key vocabulary matches the server', () => {
  it('the algorithms are exactly the server s SSH_HOST_KEY_TYPES', () => {
    assert.deepEqual(
      [...SSH_HOST_KEY_TYPES].sort(),
      serverSet('SSH_HOST_KEY_TYPES').sort(),
      'the picker and the API disagree about which algorithms exist',
    )
  })

  it('the markers are exactly the server s SSH_HOST_KEY_MARKERS', () => {
    assert.deepEqual(
      [...SSH_HOST_KEY_MARKERS].sort(),
      serverSet('SSH_HOST_KEY_MARKERS').sort(),
      'the marker picker and the API disagree about which markers exist',
    )
  })

  it('no marker carries the leading @ the server refuses', () => {
    // The API strips nothing and refuses a leading `@` by name — Terrapod adds
    // it when it renders the line — so a `@`-prefixed value here would be a
    // picker whose every option is rejected.
    for (const marker of SSH_HOST_KEY_MARKERS) {
      assert.ok(!marker.startsWith('@'), `${marker} carries a leading @`)
    }
  })
})

describe('the host-key POLICY vocabulary matches the server', () => {
  // The third closed set, and the one that is not a `known_hosts` field: it is
  // OpenSSH's `StrictHostKeyChecking`, which decides whether the rendered file
  // is consulted at all. It replaced a boolean, so the drift to watch for is a
  // UI that offers two of the three — which would leave `accept-new`
  // unreachable, and `accept-new` is the whole reason the boolean went.
  it('the policies are exactly the server s SSH_HOST_KEY_POLICIES', () => {
    assert.deepEqual(
      [...SSH_HOST_KEY_POLICIES].sort(),
      serverTuple('SSH_HOST_KEY_POLICIES').sort(),
      'the policy select and the API disagree about which policies exist',
    )
  })

  it('the default is the server s DEFAULT_SSH_HOST_KEY_POLICY', () => {
    // Read as a named constant rather than as the first member of the tuple:
    // the server declares it separately, and the two could legitimately differ.
    const m = MODELS.match(/DEFAULT_SSH_HOST_KEY_POLICY = "([^"]+)"/)
    assert.ok(m, 'could not find DEFAULT_SSH_HOST_KEY_POLICY in models.py')
    assert.equal(
      DEFAULT_SSH_HOST_KEY_POLICY,
      m[1],
      'the forms would seed a policy the column does not default to',
    )
  })

  it('bulk update offers the three, plus its own leave-unchanged', () => {
    // The ONE surface that restates the three by hand, because its values
    // carry a fourth state the shared select has no room for. A hand-restated
    // closed set beside a derived one is exactly the drift this file exists
    // for, so it is derived too — from the page's own markup.
    const bulk = readFileSync(
      join(import.meta.dirname, '..', 'src', 'app', 'admin', 'bulk-update', 'page.tsx'),
      'utf8',
    )
    const offered = optionValues(bulk, 'bu-ssh-host-key-policy')
    assert.deepEqual(
      offered.filter((v) => v !== '').sort(),
      [...SSH_HOST_KEY_POLICIES].sort(),
      'the fleet form offers a different policy set from the per-workspace one',
    )
    assert.ok(
      offered.includes(''),
      "bulk update has lost its leave-unchanged option, so a fleet write would set this on every matched workspace",
    )
  })

  it('every policy has a label, and no two share one', () => {
    // The labels are what an operator picks between, so two policies reading
    // the same is a select with a state nobody can choose deliberately.
    const labels = [
      EN.workspaceDetail.fields.sshHostKeyPolicyStrict,
      EN.workspaceDetail.fields.sshHostKeyPolicyAcceptNew,
      EN.workspaceDetail.fields.sshHostKeyPolicyOff,
    ]
    assert.equal(labels.length, SSH_HOST_KEY_POLICIES.length, 'a policy has no label of its own')
    for (const l of labels) assert.equal(typeof l, 'string', 'a policy label is missing from en.json')
    assert.equal(new Set(labels).size, labels.length, `two policies share a label: ${labels}`)
  })

  it('a value the server could send lands on a real option', () => {
    // Total by construction, which is what stops the select rendering blank
    // and losing the setting on the next save. `false` is the boolean this
    // replaced, and it has to read as `off` rather than fall through to the
    // default — the fallthrough would tell an operator their fleet is
    // verifying while the server says it is not.
    for (const p of SSH_HOST_KEY_POLICIES) assert.equal(asSshHostKeyPolicy(p), p)
    assert.equal(asSshHostKeyPolicy(false), 'off')
    assert.equal(asSshHostKeyPolicy(true), DEFAULT_SSH_HOST_KEY_POLICY)
    assert.equal(asSshHostKeyPolicy(undefined), DEFAULT_SSH_HOST_KEY_POLICY)
    assert.equal(asSshHostKeyPolicy('banana'), DEFAULT_SSH_HOST_KEY_POLICY)
  })

  it('no catalogue carries a policy WIRE value as a string', () => {
    // The three are what the API stores and what a `terrapod_workspace`
    // resource declares, so they are identifiers rather than copy. Left in the
    // catalogues, `en-x-leet` would substitute `strict` into `57r1c7` and the
    // form would send that to a server which refuses it.
    const dir = join(import.meta.dirname, '..', 'messages')
    const files = readdirSync(dir).filter((f) => f.endsWith('.json'))
    assert.ok(files.length >= 25, `only ${files.length} catalogues found`)
    const offenders: string[] = []
    for (const f of files) {
      const flat = (o: Record<string, unknown>, p = '', out: Record<string, string> = {}) => {
        for (const [k, v] of Object.entries(o)) {
          const kk = p ? `${p}.${k}` : k
          if (v && typeof v === 'object') flat(v as Record<string, unknown>, kk, out)
          else if (typeof v === 'string') out[kk] = v
        }
        return out
      }
      for (const [key, value] of Object.entries(
        flat(JSON.parse(readFileSync(join(dir, f), 'utf8'))),
      )) {
        if (!key.includes('sshHostKeyPolicy')) continue
        if ((SSH_HOST_KEY_POLICIES as readonly string[]).includes(value.trim().toLowerCase())) {
          offenders.push(`${f}:${key} = ${value}`)
        }
      }
    }
    assert.deepEqual(offenders, [], `a catalogue carries a wire value:\n  ${offenders.join('\n  ')}`)
  })
})

describe('the copy names the badges by the words the badges carry', () => {
  // Two translators found this independently, which is the measure of how
  // invisible it is in English. `markerNone` was renamed "Trusted" ->
  // "Approved" — because nothing yet enforces any of this, so a badge
  // asserting trust was claiming an enforcement that does not exist — and the
  // strings that REFER to that badge were not swept. `confirmClear` went on
  // saying "it becomes an ordinary trusted key", pointing at a badge bearing no
  // such word.
  //
  // In English the mismatch reads as a synonym. In translation it is not: the
  // two are different words rather than one word in two cases, so a reader goes
  // looking for a badge with that word on it and does not find one.
  //
  // SCOPED TO `en` DELIBERATELY. The same assertion across 31 locales would
  // fail on correct translations — Finnish and the Slavic languages inflect a
  // quoted label, Welsh mutates it — and a gate whose failures are mostly
  // correct work is one people learn to silence (#1915). The defect originates
  // in the source catalogue, which is where this catches it; the locales
  // follow the source.
  const badges = ['markerNone', 'markerRevoked', 'markerCertAuthority'] as const

  it('every badge label is a non-empty string', () => {
    for (const key of badges) {
      assert.equal(typeof COPY[key], 'string', `${key} missing from en.json`)
      assert.ok(COPY[key].length > 0, `${key} is empty`)
    }
  })

  it('markerHelp quotes the two markers it is advising about', () => {
    // It is the one string that tells an operator which option to pick, so it
    // has to name them as the picker and the badge name them.
    const help = COPY.markerHelp.toLowerCase()
    for (const key of ['markerNone', 'markerRevoked'] as const) {
      assert.ok(
        help.includes(COPY[key].toLowerCase()),
        `markerHelp does not mention ${key} ("${COPY[key]}") — it reads: ${COPY.markerHelp}`,
      )
    }
  })

  // A TRUST STATE, as opposed to a description of the key itself. The first
  // version of this matcher took any `ordinary <word> key`, and `markerHelp`'s
  // "an ordinary host key" is correct English with no badge word in it — so the
  // gate reported a defect that does not exist, which is the direction a
  // matcher fails in most expensively. The subject is the vocabulary of STATES,
  // and that is small enough to name.
  const STATES = ['trusted', 'untrusted', 'approved', 'unapproved', 'revoked']
  const stateKey = new RegExp(`\\b(?:a|an|ordinary)\\s+(?:ordinary\\s+)?(${STATES.join('|')})\\s+key\\b`, 'gi')

  it('no string describes a key by a trust state no badge carries', () => {
    const words = new Set(badges.map((k) => COPY[k].toLowerCase()))
    const problems: string[] = []
    for (const [key, value] of Object.entries(COPY)) {
      for (const m of value.matchAll(stateKey)) {
        if (!words.has(m[1].toLowerCase())) {
          problems.push(`${key}: "${m[0]}" — no badge reads "${m[1]}"`)
        }
      }
    }
    assert.deepEqual(problems, [], problems.join('\n  '))
  })

  it('the matcher fires on the sentence that shipped, and not on its neighbour', () => {
    // Both directions, as literal fixtures, asserted BEFORE anything is read
    // off the catalogue. A count over the copy cannot do this: it is zero when
    // the copy is right, which is indistinguishable from a matcher that has
    // stopped matching — and the false positive is what the fixtures catch.
    const fires = 'Clear the marker on the {type} key? It becomes an ordinary trusted key.'
    const must_not = 'Leave this as Approved for an ordinary host key.'
    assert.deepEqual(
      [...fires.matchAll(stateKey)].map((m) => m[1]),
      ['trusted'],
      'the matcher no longer catches the sentence that shipped',
    )
    assert.deepEqual(
      [...must_not.matchAll(stateKey)].map((m) => m[1]),
      [],
      '"an ordinary host key" is correct English and must not be flagged',
    )
  })
})

describe('splitting a pasted entry', () => {
  it('reads a .pub file: <type> <blob> <comment>', () => {
    const out = splitKnownHostsEntry('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI root@web-1')
    assert.deepEqual(out, {
      keyType: 'ssh-ed25519',
      publicKey: 'AAAAC3NzaC1lZDI1NTE5AAAAI',
      marker: null,
    })
  })

  it('reads a known_hosts line, dropping the host', () => {
    const out = splitKnownHostsEntry('web-1.example.net ssh-rsa AAAAB3NzaC1yc2EAAAA')
    assert.deepEqual(out, {
      keyType: 'ssh-rsa',
      publicKey: 'AAAAB3NzaC1yc2EAAAA',
      marker: null,
    })
  })

  it('reads a comma-separated host list, which is the ordinary known_hosts shape', () => {
    const out = splitKnownHostsEntry('web-1,10.0.0.4 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5')
    assert.equal(out?.publicKey, 'AAAAC3NzaC1lZDI1NTE5')
  })

  it('reads an @marker and strips the @', () => {
    const out = splitKnownHostsEntry('@cert-authority *.example.net ssh-rsa AAAAB3NzaC1yc2E')
    assert.deepEqual(out, {
      keyType: 'ssh-rsa',
      publicKey: 'AAAAB3NzaC1yc2E',
      marker: 'cert-authority',
    })
    assert.equal(splitKnownHostsEntry('@revoked h ssh-rsa AAAAB3')?.marker, 'revoked')
  })

  it('leaves a bare blob alone — there is nothing to split', () => {
    // The field's ordinary state. Returning a result here would mean the note
    // fires on every keystroke of a hand-typed key.
    assert.equal(splitKnownHostsEntry('AAAAC3NzaC1lZDI1NTE5AAAAI'), null)
    assert.equal(splitKnownHostsEntry('   AAAAC3NzaC1lZDI1NTE5   '), null)
    assert.equal(splitKnownHostsEntry(''), null)
  })

  it('REFUSES a line whose algorithm field is not one of the seven', () => {
    // The load-bearing case. `ssh-dss` is a real algorithm OpenSSH knows and
    // Terrapod does not accept, and `ssh-ed25519-sk` is one letter from a
    // plausible typo. Splitting either by POSITION would hand the API a blob
    // under whichever type the picker happened to be showing — accepted
    // happily, and then it never matches the host. Refusing leaves the value
    // as typed so the server's own refusal, which names the mistake, is what
    // the operator reads.
    assert.equal(splitKnownHostsEntry('web-1 ssh-dss AAAAB3NzaC1kc3MAAAC'), null)
    assert.equal(splitKnownHostsEntry('sk-ssh-ed25519-sk@openssh.com AAAAGn'), null)
  })

  it('REFUSES an unknown @marker rather than dropping it', () => {
    // Dropping it would be the one silent change this makes: the entry would
    // be declared as an ordinary trusted key, which is the opposite of what a
    // marker someone bothered to type is likely to have meant.
    assert.equal(splitKnownHostsEntry('@banana h ssh-rsa AAAAB3'), null)
  })

  it('REFUSES a line with an algorithm and nothing after it', () => {
    assert.equal(splitKnownHostsEntry('web-1 ssh-ed25519'), null)
  })
})
