// A run's resource profile, per phase (#2475).
//
// The flat `peak-memory-bytes` on a run is whichever phase reported LAST. A
// composite run is two runner Jobs — an apply, then a configure — driven by one
// Run, so on those it is the configure's figure, and a configure typically needs
// a fraction of what the apply did. Sizing a runner off it is the defect this
// exists to fix.
//
// The logic lives here rather than inline in the component because the property
// worth pinning is WHICH number is chosen, and that cannot be asserted through
// rendered markup without a DOM: a test that drives the component would be
// checking the bar's width, which is the same assertion two layers of
// indirection away from the decision.

export interface PhaseProfile {
  'peak-memory-bytes': number | null
  'peak-cpu-usec': number | null
  'exit-code': number | null
}

// The phases in the order they RUN, not the order a map happens to iterate.
// JSON object order is the server's insertion order, which is the order the
// phases reported — the same thing today, and not something to depend on.
export const PHASE_ORDER = ['plan', 'apply', 'configure'] as const

export type PhaseName = (typeof PHASE_ORDER)[number]

/** The phases that reported a memory peak, in run order. */
export function reportedPhases(
  profiles: Record<string, PhaseProfile> | undefined,
): ReadonlyArray<readonly [PhaseName, PhaseProfile]> {
  if (!profiles) return []
  const out: Array<readonly [PhaseName, PhaseProfile]> = []
  for (const phase of PHASE_ORDER) {
    const prof = profiles[phase]
    // A phase that could read none of the cgroup files reported nothing worth
    // a row: showing it as "—" would read as "measured, and nothing", and
    // showing it as 0 would read as "measured, and zero".
    // `!= null`, not `!== null`: a MISSING key is `undefined`, which `!==`
    // admits, and `shownPeakBytes` then casts it into `Math.max(...)` and
    // renders NaN for the figure and the bar. The server writes a profile when
    // ANY figure is non-null, so a document carrying only an exit code is a
    // real shape rather than a hypothetical one.
    if (prof && prof['peak-memory-bytes'] != null) out.push([phase, prof])
  }
  return out
}

/**
 * The peak to show against the limit: the HIGHEST phase, not the last.
 *
 * Falls back to the flat value when no phase reported, which is every run from
 * before the field existed — so nothing about those changes. Deliberately not
 * the other way round: a run WITH per-phase figures should never be described
 * by the flat one, because that is the number this fixes.
 */
export function shownPeakBytes(
  profiles: Record<string, PhaseProfile> | undefined,
  flatPeakBytes: number | null,
): number | null {
  const peaks = reportedPhases(profiles).map(([, p]) => p['peak-memory-bytes'] as number)
  return peaks.length > 0 ? Math.max(...peaks) : flatPeakBytes
}
