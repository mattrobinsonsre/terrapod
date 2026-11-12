/**
 * The run-log follower (#1591).
 *
 * After a phase ends, the log endpoint serves the listener's live snapshot
 * (no end-of-log marker, no tail) until the runner's exit handler uploads the
 * stored log. These tests pin that the follower keeps asking until the marker
 * arrives, assembles the log exactly once, never drops a reset requested
 * mid-fetch, stops cleanly on a bare marker, and gives up after its budget.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import {
  createLogFollower,
  parseLogFrame,
  type LogFollower,
  type LogTextMode,
} from '../src/lib/log-follower.ts'

const enc = new TextEncoder()
const STX = '\x02'
const ETX = '\x03'
const bytes = (s: string) => enc.encode(s)

/** Let pending promise callbacks run. */
async function flush() {
  for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r))
}

/** A manual clock for the follower's timers. */
function fakeClock() {
  let now = 0
  let nextId = 1
  const timers = new Map<number, { at: number; fn: () => void }>()
  return {
    setTimer(fn: () => void, ms: number): unknown {
      const id = nextId++
      timers.set(id, { at: now + ms, fn })
      return id
    },
    clearTimer(id: unknown) {
      timers.delete(id as number)
    },
    pending: () => timers.size,
    async advance(ms: number) {
      const target = now + ms
      for (;;) {
        let next: [number, { at: number; fn: () => void }] | null = null
        for (const e of timers) if (e[1].at <= target && (!next || e[1].at < next[1].at)) next = e
        if (!next) break
        timers.delete(next[0])
        now = next[1].at
        next[1].fn()
        await flush()
      }
      now = target
    },
  }
}

interface ReadCall {
  offset: number
  fresh: boolean
}

/** A follower wired to a display buffer, reading from `respond`. */
function harness(respond: (call: ReadCall, n: number) => Promise<Uint8Array | null> | Uint8Array | null) {
  const clock = fakeClock()
  const calls: ReadCall[] = []
  let shown = ''
  const writes: LogTextMode[] = []
  const follower: LogFollower = createLogFollower({
    read: async (offset, fresh) => {
      const call = { offset, fresh }
      calls.push(call)
      return respond(call, calls.length)
    },
    onText: (text, mode) => {
      writes.push(mode)
      shown = mode === 'replace' ? text : shown + text
    },
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  })
  return { follower, clock, calls, writes, shown: () => shown }
}

function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

const SNAPSHOT = 'Initializing...\naws_instance.demo[0]: Refreshing state...\n'
const FULL = SNAPSHOT + 'Plan: 1 to add, 0 to change, 0 to destroy.\nPLAN_HAS_CHANGES=true\n'
const count = (hay: string, needle: string) => hay.split(needle).length - 1

describe('parseLogFrame', () => {
  it('strips STX at offset 0 and reports ETX', () => {
    assert.deepEqual(parseLogFrame(bytes(`${STX}abc${ETX}`), 0), { text: 'abc', dataBytes: 3, ended: true })
    assert.deepEqual(parseLogFrame(bytes(`${STX}abc`), 0), { text: 'abc', dataBytes: 3, ended: false })
  })

  it('treats a bare STX+ETX as a complete, empty log', () => {
    assert.deepEqual(parseLogFrame(bytes(`${STX}${ETX}`), 0), { text: '', dataBytes: 0, ended: true })
  })

  it('does not strip a leading 0x02 past offset 0', () => {
    assert.deepEqual(parseLogFrame(bytes(`${STX}x${ETX}`), 5), { text: `${STX}x`, dataBytes: 2, ended: true })
  })
})

