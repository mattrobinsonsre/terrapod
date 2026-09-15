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

  it('apply log: streams incrementally without duplication, then follows to ETX', async () => {
    const live = ['Applying...\n', 'aws_instance.demo[0]: Creating...\n']
    const final = live.join('') + 'Apply complete! Resources: 1 added.\n'
    const h = harness((c) => {
      // Streaming reads are incremental against the growing live log.
      if (c.offset === 0 && h.calls.length <= 1) return bytes(STX + live[0])
      if (c.offset === enc.encode(live[0]).length) return bytes(live[1])
      if (c.offset > 0) return bytes('')
      // Once finished: the first full read is still the snapshot, then the
      // stored log with ETX.
      return h.calls.filter((x) => x.offset === 0).length <= 2
        ? bytes(STX + live.join(''))
        : bytes(STX + final + ETX)
    })
    h.follower.setPhase('streaming')
    await h.follower.fetch(true)
    await h.clock.advance(2500)
    await h.clock.advance(2500)
    assert.equal(h.shown(), live.join(''))
    assert.equal(count(h.shown(), 'Creating'), 1)
    assert.deepEqual(h.writes, ['replace', 'append'])

    h.follower.setPhase('finished')
    await h.follower.fetch(true)
    assert.equal(h.shown(), live.join(''))
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
