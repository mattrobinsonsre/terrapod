// Which peak the resource panel shows (#2475).
//
// The reported defect was that a composite run's figures were overwritten: a
// configure and an apply are two runner Jobs driven by one Run, and both report
// to the same three scalar columns, so the last one in won. The per-phase column
// fixes the storage; these pin the READING, which is where an operator meets it.
//
// The property is "which number", so it is asserted on the number. Driven
// through the component it would only be observable as a bar's width, which is
// the same assertion two layers of indirection away from the decision.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  PHASE_ORDER,
  reportedPhases,
  shownPeakBytes,
  type PhaseProfile,
} from '../src/lib/phase-profiles.ts'

const prof = (mem: number | null): PhaseProfile => ({
  'peak-memory-bytes': mem,
  'peak-cpu-usec': 1,
  'exit-code': 0,
})

// The shape the report describes: an apply needing 5 GiB, then a configure
// needing 100 MiB, with the flat field holding the configure's because it
// reported last.
const COMPOSITE = { apply: prof(5 * 1024 ** 3), configure: prof(100 * 1024 ** 2) }
const FLAT_IS_THE_CONFIGURES = 100 * 1024 ** 2

test('the shown peak is the highest phase, not the last one to report', () => {
  assert.equal(shownPeakBytes(COMPOSITE, FLAT_IS_THE_CONFIGURES), 5 * 1024 ** 3)
  // Stated as a contrast, because agreement is what the defect looked like:
  // the flat value and the right answer DIFFER on this run, and a reading that
  // took the flat one would have an operator size a runner for 100 MiB when the
  // apply needed fifty times that.
  assert.notEqual(shownPeakBytes(COMPOSITE, FLAT_IS_THE_CONFIGURES), FLAT_IS_THE_CONFIGURES)
})

test('a run with no per-phase figures falls back to the flat one', () => {
  // Every run from before the column, which is most of them. Unchanged
  // behaviour is the point: this change must not blank their panel.
  assert.equal(shownPeakBytes(undefined, 1234), 1234)
  assert.equal(shownPeakBytes({}, 1234), 1234)
  assert.equal(shownPeakBytes(undefined, null), null)
})

test('the fallback never runs the other way round', () => {
  // A run WITH per-phase figures is never described by the flat one, even when
  // the flat one is larger — which cannot happen from the API today, and is
  // exactly the kind of thing a later change could introduce.
  assert.equal(shownPeakBytes({ apply: prof(10) }, 99999), 10)
})

test('a phase that reported no memory is not shown as a row', () => {
  // Null means the runner could not read the cgroup file. A row of "—" reads as
  // "measured, and nothing"; a row of 0 reads as "measured, and zero".
  const phases = reportedPhases({ plan: prof(null), apply: prof(500) })
  assert.deepEqual(
    phases.map(([p]) => p),
    ['apply'],
  )
})

test('phases are listed in the order they run, not the order they arrived', () => {
  // JSON object order is the server's insertion order, i.e. the order the
  // phases reported. That is the same thing today and not worth depending on:
  // a configure that reported before an apply finished would otherwise be
  // listed first and read as having run first.
  const reversed = { configure: prof(1), apply: prof(2), plan: prof(3) }
  assert.deepEqual(
    reportedPhases(reversed).map(([p]) => p),
    ['plan', 'apply', 'configure'],
  )
})

test('an unknown phase is ignored rather than rendered', () => {
  // A newer runner could report a phase this build has no label for, and a row
  // with no label is worse than no row. PHASE_ORDER is the list, so adding a
  // phase is a deliberate edit here and in the labels at the same time.
  const phases = reportedPhases({ apply: prof(1), destroy: prof(2) })
  assert.deepEqual(
    phases.map(([p]) => p),
    ['apply'],
  )
  assert.ok(!PHASE_ORDER.includes('destroy' as never))
})

test('a single-phase run shows its own phase and agrees with the flat value', () => {
  // The ordinary plan-only run: nothing about the panel should change for it,
  // and the two readings must agree or the composite case is being fixed by
  // making the simple case wrong.
  assert.equal(shownPeakBytes({ plan: prof(777) }, 777), 777)
  assert.deepEqual(
    reportedPhases({ plan: prof(777) }).map(([p]) => p),
    ['plan'],
  )
})
