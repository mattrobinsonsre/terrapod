/**
 * Where the CLI login hand-off is willing to send an authorization code.
 *
 * `/auth/cli-complete` reads `redirect_uri` from its own query string and
 * navigates to it. The server's `validate_cli_redirect_uri` never sees that
 * value — it guards the STORED auth state, and a hand-crafted link reaches the
 * browser without passing through it — so the page needs the same allow-list.
 *
 * This matters more since the page began navigating automatically on a
 * rejected delivery: an attacker can force that rejection (any non-loopback
 * `http:` origin is mixed content from an HTTPS page, and an unsupported
 * scheme is a network error), which turned a click-gated sink into a
 * zero-click one. With no `script-src` in the deployment's CSP, a
 * `javascript:` URI there is XSS against an origin holding the user's API
 * token in localStorage.
 *
 * These drive the real strings rather than asserting on the source text.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { isDeliverableRedirect, LOGIN_PORT_MIN, LOGIN_PORT_MAX } from '../src/lib/cli-redirect.ts'

describe('the CLI listener addresses we accept', () => {
  for (const ok of [
    `http://127.0.0.1:${LOGIN_PORT_MIN}/login`,
    `http://127.0.0.1:${LOGIN_PORT_MAX}/login`,
    'http://localhost:10005/login',
    'http://[::1]:10000/login',
    'http://127.0.0.1:10000',
    'http://127.0.0.1:10000/login?already=set',
  ]) {
    it(`accepts ${ok}`, () => {
      assert.equal(isDeliverableRedirect(ok), true)
    })
  }
})

describe('the addresses that would make this an open redirect or XSS', () => {
  for (const [why, bad] of [
    ['a foreign origin', 'http://evil.example/login'],
    // The sharpest open-redirect case: only the host allow-list catches this
    // one, because the port is inside the CLI's advertised range.
    ['a foreign origin on an in-range port', 'http://evil.example:10000/login'],
    ['a foreign origin over https', 'https://evil.example/login'],
    ['javascript:, the XSS case', 'javascript:alert(document.domain)//'],
    ['javascript: with a comment tail', 'javascript:eval(name)//?x=1'],
    ['data:', 'data:text/html,<script>alert(1)</script>'],
    ['vbscript:', 'vbscript:msgbox(1)'],
    ['a host smuggled as userinfo', 'http://localhost:10000@evil.example/login'],
    ['userinfo with a password', 'http://user:pw@127.0.0.1:10000/login'],
    ['a loopback-looking subdomain', 'http://127.0.0.1.evil.example:10000/login'],
    ['a loopback-prefixed host', 'http://localhost.evil.example:10000/login'],
    ['a port below the range', 'http://127.0.0.1:9999/login'],
    ['a port above the range', 'http://127.0.0.1:10011/login'],
    ['no port at all', 'http://127.0.0.1/login'],
    ['a fragment, which never reaches a server', 'http://127.0.0.1:10000/login#frag'],
    ['a bare path', '/login'],
    ['a protocol-relative URL', '//evil.example/login'],
    ['an empty value', ''],
    ['whitespace', '   '],
  ] as const) {
    it(`refuses ${why}: ${bad}`, () => {
      assert.equal(isDeliverableRedirect(bad), false)
    })
  }
})

describe('the allow-list is an allow-list', () => {
  it('refuses a scheme nobody thought to deny', () => {
    // The point of allow-listing: this needs no entry in any deny-list.
    assert.equal(isDeliverableRedirect('weird-scheme://127.0.0.1:10000/login'), false)
  })

  it('refuses https even to a loopback host', () => {
    // The CLI listener is plain HTTP. Accepting https here would widen the
    // surface for nothing, and it is not a thing tofu login produces.
    assert.equal(isDeliverableRedirect('https://127.0.0.1:10000/login'), false)
  })
})
