/**
 * The theme cookie value the root layout trusts: only the exact values `dark`
 * and `light` select a theme, anything else (missing, tampered, stale) falls
 * back to the deployment default.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { DEFAULT_THEME, parseTheme } from '../src/lib/theme.ts'

describe('parseTheme', () => {
  it('selects each theme only for its exact value', () => {
    assert.equal(parseTheme('dark'), 'dark')
    assert.equal(parseTheme('light'), 'light')
  })

  it('falls back to the default for anything else', () => {
    for (const v of ['DARK', 'Light', '', 'x', undefined, null]) {
      assert.equal(parseTheme(v), DEFAULT_THEME)
    }
  })

  it('ships light as the default', () => {
    assert.equal(DEFAULT_THEME, 'light')
  })
})
