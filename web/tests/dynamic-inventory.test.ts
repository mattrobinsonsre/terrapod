/**
 * The dynamic-inventory mode (#2413 F19).
 *
 * The property is a ROUND TRIP across all four states, and it is asserted as a
 * cross product rather than per case: the finding's own enumeration was one row
 * short, and a per-case test written from that enumeration would have agreed
 * with it.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import {
  DYNAMIC_INVENTORY_MODES,
  flagsFromMode,
  modeFromFlags,
  type DynamicInventoryMode,
} from '../src/lib/dynamic-inventory.ts'

describe('the dynamic-inventory mode', () => {
  it('names all FOUR flag combinations, not three', () => {
    // `scripts` without `plugins` is a real configuration -- measured against
    // the runner's `_enabled_plugins`, which appends the two independently and
    // yields `script,yaml,ini`. A tri-state would have no name for it.
    const seen = new Set<DynamicInventoryMode>()
    for (const plugins of [false, true]) {
      for (const scripts of [false, true]) {
        seen.add(modeFromFlags(plugins, scripts))
      }
    }
    assert.equal(seen.size, 4, 'two flag combinations share a mode, so one is unreachable')
    assert.deepEqual([...seen].sort(), [...DYNAMIC_INVENTORY_MODES].sort())
  })

  it('round-trips every mode through the flags and back', () => {
    // The property that stops a value being lost on save: a mode must survive
    // being written to two booleans and read back.
    for (const mode of DYNAMIC_INVENTORY_MODES) {
      const { plugins, scripts } = flagsFromMode(mode)
      assert.equal(modeFromFlags(plugins, scripts), mode, `${mode} did not round-trip`)
    }
  })

  it('round-trips every flag PAIR through the mode and back', () => {
    // The other direction, which is the one that matters for a workspace whose
    // columns were set by the API or by a rule rather than by this control.
    for (const plugins of [false, true]) {
      for (const scripts of [false, true]) {
        const back = flagsFromMode(modeFromFlags(plugins, scripts))
        assert.deepEqual(back, { plugins, scripts }, `(${plugins}, ${scripts}) did not round-trip`)
      }
    }
  })

  it('is ordered by what it permits, so the default is first and both is last', () => {
    // The order is the control's option order, so it is load-bearing: the
    // dangerous end has to be the bottom of the list.
    assert.deepEqual([...DYNAMIC_INVENTORY_MODES], ['none', 'plugins', 'scripts', 'both'])
    assert.deepEqual(flagsFromMode('none'), { plugins: false, scripts: false })
    assert.deepEqual(flagsFromMode('both'), { plugins: true, scripts: true })
  })

  it('the default state is the one two absent columns read as', () => {
    // Both columns default false server-side, so a workspace that has never
    // touched either must land on `none` rather than on a blank control.
    assert.equal(modeFromFlags(false, false), 'none')
  })
})
