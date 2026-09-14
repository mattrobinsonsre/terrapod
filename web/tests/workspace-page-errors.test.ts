// The workspace page never throws away the API's reason for a failure (#1586).
//
// Every failed request on that page used to throw a fixed "Failed to …"
// message, so a 422 that explained itself — "Cannot change vcs-workflow while
// 7 PR run(s) are in flight. Cancel or discard them first." — showed only
// "Failed to update workspace". Two saves went the other way and showed the raw
// response body, a JSON blob. The page now passes the response through
// parseApiError, with the translated message as the fallback. These guards fail
// if either shape comes back.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const page = readFileSync(path.join(here, '..', 'src', 'app', 'workspaces', '[id]', 'page.tsx'), 'utf8')

function linesMatching(re: RegExp): string[] {
  return page
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
