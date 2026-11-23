// Approved SSH host keys — the client-side half of #2592.
//
// Terrapod owns the model and hands off to OpenSSH: a `known_hosts` file is the
// interface, and these are the two closed sets that file's second and optional
// first fields draw from. Both are restated here because the browser has no
// endpoint to ask, and `tests/ssh-host-key-sets.test.ts` derives the server's
// own frozensets out of `db/models.py` and asserts set equality — so a seventh
// algorithm, or an eighth, fails there rather than leaving a picker that cannot
// offer a type the API accepts.

/**
 * The algorithms a host key may be declared under, ordered as the picker
 * offers them rather than alphabetically.
 *
 * Ed25519 first because it is what a current OpenSSH prefers, and `ssh-rsa`
 * last because it is the SHA-1 signature algorithm — still a valid host key
 * and still something an older fleet presents, so it is offered, but it is not
 * the one to reach for first. The ORDER is this module's; the SET is the
 * server's, and the gate holds them equal.
 */
export const SSH_HOST_KEY_TYPES = [
  'ssh-ed25519',
  'ecdsa-sha2-nistp256',
  'ecdsa-sha2-nistp384',
  'ecdsa-sha2-nistp521',
  'rsa-sha2-512',
  'rsa-sha2-256',
  'ssh-rsa',
] as const

export type SshHostKeyType = (typeof SSH_HOST_KEY_TYPES)[number]

/**
 * The optional first field of a `known_hosts` line, without its `@`.
 *
 * Per LINE rather than per algorithm, which is why it is one value on a row
 * and not a set: OpenSSH keys its revoked set on the marshalled key, so a
 * rotation declares the outgoing key `revoked` and keeps it — a replay of it
 * is then refused rather than silently trusted as an unknown host.
 */
export const SSH_HOST_KEY_MARKERS = ['cert-authority', 'revoked'] as const

export type SshHostKeyMarker = (typeof SSH_HOST_KEY_MARKERS)[number]

/**
 * The placeholder in the public-key field: the opening bytes every ed25519 host
 * key shares, which is what an operator sees at the start of the thing they are
 * about to paste.
 *
 * NOT in the message catalogue, and that is the AGENTS.md rule rather than a
 * shortcut past it: a base64 blob is an identifier, not UX copy, so there is
 * nothing to translate and nothing a locale could usefully say about it. Left
 * in the catalogue it would need an `--allow-identical` exemption in all
 * thirty-one locales, each of which is a claim that a translator considered it
 * — and `en-x-leet` would substitute the capital A's, corrupting the sample
 * into something that is not base64 at all.
 */
export const SAMPLE_HOST_KEY_BLOB = 'AAAAC3NzaC1lZDI1NTE5AAAAI...'

/**
 * How a configure verifies the host keys of the hosts it targets (#2592).
 *
 * The third closed set the server restates here, and the one that is NOT a
 * `known_hosts` field: it is OpenSSH's own `StrictHostKeyChecking`, which is
 * what decides whether the file above is consulted as an allow-list or
 * ignored. It replaced a boolean named after ansible's `HOST_KEY_CHECKING`
 * flag, because a boolean cannot say `accept-new` -- trust a host on first
 * contact, refuse it once its key changes -- and ansible cannot express that
 * state at all.
 *
 * `accept-new` is the one worth understanding, because it is what makes a
 * growing fleet workable: the rendered file is an ALLOW-LIST and `ssh_args` is
 * process-wide, so under `strict` declaring one approved key makes every
 * address in that inventory nobody has pinned unreachable. Under `accept-new`
 * the pinned hosts are still verified strictly against their approved keys and
 * the unpinned ones stay reachable.
 *
 * Ordered by what it permits, dangerous end last, as
 * `DYNAMIC_INVENTORY_MODES` is. The ORDER is this module's; the SET is the
 * server's `SSH_HOST_KEY_POLICIES`, and `tests/ssh-host-key-sets.test.ts`
 * derives it out of `db/models.py` and holds the two equal -- so a fourth
 * state fails there rather than leaving a picker that cannot offer it.
 *
 * **These are WIRE VALUES and are never translated.** The labels beside them
 * are prose and are; `strict` is what the API stores and what a
 * `terrapod_workspace` resource declares, so an `en-x-leet` substitution of it
 * would send `57r1c7` to a server that refuses it.
 */
export const SSH_HOST_KEY_POLICIES = ['strict', 'accept-new', 'off'] as const

export type SshHostKeyPolicy = (typeof SSH_HOST_KEY_POLICIES)[number]

