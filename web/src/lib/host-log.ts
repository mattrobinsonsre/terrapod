/**
 * Attribute a configure log's output to the host it belongs to (#2144, #2513).
 *
 * Ansible's combined output interleaves hosts by construction, and at any real
 * fleet size that is the difference between a log you can read and one you
 * scroll past. So the runner's stdout callback writes a self-identifying format
 * and this reads it back.
 *
 * There are two, and both are parsed here because a stored log outlives the
 * runner image that wrote it.
 *
 * **v2 — one block per task per host.** The host is a FIELD of the block's
 * header line; everything indented beneath belongs to the same block, and so to
 * the same host:
 *
 *     #terrapod-host-log v2
 *     [2026-11-11 10:28:27]  ~                 play     PLAY [configure]
 *     [10:28:30 +2.9s]       web1              changed  platform : Deploy
 *                       {
 *                           "msg": "deployed"
 *                       }
 *
 * **v1 — one host prefix per line.** `web1       | ok: [web1]`, the shape
 * `docker compose` and `kubectl logs --prefix` use. Superseded because it left
 * the layout as ansible's with a column of names down the left, and because the
 * task, the result and any timing line stayed three separate items.
 *
 * Properties the viewer depends on, in both versions:
 *
 * - **The header is NOT at offset 0.** The same artifact legitimately carries
 *   untagged bytes first — the collection install writes galaxy output to it,
 *   and the orchestrator's own log lines are rolled in around it — so the
 *   header is searched for. Everything before it is context.
 * - **Output belonging to no single host is kept and marked**, never dropped: a
 *   PLAY banner and the recap are what tell a reader which play a block belongs
 *   to and how the run ended, so a filtered view still shows them.
 * - **An absent header means an untagged log**, from a runner that predates all
 *   of this. The per-host switch is then not offered and the whole log is all
 *   there is — the same data-gating rule as everywhere else, applied to a log's
 *   own content. Failing to a blank per-host panel would be the recorded
 *   "absent is not acceptable" failure.
 *
 * The literals are restated here because the plugin that writes them runs under
 * the execution environment's Python and cannot be imported from TypeScript.
 * `web/tests/host-log.test.ts` reads the plugin's own source — including one
 * complete example block — and fails if either side moves, which is what stands
 * in for a shared constant.
 */

/** Emitted once by the callback, before anything it writes. */
export const HOST_LOG_HEADER = '#terrapod-host-log v2'

/** The superseded per-line-prefix format. Still read: stored logs outlive it. */
export const HOST_LOG_HEADER_V1 = '#terrapod-host-log v1'

/** v1 only: separates the host field from ansible's own line. */
export const HOST_LOG_DELIMITER = ' | '

/** The host field's value for output belonging to no single host. */
export const HOST_LOG_HOSTLESS = '~'

/**
 * A v2 block header: a bracketed time, then the host, the result and the task.
 *
 * The time is matched tightly on purpose. A loose `^\[[^\]]*\]` would also match
 * a line of pass-through output that happens to open with a bracket, and would
 * then invent a host out of its second word. Our own producer always writes
 * `HH:MM:SS` or `YYYY-MM-DD HH:MM:SS`, so requiring that costs nothing and
 * closes the only way this grammar could misread someone else's line.
 */
const BLOCK_HEADER =
  /^\[(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}[^\]]*\]\s+(\S+)\s+(\S+)(?:\s+(.*))?$/

/**
 * A leading run of SGR (colour) sequences.
 *
 * The whole block is coloured by its result, so in v2 colour appears at the
 * START of every line rather than after a delimiter. Both the header match and
 * the "is this indented" test are therefore made against the line with any
 * leading colour removed — and only leading colour, so nothing inside the
 * line's own text is touched.
 */
const LEADING_SGR = /^(?:\u001b\[[0-9;]*m)+/

export interface HostLogLine {
  /** The host, or null for output that belongs to all of them. */
  host: string | null
  /** The log's own line, byte-for-byte. */
  text: string
}

export interface HostLog {
  /** Whether the log carries a format header at all. */
  tagged: boolean
  /** Which format it carries, for a caller that has to care. */
  version: 1 | 2 | null
  lines: HostLogLine[]
  /** Every host seen, in first-seen order. Excludes the host-less marker. */
  hosts: string[]
}

/**
 * Reverse the callback's percent-escaping of the host field.
 *
 * It escapes `%`, `|`, `~` and every whitespace character, because
 * `validate_host_name` does not refuse `|` and a host from a git inventory
 * source never passes through it at all — so the format can assume nothing
 * about a name. A name with none of those round-trips untouched, which is the
 * overwhelming case.
 */
export function unescapeHost(field: string): string {
  return field.replace(/%([0-9A-Fa-f]{2})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)))
}

function untagged(text: string): HostLog {
  return {
    tagged: false,
    version: null,
    lines: text.split('\n').map(line => ({ host: null, text: line })),
    hosts: [],
  }
}

/** The bytes before the header are real output, and host-less. */
function head(text: string, at: number, lines: HostLogLine[]): void {
  const before = text.slice(0, at)
  if (!before) return
  for (const line of before.replace(/\n$/, '').split('\n')) lines.push({ host: null, text: line })
}

