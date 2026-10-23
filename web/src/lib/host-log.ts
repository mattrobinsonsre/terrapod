/**
 * Parse a configure log's per-host line tags (#2144).
 *
 * Ansible's combined output interleaves hosts by construction, and at any real
 * fleet size that is the difference between a log you can read and one you
 * scroll past. So the runner's stdout callback prefixes every line it emits
 * with the host it belongs to, and this reads that back.
 *
 * The format, which is the shape `docker compose` and `kubectl logs --prefix`
 * already use:
 *
 *     #terrapod-host-log v1
 *     web1       | ok: [web1] => {
 *     web1       |     "msg": "hello from web1"
 *     web1       | }
 *     ~          | TASK [say hello] ***************************************
 *
 * Three properties the viewer depends on:
 *
 * - **The header is NOT at offset 0.** The same artifact legitimately carries
 *   untagged bytes first — the collection install writes galaxy output to it,
 *   and the orchestrator's own log lines are rolled in around it — so the
 *   header is searched for. Everything before it is context.
 * - **`~` marks a line belonging to no single host** — task banners, the PLAY
 *   RECAP. A filtered view still shows them, because they are what tells a
 *   reader WHICH task a line belongs to. The marker is explicit rather than
 *   "no prefix" because the absence of a prefix is also what an older runner's
 *   log looks like, and those two must not be confusable.
 * - **An absent header means an untagged log**, from a runner that predates
 *   this. The per-host switch is then not offered and the traditional view is
 *   all there is — the same data-gating rule as everywhere else, applied to a
 *   log's own content. Failing to a blank per-host panel would be the recorded
 *   "absent is not acceptable" failure.
 *
 * The literals are restated here because the plugin that writes them runs under
 * the execution environment's Python and cannot be imported from TypeScript.
 * `web/tests/host-log.test.ts` reads the plugin's own source and fails if
 * either side moves, which is what stands in for a shared constant.
 */

/** Emitted once by the callback, before anything it tags. */
export const HOST_LOG_HEADER = '#terrapod-host-log v1'

/** Separates the host field from ansible's own line. */
export const HOST_LOG_DELIMITER = ' | '

/** The host field's value for a line belonging to no single host. */
export const HOST_LOG_HOSTLESS = '~'

export interface HostLogLine {
  /** The host, or null for a line that belongs to all of them. */
  host: string | null
  /** Ansible's own line, byte-for-byte, with no tag. */
  text: string
}

export interface HostLog {
  /** Whether the log carries the format header at all. */
  tagged: boolean
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

/**
 * Split a log into per-line host attributions.
 *
 * Never throws and never drops a line: a line that does not parse is returned
 * as host-less, which shows it in every view rather than hiding it. Losing a
 * line of someone's log to a parse decision is the one outcome worth ruling
 * out entirely.
 */
export function parseHostLog(text: string | null | undefined): HostLog {
  if (!text) return { tagged: false, lines: [], hosts: [] }

  const at = text.indexOf(HOST_LOG_HEADER)
  if (at < 0) {
    return {
      tagged: false,
      lines: text.split('\n').map(line => ({ host: null, text: line })),
      hosts: [],
    }
  }

  const lines: HostLogLine[] = []
  const hosts: string[] = []
  // Lines before the header are real output and must be kept — see the module
  // comment. They are host-less, so they appear in every view.
  const head = text.slice(0, at)
  if (head) {
    for (const line of head.replace(/\n$/, '').split('\n')) lines.push({ host: null, text: line })
  }

  const body = text.slice(at + HOST_LOG_HEADER.length).replace(/^\n/, '')
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
  return { tagged: true, lines, hosts }
}

/**
 * The traditional view: ansible's own words and nothing else.
 *
 * Exactly `parseHostLog` with the tags dropped, so the two views cannot
 * disagree about what a line said.
 */
export function stripHostTags(text: string | null | undefined): string {
  const parsed = parseHostLog(text)
  if (!parsed.tagged) return text ?? ''
  return parsed.lines.map(l => l.text).join('\n')
}

/**
 * One host's output, with the shared context kept.
 *
 * Host-less lines are retained deliberately: a task banner is what tells the
 * reader which task the next line belongs to, and a filtered view without them
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
 * has not reported on appears in no tagged line, and that is precisely the host
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
