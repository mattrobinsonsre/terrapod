// No form page throws away the API's reason for a failure (#1586, #2413 F2).
//
// Every failed request on that page used to throw a fixed "Failed to …"
// message, so a 422 that explained itself — "Cannot change vcs-workflow while
// 7 PR run(s) are in flight. Cancel or discard them first." — showed only
// "Failed to update workspace". Two saves went the other way and showed the raw
// response body, a JSON blob. The page now passes the response through
// parseApiError, with the translated message as the fallback. These guards fail
// if either shape comes back.
//
// Extended to the two ADMIN forms by #2413 F2, because the same defect was
// still live on one of them: the autodiscovery rule form interpolated
// `await res.text()` into its message, so a refusal arrived wrapped in a JSON
// envelope. That form is where it matters most -- collection parsing
// deliberately validates nothing in the browser, on the grounds that the server
// refuses a malformed entry BY NAME, which makes the server's message the whole
// of the feedback.
//
// `body.detail || fallback` is tolerated: it reads the detail rather than the
// raw body, and the API's dual-key error envelope always carries it. What is
// forbidden is putting the undecoded body in front of a person.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (...parts: string[]) =>
  readFileSync(path.join(here, '..', 'src', 'app', ...parts), 'utf8')

/**
 * The form pages that save and must surface the server's reason.
 *
 * Hand-listed rather than globbed, deliberately: a glob over `app/` would pull
 * in every read-only page and make the floor below meaningless. Adding a form
 * page here is the cost of having one.
 */
const PAGES: Record<string, string> = {
  'workspaces/[id]/page.tsx': read('workspaces', '[id]', 'page.tsx'),
  'admin/autodiscovery/page.tsx': read('admin', 'autodiscovery', 'page.tsx'),
  'admin/bulk-update/page.tsx': read('admin', 'bulk-update', 'page.tsx'),
}

const page = PAGES['workspaces/[id]/page.tsx']

function linesMatching(re: RegExp, src: string = page): string[] {
  return src
    .split('\n')
    .map((line, i) => `${i + 1}: ${line.trim()}`)
    .filter((line) => re.test(line))
}

test('no failed request on the workspace page discards the API error', () => {
  assert.deepEqual(linesMatching(/if \(!res\.ok\) throw new Error\(t\(/), [])
})

test('no failed request shows the raw response body', () => {
  assert.deepEqual(linesMatching(/throw new Error\(body \|\|/), [])
})

test('the page reads failures through parseApiError', () => {
  assert.match(page, /import \{[^}]*\bparseApiError\b[^}]*\} from '@\/lib\/api'/)
})


// ── #2413 F2: the same guarantee on every form page ───────────────────────

test('no form page puts an undecoded response body in front of a person', () => {
  // `await res.text()` reaching a message is the defect: the body arrives as a
  // JSON:API envelope, so the reader gets `{"errors":[{"detail":"..."}]}`
  // rather than the sentence inside it.
  for (const [name, src] of Object.entries(PAGES)) {
    const hits = linesMatching(/await res\.text\(\)/, src)
    assert.deepEqual(hits, [], `${name} reads the raw body: ${hits.join(' | ')}`)
  }
})

test('every form page reads failures through parseApiError', () => {
  for (const [name, src] of Object.entries(PAGES)) {
    assert.match(
      src,
      /import \{[^}]*\bparseApiError\b[^}]*\} from '@\/lib\/api'/,
      `${name} does not import parseApiError`,
    )
  }
})

test('the page list is not empty — a floor, so a rename cannot pass on nothing', () => {
  assert.ok(Object.keys(PAGES).length >= 3, 'expected at least three form pages')
  for (const [name, src] of Object.entries(PAGES)) {
    assert.ok(src.length > 1000, `${name} looks empty — the path is probably wrong`)
  }
})