/**
 * The column default, and the behaviour that already existed rather than a new
 * restriction: ansible's own `HOST_KEY_CHECKING` defaults to on, so first
 * contact with an unseen host already failed closed.
 *
 * Both forms seed it rather than letting an absent value read as `off`, which
 * would misreport a workspace nobody has touched as accepting any host key.
 */
export const DEFAULT_SSH_HOST_KEY_POLICY: SshHostKeyPolicy = 'strict'

/**
 * The policy a stored value reads as.
 *
 * Total by construction, which is the property that matters: a value this
 * could not name would render the select blank and lose the operator's setting
 * on the next save. An older API that predates the attribute sends nothing,
 * and a 1.x one could send the boolean this replaced -- `true` was `strict`
 * and `false` was `off`, which is the conversion the migration applied -- so
 * both are read rather than falling through to the default, because reading
 * `false` as `strict` would tell an operator their fleet is verifying when the
 * server says it is not.
 */
export function asSshHostKeyPolicy(raw: unknown): SshHostKeyPolicy {
  if (typeof raw === 'string' && (SSH_HOST_KEY_POLICIES as readonly string[]).includes(raw)) {
    return raw as SshHostKeyPolicy
  }
  if (raw === false) return 'off'
  return DEFAULT_SSH_HOST_KEY_POLICY
}

export interface SplitKnownHostsEntry {
  keyType: SshHostKeyType
  publicKey: string
  /** `null` when the line carried no `@marker`. */
  marker: SshHostKeyMarker | null
}

const TYPES: readonly string[] = SSH_HOST_KEY_TYPES
const MARKERS: readonly string[] = SSH_HOST_KEY_MARKERS

/**
 * Split a pasted `known_hosts` line or `.pub` file into the fields the API takes.
 *
 * **Why this exists, given the API refuses such a paste outright.** The
 * commonest thing an operator has to hand is a whole line — either
 * `/etc/ssh/ssh_host_ed25519_key.pub` (`<type> <blob> <comment>`) or a
 * `known_hosts` entry (`[@marker] <host[,host]> <type> <blob>`) — and the
 * server's refusal is a good one: stored as the blob, a whole line renders
 * into a `known_hosts` entry with the host and algorithm doubled, which no
 * client matches, and the symptom is an unknown host rather than a parse
 * error. So the API is right to refuse it.
 *
 * It is right about the stored VALUE. This is operator INPUT, and the two are
 * not the same thing: splitting here fills the visible fields, which the
 * operator reads and submits, so nothing is transformed behind anyone's back —
 * where not splitting guarantees a 422 on the one paste they were always going
 * to make. The whitespace test is the boundary: a value with whitespace in it
 * is one the API will refuse whatever we do, so there is no correct input this
 * can take away.
 *
 * **It refuses rather than guesses.** Returning `null` leaves the value exactly
 * as typed, so the API's own refusal — which names the mistake and is better
 * written than anything this could say — is what the operator sees. In
 * particular a line whose algorithm field is not one of the seven is NOT
 * split: that is either a typo or an algorithm Terrapod does not accept, and
 * both want the server's message rather than a silent reinterpretation.
 *
 * The host field and any trailing comment are dropped, deliberately: the host
 * comes from the inventory row this key hangs off, and a comment is not part of
 * the key. That loss is visible, because the fields it fills are on screen.
 */
export function splitKnownHostsEntry(raw: string): SplitKnownHostsEntry | null {
  const tokens = raw.trim().split(/\s+/).filter(Boolean)
  // One token is a bare blob, already the shape the API wants — there is
  // nothing to split and saying so is not this function's job.
  if (tokens.length < 2) return null

  // The ALGORITHM is the anchor, not the position: a `.pub` file starts with it
  // and a `known_hosts` line has a host (and possibly a marker) in front, so
  // counting fields from the left gets one of the two shapes wrong.
  const at = tokens.findIndex((tok) => TYPES.includes(tok))
  if (at < 0) return null

  const publicKey = tokens[at + 1]
  if (!publicKey) return null

  // A marker is `@cert-authority` / `@revoked` and can only precede the host,
  // so it is looked for only in what came BEFORE the algorithm. An unknown
  // `@something` is left out rather than passed through: the API refuses an
  // unknown marker, and dropping it would be the one silent change this makes.
  const before = tokens.slice(0, at)
  const flagged = before.find((tok) => tok.startsWith('@'))
  const bare = flagged?.slice(1)
  if (flagged && !MARKERS.includes(bare ?? '')) return null

  return {
    keyType: tokens[at] as SshHostKeyType,
    publicKey,
    marker: (bare as SshHostKeyMarker | undefined) ?? null,
  }
}
