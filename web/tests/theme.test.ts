/**
 * The theme cookie value the root layout trusts: only `dark` selects dark,
 * anything else (missing, tampered, stale) falls back to the light default.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { parseTheme } from '../src/lib/theme.ts'

describe('parseTheme', () => {
  it('selects dark only for the exact value', () => {
    assert.equal(parseTheme('dark'), 'dark')
  })

  it('falls back to light for anything else', () => {
    for (const v of ['light', 'DARK', '', 'x', undefined, null]) {
      assert.equal(parseTheme(v), 'light')
    }
  })
})
