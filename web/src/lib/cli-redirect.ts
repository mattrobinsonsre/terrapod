/**
 * Where the CLI login hand-off page is willing to send an authorization code.
 *
 * `/auth/cli-complete` takes `redirect_uri` from its own query string and
 * navigates to it. Without this check that is an open redirect — and because
 * the deployment's CSP sets no `script-src`, a `javascript:` URI there is XSS
 * against an origin that holds the user's API token in localStorage.
 *
 * The server already refuses to STORE an auth state with a bad `redirect_uri`
 * (`validate_cli_redirect_uri` in services/terrapod/auth/redirect_uri.py), but
 * that check never sees this page's query string: a hand-crafted link reaches
 * the browser without passing through it. So the same allow-list has to exist
 * on this side too.
 *
 * Deliberately an allow-list rather than a deny-list of known-bad schemes.
 * `javascript:`, `data:`, `vbscript:` is a list nobody finishes.
 *
 * Kept in step with the server's rules by hand; both are small and both fail
 * closed.
 */

/** Loopback ports `terraform login` / `tofu login` advertise. Mirrors LOGIN_PORTS. */
export const LOGIN_PORT_MIN = 10000
export const LOGIN_PORT_MAX = 10010

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1'])

export function isDeliverableRedirect(raw: string): boolean {
  if (!raw) return false

  let url: URL
  try {
    url = new URL(raw)
  } catch {
    // Not absolute, or not parseable. A bare path lands here; `javascript:`
    // parses but is refused below on scheme and host.
    return false
  }

  if (url.protocol !== 'http:') return false

  // `hostname` lower-cases, and is empty for a scheme with no authority —
  // which `javascript:alert(1)` is. It does NOT strip the brackets from an
  // IPv6 literal, unlike Python's `urlsplit().hostname`, so `http://[::1]:…`
  // arrives here as `[::1]`. Strip them rather than carrying a bracketed
  // entry, so this set reads the same as the server's `_LOOPBACK_HOSTS`.
  const host = url.hostname.startsWith('[') && url.hostname.endsWith(']')
    ? url.hostname.slice(1, -1)
    : url.hostname
  if (!LOOPBACK_HOSTS.has(host)) return false

  // Credentials in the authority are the classic way to smuggle a foreign host
  // past a naive check: in `http://localhost:10000@evil.tld/` the host is
  // evil.tld and `localhost:10000` is merely userinfo. URL parsing resolves
  // that correctly on its own, so this is belt and braces.
  if (url.username || url.password) return false

  // A fragment is never sent to a server, so appending the code after one
  // strands it in the browser.
  if (url.hash) return false

  if (!url.port) return false
  const port = Number(url.port)
  if (!Number.isInteger(port)) return false

  return port >= LOGIN_PORT_MIN && port <= LOGIN_PORT_MAX
}
