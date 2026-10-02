/**
 * Where the login flow is willing to send the browser after it has signed in.
 *
 * `/login?redirect=…` exists so an expired session can resume where it left
 * off (`loginRedirectUrl()` in `lib/auth.ts` builds it, and the Slack link page
 * hands one over by hand). Nothing validated it, so a crafted
 * `/login?redirect=https://evil.example` was an open redirect that fired AFTER
 * the session token had been written to localStorage — the victim arrives at an
 * attacker's page already logged in, having seen a genuine Terrapod login form
 * on the real origin.
 *
 * Two sinks share the value: the local-login path assigns it to
 * `window.location.href`, and the SSO path parks it in `sessionStorage` for
 * `/auth/callback` to pick up once the IdP returns. Both go through here, and
 * the SSO path is checked on the way IN as well as on the way out, so a
 * sessionStorage entry written by some other means cannot poison the callback.
 *
 * Deliberately narrow: a path on this origin, and nothing else. A same-origin
 * allow-list of hostnames would be a bigger surface for no gain — nothing in
 * the product needs to send the user to another host after login.
 *
 * Returns the value unchanged when it is safe, or `null`, whereupon the caller
 * falls back to the app root.
 */
export function safeRedirectPath(raw: string | null | undefined): string | null {
  if (!raw) return null

  // Browsers REMOVE tab, LF and CR from a URL before resolving it, so a check
  // that reads the raw string is not reading what the browser will navigate to:
  // `/\t/evil.example` begins `/` then a tab, passing a naive protocol-relative
  // test, and resolves to `//evil.example`. The same stripping is what turns
  // `java\nscript:` back into `javascript:`. Refuse the whole class rather than
  // trying to normalise it the way each browser does.
  if (/[\u0000-\u001f\u007f]/.test(raw)) return null

  // Any scheme at all, not a deny-list of the known-dangerous ones —
  // `javascript:`, `data:`, `vbscript:` is a list nobody finishes. The pattern
  // is the RFC 3986 scheme production, so it catches mixed case too.
  //
  // REDUNDANT TODAY, deliberately: a scheme cannot appear before the leading
  // `/` that the path-absolute rule below insists on, so deleting this line
  // changes no behaviour and no test can pin it. It stays because the rule it
  // backs up is the one most likely to be relaxed — the day someone accepts a
  // same-origin ABSOLUTE url here, this is what keeps `javascript:` out.
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(raw)) return null

  // URL parsing folds `\` to `/` for http(s), so `/\evil.example` is
  // `//evil.example` — protocol-relative, and off this origin. A legitimate
  // target is percent-encoded (`%5C`), so a raw backslash never appears in one.
  if (raw.includes('\\')) return null

  // Path-absolute only. This is what makes the result same-origin: a bare
  // `evil.example/x` would resolve relative to the current directory, but a
  // leading `/` can only ever address this origin.
  if (!raw.startsWith('/')) return null

  // `//evil.example` is protocol-relative — an absolute URL that borrows the
  // current scheme. It is the one hostile shape that does start with `/`.
  if (raw.startsWith('//')) return null

  return raw
}