describe('createLogFollower', () => {
  it('keeps following a finished phase until ETX, and assembles the log once', async () => {
    const h = harness((_c, n) => (n === 1 ? bytes(STX + SNAPSHOT) : bytes(STX + FULL + ETX)))
    h.follower.setPhase('finished')
    await h.follower.fetch(true)
    // The transition fetch lands before the upload: snapshot, no tail.
    assert.equal(h.shown(), SNAPSHOT)
    assert.equal(h.follower.isComplete(), false)

    await h.clock.advance(1000)
    assert.equal(h.shown(), FULL)
    assert.equal(count(h.shown(), 'Refreshing state'), 1)
    assert.equal(h.follower.isComplete(), true)
    assert.deepEqual(h.calls.map((c) => c.offset), [0, 0])

    // Complete: no more timers, no more reads.
    assert.equal(h.clock.pending(), 0)
    await h.clock.advance(600_000)
    assert.equal(h.calls.length, 2)
  })

  it('replaces rather than appends when the stored log is not an extension of the snapshot', async () => {
    // The live snapshot is a tail window of the pod's output; the stored log
    // is assembled separately. Offsets in one are not offsets in the other.
    const window = 'aws_instance.demo[0]: Refreshing state...\n'
    const h = harness((_c, n) => (n === 1 ? bytes(STX + window) : bytes(STX + FULL + ETX)))
    h.follower.setPhase('finished')
    await h.follower.fetch(true)
    await h.clock.advance(1000)
    assert.equal(h.shown(), FULL)
    assert.deepEqual(h.writes, ['replace', 'replace'])
  })

  it('continues following when the phase ends while a streaming fetch is in flight', async () => {
    const gate = deferred<Uint8Array | null>()
    const h = harness((_c, n) => (n === 1 ? gate.promise : bytes(STX + FULL + ETX)))
    h.follower.setPhase('streaming')
    const inflight = h.follower.fetch(true)
    h.follower.setPhase('finished')
    gate.resolve(bytes(STX + SNAPSHOT))
    await inflight
    assert.equal(h.shown(), SNAPSHOT)
    await h.clock.advance(1000)
    assert.equal(h.shown(), FULL)
    assert.equal(h.follower.isComplete(), true)
  })

  it('runs a reset requested mid-fetch as soon as the in-flight fetch completes', async () => {
    const first = deferred<Uint8Array | null>()
    const h = harness((c, n) => (n === 1 ? first.promise : bytes(STX + FULL + ETX)))
    h.follower.setPhase('finished')

    const inflight = h.follower.fetch() // e.g. a log_updated fetch
    let resetDone = false
    const reset = h.follower.fetch(true).then(() => {
      resetDone = true
    })
    await flush()
    // Queued, not dropped, and not run concurrently.
    assert.equal(h.calls.length, 1)
    assert.equal(resetDone, false)

    first.resolve(bytes(STX + SNAPSHOT))
    await inflight
    await reset
    assert.equal(resetDone, true)
    assert.equal(h.calls.length, 2)
    assert.deepEqual(h.calls[1], { offset: 0, fresh: true })
    assert.equal(h.shown(), FULL)
    assert.equal(h.follower.isComplete(), true)
  })

  it('coalesces plain fetches requested mid-fetch into one follow-up', async () => {
    const first = deferred<Uint8Array | null>()
    const h = harness((_c, n) => (n === 1 ? first.promise : bytes('')))
    h.follower.setPhase('streaming')
    const a = h.follower.fetch()
    const b = h.follower.fetch()
    const c = h.follower.fetch()
    first.resolve(bytes(STX + SNAPSHOT))
    await Promise.all([a, b, c])
    assert.equal(h.calls.length, 2)
  })

  it('stops cleanly on a phase that ends with no log at all (bare ETX)', async () => {
    const h = harness(() => bytes(STX + ETX))
    h.follower.setPhase('finished')
    await h.follower.fetch(true)
    assert.equal(h.follower.isComplete(), true)
    assert.equal(h.writes.length, 0)
    assert.equal(h.shown(), '')
    assert.equal(h.clock.pending(), 0)
    await h.clock.advance(600_000)
    assert.equal(h.calls.length, 1)
  })

  it('gives up after the follow budget when ETX never arrives', async () => {
    const h = harness(() => bytes(STX + SNAPSHOT))
    h.follower.setPhase('finished')
    await h.follower.fetch(true)
    await h.clock.advance(600_000)
    // The transition fetch plus one per default backoff step (about a minute).
    assert.equal(h.calls.length, 1 + 6)
    assert.equal(h.clock.pending(), 0)
    assert.equal(h.follower.isComplete(), false)
    assert.equal(h.shown(), SNAPSHOT)
    // Still reads when asked explicitly (the refresh button).
    await h.follower.fetch(true)
    assert.equal(h.calls.length, 8)
  })

  it('treats a failed read like a missing tail and keeps following', async () => {
    const h = harness((_c, n) => {
      if (n === 1) throw new Error('network')
      if (n === 2) return null // non-OK response
      return bytes(STX + FULL + ETX)
    })
    h.follower.setPhase('finished')
    await h.follower.fetch(true)
    await h.clock.advance(1000)
    await h.clock.advance(2000)
    assert.equal(h.shown(), FULL)
    assert.equal(h.follower.isComplete(), true)
  })

  it('a SLIDING live window does not freeze the view or duplicate text', async () => {
    // The case the incremental reader could not see, and the reason this fix
    // exists. The live snapshot is `kubectl logs --tail=500` capped at 256KB,
    // `setex`ed WHOLESALE each reconciler cycle -- so once the output passes
    // the cap the window's own byte 0 moves forward. A reader holding an
    // offset into the PREVIOUS window then slices a window that has shifted.
    //
    // This fixture is that shift, in miniature: a three-line window that drops
    // its oldest line as a new one arrives, so every window is shorter than
    // the running total the old reader would have accumulated.
    const lines = ['one\n', 'two\n', 'three\n', 'four\n', 'five\n']
    let cycle = 0
    const windowAt = (i: number) => lines.slice(Math.max(0, i - 2), i + 1).join('')
    const h = harness((c) => {
      // The server slices whatever window it currently holds at the offset it
      // is given -- which is the whole defect, so the fixture models it.
      const w = windowAt(Math.min(cycle, lines.length - 1))
      cycle += 1
      return bytes((c.offset === 0 ? STX : '') + enc.encode(w).slice(c.offset).length
        ? (c.offset === 0 ? STX : '') + new TextDecoder().decode(enc.encode(w).slice(c.offset))
        : '')
    })
    h.follower.setPhase('streaming')
    await h.follower.fetch(true)
    for (let i = 0; i < 4; i++) await h.clock.advance(2500)

    // Every read from 0, so what is shown is exactly the newest window --
    // never a stale join, and never empty.
    assert.equal(h.shown(), windowAt(lines.length - 1))
    assert.equal(h.shown(), 'three\nfour\nfive\n')
    assert.ok(
      h.calls.every((c) => c.offset === 0),
      `a non-zero offset was asked for: ${JSON.stringify(h.calls.map((c) => c.offset))}`,
    )
    // No line appears twice, which an append past a shifted offset would do.
    for (const l of lines) assert.ok(count(h.shown(), l) <= 1, `duplicated ${JSON.stringify(l)}`)
    // And the view is not frozen on the first window.
    assert.notEqual(h.shown(), windowAt(0))
  })

  it('apply log: streams without duplication, then follows to ETX', async () => {
    const live = ['Applying...\n', 'aws_instance.demo[0]: Creating...\n']
    const final = live.join('') + 'Apply complete! Resources: 1 added.\n'
    let n = 0
    let stored = false
    const h = harness(() => {
      // Every read is from 0 against the window as it currently stands, so the
      // fixture does not branch on an offset -- there is only ever one. It
      // branches on whether the runner's exit trap has landed the stored log,
      // which is the real discriminator and the one the follower cannot see.
      n += 1
      if (!stored) return bytes(STX + (n === 1 ? live[0] : live.join('')))
      return bytes(STX + final + ETX)
    })
    h.follower.setPhase('streaming')
    await h.follower.fetch(true)
    await h.clock.advance(2500)
    await h.clock.advance(2500)
    assert.equal(h.shown(), live.join(''))
    assert.equal(count(h.shown(), 'Creating'), 1)
    // Never `append`: the live snapshot is a sliding window, so a non-zero
    // offset is never asked for and the mode is always a whole replace.
    assert.ok(h.writes.length >= 2, `only ${h.writes.length} writes`)
    assert.ok(
      h.writes.every((m) => m === 'replace'),
      `an append was produced: ${JSON.stringify(h.writes)}`,
    )
    assert.ok(
      h.calls.every((c) => c.offset === 0),
      `a non-zero offset was asked for: ${JSON.stringify(h.calls.map((c) => c.offset))}`,
    )
    // The phase ends, but the runner uploads the stored log from its EXIT
    // trap -- so the first finished read is still the live snapshot.
    h.follower.setPhase('finished')
    await h.follower.fetch(true)
    assert.equal(h.shown(), live.join(''))
    stored = true
    await h.clock.advance(1000)
    assert.equal(h.shown(), final)
    assert.equal(count(h.shown(), 'Creating'), 1)
    assert.equal(h.follower.isComplete(), true)
    assert.equal(h.clock.pending(), 0)
  })

  it('idle stops polling and dispose stops everything', async () => {
    const h = harness(() => bytes(''))
    h.follower.setPhase('streaming')
    await h.clock.advance(2500)
    assert.equal(h.calls.length, 1)
    h.follower.setPhase('idle')
    await h.clock.advance(60_000)
    assert.equal(h.calls.length, 1)

    h.follower.setPhase('finished')
    h.follower.dispose()
    assert.equal(h.clock.pending(), 0)
    await h.follower.fetch(true)
    assert.equal(h.calls.length, 1)
  })
})
