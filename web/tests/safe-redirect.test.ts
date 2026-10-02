/**
 * Where the login flow is willing to send the browser after it has signed in.
 *
 * `/login?redirect=…` had no validation, so a crafted link was an open redirect
 * that fired AFTER the session token reached localStorage: the victim lands on
 * the attacker's page already signed in, having typed their password into a
 * genuine Terrapod form on the real origin. The SSO half of the flow parks the
 * same value in sessionStorage for `/auth/callback`, so it is checked on the way
 * in as well as on the way out.
 *
 * These drive the real function rather than asserting on the source text.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { safeRedirectPath } from '../src/lib/safe-redirect.ts'

describe('the paths on this origin we are willing to resume at', () => {
  for (const ok of [
    '/',
    '/workspaces',
    '/workspaces/ws-123/runs/run-456',
    '/slack/link?state=abc123',
    '/workspaces?filter=status%3Aerrored#top',
    // Percent-encoded separators stay in the PATH when the browser resolves
    // this, so it cannot reach another origin — and refusing it would break a
    // resume for any page whose query carries one.
    '/login?next=%2F%2Fexample',
  ]) {
    it(`accepts ${ok}`, () => {
      assert.equal(safeRedirectPath(ok), ok)
    })
  }
})

describe('the values that would make this an open redirect', () => {
  for (const [why, bad] of [
    ['an absolute https URL', 'https://evil.example/'],
    ['an absolute http URL', 'http://evil.example/'],
    ['a mixed-case scheme', 'HtTpS://evil.example/'],
    ['an upper-case scheme', 'HTTPS://evil.example/'],
    // The classic bypass of a leading-slash check: it starts with `/`, and it
    // is an absolute URL borrowing the current scheme.
    ['a protocol-relative URL', '//evil.example/'],
    ['a protocol-relative URL with a path', '//evil.example/login'],
    // Browsers fold `\` to `/` in an http(s) URL, so this resolves to
    // `//evil.example` — the check has to read it as the browser will.
    ['the backslash variant', '/\\evil.example/'],
    ['a double backslash', '/\\\\evil.example/'],
    ['a backslash after the slash', '\\/evil.example/'],
    // Browsers STRIP tab/LF/CR before resolving, which is what makes these
    // protocol-relative once they reach navigation.
    ['a tab hiding a protocol-relative URL', '/\t/evil.example/'],
    ['a newline hiding a protocol-relative URL', '/\n/evil.example/'],
    ['a carriage return hiding one', '/\r/evil.example/'],
    ['a scheme broken up by a newline', 'java\nscript:alert(document.domain)'],
    ['a scheme broken up by a tab', 'java\tscript:alert(1)'],
    ['leading whitespace before a scheme', ' https://evil.example/'],
    ['a NUL byte', '/workspaces\u0000'],
    // Not a redirect at all: with no `script-src` in the deployment's CSP this
    // is script execution on an origin holding the user's API token.
    ['javascript:, the XSS case', 'javascript:alert(document.domain)'],
    ['javascript: with a comment tail', 'javascript:eval(name)//'],
    ['data:', 'data:text/html,<script>alert(1)</script>'],
    ['vbscript:', 'vbscript:msgbox(1)'],
    ['a scheme nobody thought to deny', 'weird-scheme://evil.example/'],
    // Resolved relative to the CURRENT directory, so where it lands depends on
    // the page it was handed to. Never what a resume target should look like.
    ['a relative path', 'workspaces'],
    ['a bare host', 'evil.example/login'],
    ['an empty value', ''],
    ['whitespace only', '   '],
  ] as const) {
    it(`refuses ${why}: ${JSON.stringify(bad)}`, () => {
      assert.equal(safeRedirectPath(bad), null)
    })
  }

  it('refuses a missing value', () => {
    assert.equal(safeRedirectPath(null), null)
    assert.equal(safeRedirectPath(undefined), null)
  })
})

describe('the values the app itself produces round-trip', () => {
  // loginRedirectUrl() in lib/auth.ts builds `?redirect=` from
  // `location.pathname + location.search`, and searchParams.get() decodes it —
  // so the value this sees is a decoded same-origin path. If that stopped being
  // accepted, every expired-session resume would silently land on the root.
  for (const produced of [
    '/workspaces/ws-7f3a/runs/run-91b2?tab=plan',
    '/admin/policy-sets/ps-1',
    '/slack/link?state=eyJhbGciOiJIUzI1NiJ9',
  ]) {
    it(`accepts the app's own resume target ${produced}`, () => {
      assert.equal(safeRedirectPath(decodeURIComponent(encodeURIComponent(produced))), produced)
    })
  }
})
