/**
 * Follows one phase's run log (plan or apply) until the server says it is
 * complete (#1591).
 *
 * The log endpoint frames its body with STX (0x02) at offset 0 and ETX (0x03)
 * at the end. ETX means "this is the authoritative, final log". The server
 * sends it only when it serves the log from object storage after the phase
 * has finished, or as a bare STX+ETX when the phase finished and no log will
 * ever exist.
 *
 * The runner uploads the stored log from its exit handler, which runs AFTER
 * the run has turned terminal. So for a short time after a phase ends, the
 * endpoint still serves the listener's live snapshot, without ETX and without
 * the tail (the `Plan: …` summary, the entrypoint's closing lines and the
 * post-plan policy output). The client has to keep asking until ETX arrives.
 * This module does that:
 *
 *  - **streaming**: an incremental poll every `pollIntervalMs`, appending the
 *    bytes past the current offset. This is the page's live view, unchanged.
 *  - **finished**: keep fetching, with a backoff (`followDelaysMs`), until a
 *    response carries ETX, then stop. The budget is bounded so a log that
 *    never finalises (a Job killed before its upload) cannot poll forever.
 *  - **idle**: no polling.
 *
 * While a finished phase is being followed, every fetch reads from offset 0
 * and REPLACES the text, rather than appending past the old offset. The live
 * snapshot is a tail window of the pod's output, capped by line count and
 * size, and the stored log is a separately assembled file. Byte offsets in one
 * are not offsets in the other, so appending at the old offset could duplicate
 * or drop text at the join. Reading from 0 costs a few extra reads of a
 * finished log, and always assembles it exactly once.
 *
 * A reset requested while a fetch is in flight is never dropped: it is queued
 * and runs as soon as the in-flight fetch finishes. A plain fetch requested
 * mid-fetch is coalesced into one follow-up fetch.
 */

export type LogPhaseState = 'idle' | 'streaming' | 'finished'

/** How a fetch's text should be applied to what is displayed. */
export type LogTextMode = 'replace' | 'append'

export interface LogFrame {
  /** Decoded log text, with the STX/ETX markers removed. */
  text: string
  /** Number of log bytes (excluding the markers) the response carried. */
  dataBytes: number
  /** True when the response ended with ETX: the log is complete. */
  ended: boolean
}

/** Default backoff after a phase ends: one minute in total. */
export const DEFAULT_FOLLOW_DELAYS_MS: readonly number[] = [1000, 2000, 4000, 8000, 15000, 30000]
export const DEFAULT_POLL_INTERVAL_MS = 2500

const STX = 0x02
const ETX = 0x03

/**
 * Strip the framing from one log response. STX is only ever sent at offset 0;
 * ETX only ever as the very last byte.
 */
export function parseLogFrame(bytes: Uint8Array, offset: number): LogFrame {
  let start = 0
  let end = bytes.length
  if (offset === 0 && end > 0 && bytes[0] === STX) start = 1
  const ended = end > start && bytes[end - 1] === ETX
  if (ended) end -= 1
  const dataBytes = Math.max(0, end - start)
  const text = dataBytes > 0 ? new TextDecoder().decode(bytes.subarray(start, end)) : ''
  return { text, dataBytes, ended }
}

export interface LogFollowerOptions {
  /**
   * Read the log from `offset`. Return the raw response bytes (an empty array
   * for an empty body), or `null` when the log is not available (a non-OK
   * response, or no log URL yet). A thrown error is treated as `null`.
   * `fresh` is true for a reset, so the caller can drop a cached log URL.
   */
  read: (offset: number, fresh: boolean) => Promise<Uint8Array | null>
  /** Display `text`, replacing or appending to what is shown. */
  onText: (text: string, mode: LogTextMode) => void
  /** Delays between follow fetches once the phase has finished. */
  followDelaysMs?: readonly number[]
  pollIntervalMs?: number
  /** Timer hooks, injectable for tests. Default to setTimeout/clearTimeout. */
  setTimer?: (fn: () => void, ms: number) => unknown
  clearTimer?: (handle: unknown) => void
}

export interface LogFollower {
  /**
   * Fetch the log. `reset` starts again from offset 0 and replaces the text.
   * The promise settles once this request, or the queued request it was
   * folded into, has run.
   */
  fetch: (reset?: boolean) => Promise<void>
  /** Tell the follower where its phase is. Starts or stops the timers. */
  setPhase: (state: LogPhaseState) => void
  /** True once a response has carried ETX (until the next reset). */
  isComplete: () => boolean
  /** Stop all timers and ignore any response still in flight. */
  dispose: () => void
}