/**
 * Split a log into per-line host attributions.
 *
 * Never throws and never drops a line: a line that does not parse is returned
 * as host-less, which shows it in every view rather than hiding it. Losing a
 * line of someone's log to a parse decision is the one outcome worth ruling
 * out entirely.
 */
export function parseHostLog(text: string | null | undefined): HostLog {
  if (!text) return { tagged: false, version: null, lines: [], hosts: [] }

  const v2 = text.indexOf(HOST_LOG_HEADER)
  if (v2 >= 0) return parseBlocks(text, v2)
  const v1 = text.indexOf(HOST_LOG_HEADER_V1)
  if (v1 >= 0) return parsePrefixes(text, v1)
  return untagged(text)
}

/** v2: the host is a field of a block header, inherited by its body lines. */
function parseBlocks(text: string, at: number): HostLog {
  const lines: HostLogLine[] = []
  const hosts: string[] = []
  head(text, at, lines)

  // The host of the block currently open. A body line inherits it; anything
  // that is neither a header nor a body line closes the block, because it is
  // output we did not frame and so belongs to no host in particular.
  let current: string | null = null
  const body = text.slice(at + HOST_LOG_HEADER.length).replace(/^\n/, '')
  for (const line of body.split('\n')) {
    const probe = line.replace(LEADING_SGR, '')
    const header = BLOCK_HEADER.exec(probe)
    if (header) {
      const field = header[1]
      current = field === HOST_LOG_HOSTLESS ? null : unescapeHost(field)
      if (current !== null && !hosts.includes(current)) hosts.push(current)
      lines.push({ host: current, text: line })
      continue
    }
    if (probe.startsWith(' ') || probe.startsWith('\t')) {
      lines.push({ host: current, text: line })
      continue
    }
    current = null
    lines.push({ host: null, text: line })
  }
  return { tagged: true, version: 2, lines, hosts }
}

/** v1: one `host | ` prefix per line. Kept because stored logs outlive it. */
function parsePrefixes(text: string, at: number): HostLog {
  const lines: HostLogLine[] = []
  const hosts: string[] = []
  head(text, at, lines)

  const body = text.slice(at + HOST_LOG_HEADER_V1.length).replace(/^\n/, '')
  for (const line of body.split('\n')) {
    const cut = line.indexOf(HOST_LOG_DELIMITER)
    if (cut < 0) {
      // The header line itself is dropped above; anything else unprefixed is
      // output we did not write (a tool printing straight to the terminal), so
      // it is context rather than something to discard.
      lines.push({ host: null, text: line })
      continue
    }
    const field = line.slice(0, cut).trimEnd()
    const rest = line.slice(cut + HOST_LOG_DELIMITER.length)
    if (field === HOST_LOG_HOSTLESS) {
      lines.push({ host: null, text: rest })
      continue
    }
    const host = unescapeHost(field)
    if (!hosts.includes(host)) hosts.push(host)
    lines.push({ host, text: rest })
  }
  return { tagged: true, version: 1, lines, hosts }
}

/**
 * The whole log, with no per-host filtering.
 *
 * For v1 that means ansible's own words with the prefixes removed — the two
 * views cannot then disagree about what a line said. For v2 there is nothing to
 * remove: the blocks ARE the log, the format Terrapod owns end to end, so this
 * is the text with only the header line gone.
 */
export function stripHostTags(text: string | null | undefined): string {
  const parsed = parseHostLog(text)
  if (!parsed.tagged) return text ?? ''
  return parsed.lines.map(l => l.text).join('\n')
}

/**
 * One host's output, with the shared context kept.
 *
 * Host-less lines are retained deliberately: a PLAY banner is what tells the
 * reader which play the next block belongs to, and a filtered view without them
 * is a list of results with nothing to attach them to.
 */
export function filterHostLog(parsed: HostLog, host: string): string {
  return parsed.lines
    .filter(l => l.host === null || l.host === host)
    .map(l => l.text)
    .join('\n')
}

/** A host the run targeted, as `/host-results` reports it. */
export interface TargetedHost {
  host: string
  /** The server's own status slug, or '' when it has not said yet. */
  status: string
}

/**
 * Every host the picker should offer, log-seen first.
 *
 * The log cannot answer this on its own. A host the run TARGETED and ansible
 * has not reported on appears in no block, and that is precisely the host
 * someone goes looking for — so a picker built from the log alone omits the one
 * entry the control exists to provide. `/host-results` is complete from run
 * creation (it unions the recap rows with the run's own resolved inventory), so
 * the two together are the full set.
 *
 * Order is the log's first-seen order, then the targeted hosts that have
 * produced nothing. First-seen is meaningful — it is the order they began
 * working — and appending the silent ones keeps it stable as output arrives: a
 * host moves from the tail into its first-seen position exactly once.
 *
 * A status is attached only when the server has given one. It is never derived
 * here: a dead host reports `ok` for any task needing no connection, so status
 * is a property of the tasks that touched it, not of the host (#1928).
 */
export function pickerHostList(
  logHosts: readonly string[],
  targeted: readonly TargetedHost[],
): TargetedHost[] {
  const status = new Map(targeted.map(r => [r.host, r.status]))
  const seen = new Set(logHosts)
  return [
    ...logHosts.map(host => ({ host, status: status.get(host) ?? '' })),
    ...targeted.filter(r => !seen.has(r.host)).map(r => ({ host: r.host, status: r.status })),
  ]
}