type Pending = 'none' | 'fetch' | 'reset'

export function createLogFollower(opts: LogFollowerOptions): LogFollower {
  const followDelays = opts.followDelaysMs ?? DEFAULT_FOLLOW_DELAYS_MS
  const pollInterval = opts.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS
  const setTimer = opts.setTimer ?? ((fn: () => void, ms: number) => setTimeout(fn, ms))
  const clearTimer =
    opts.clearTimer ?? ((h: unknown) => clearTimeout(h as ReturnType<typeof setTimeout>))

  let phase: LogPhaseState = 'idle'
  let offset = 0
  let complete = false
  let disposed = false

  let inFlight: Promise<void> | null = null
  let pending: Pending = 'none'
  // Settles when the queued request has run; shared by every caller that
  // asked while a fetch was in flight.
  let pendingDone: Promise<void> | null = null
  let resolvePending: (() => void) | null = null

  let pollTimer: unknown = null
  let followTimer: unknown = null
  let followAttempt = 0

  function stopPoll() {
    if (pollTimer !== null) clearTimer(pollTimer)
    pollTimer = null
  }

  function stopFollow() {
    if (followTimer !== null) clearTimer(followTimer)
    followTimer = null
  }

  function schedulePoll() {
    if (disposed || phase !== 'streaming' || pollTimer !== null) return
    pollTimer = setTimer(() => {
      pollTimer = null
      void request(false)
      schedulePoll()
    }, pollInterval)
  }

  function scheduleFollow() {
    if (disposed || phase !== 'finished' || complete || followTimer !== null) return
    if (followAttempt >= followDelays.length) return // budget spent: give up
    const delay = followDelays[followAttempt]
    followAttempt += 1
    followTimer = setTimer(() => {
      followTimer = null
      void request(false)
    }, delay)
  }

  async function runOnce(reset: boolean): Promise<void> {
    if (reset) {
      offset = 0
      complete = false
      // A reset restarts the follow budget; the loop re-arms it afterwards.
      stopFollow()
      followAttempt = 0
    } else if (complete) {
      return // the log is final; nothing more will ever arrive
    }
    // Following a finished phase: always read the whole log from 0 (see the
    // module comment for why an old offset cannot be trusted there).
    const from = reset || phase === 'finished' ? 0 : offset
    let bytes: Uint8Array | null
    try {
      bytes = await opts.read(from, reset)
    } catch {
      bytes = null
    }
    if (disposed || bytes === null || bytes.length === 0) return
    const frame = parseLogFrame(bytes, from)
    if (frame.ended) complete = true
    if (frame.dataBytes <= 0) return
    offset = from + frame.dataBytes
    opts.onText(frame.text, from === 0 ? 'replace' : 'append')
  }

  /** Run `first`, then every request queued while it ran, one at a time. */
  async function drain(first: boolean): Promise<void> {
    let reset = first
    let settle: (() => void) | null = null
    try {
      for (;;) {
        await runOnce(reset)
        settle?.()
        settle = null
        if (disposed || pending === 'none') break
        reset = pending === 'reset'
        pending = 'none'
        settle = resolvePending
        resolvePending = null
        pendingDone = null
      }
    } finally {
      settle?.()
      inFlight = null
      if (complete) stopFollow()
      else scheduleFollow()
    }
  }

  function request(reset: boolean): Promise<void> {
    if (disposed) return Promise.resolve()
    if (inFlight) {
      // Never drop a reset; fold a plain fetch into a single follow-up.
      pending = reset || pending === 'reset' ? 'reset' : 'fetch'
      if (!pendingDone) {
        pendingDone = new Promise<void>((resolve) => {
          resolvePending = resolve
        })
      }
      return pendingDone
    }
    inFlight = drain(reset)
    return inFlight
  }

  return {
    fetch: (reset = false) => request(reset),
    setPhase(state: LogPhaseState) {
      if (disposed || state === phase) return
      phase = state
      if (state === 'streaming') {
        stopFollow()
        schedulePoll()
      } else if (state === 'finished') {
        stopPoll()
        // A fetch already in flight schedules the follow when it settles.
        if (!inFlight) scheduleFollow()
      } else {
        stopPoll()
        stopFollow()
      }
    },
    isComplete: () => complete,
    dispose() {
      disposed = true
      stopPoll()
      stopFollow()
      resolvePending?.()
      resolvePending = null
      pendingDone = null
      pending = 'none'
    },
  }
}
