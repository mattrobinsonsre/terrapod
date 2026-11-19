import type { JSX } from 'react'

/** The two indicators an artifact carries, and they are deliberately TWO.
 *
 * A single severity scale that meant both "signature verification FAILED" and
 * "has 4 known CVEs" teaches a reviewer the opposite of what we intend: it makes
 * an advisory-by-design CVE look like an incident, and buries a real
 * verification failure among many of them (#2589).
 *
 * So verification is a badge with three states, and findings are a separate,
 * quieter count. Only `failed` earns critical styling, because only `failed`
 * means something happened.
 *
 * **Neither is colour-only.** `status-dot-pairing.test.ts` records why: no
 * single mid-chroma fill clears 3:1 against both themes' surfaces, so the WORD
 * states the status and the colour reinforces it. Every state here renders its
 * label visibly.
 */

export type VerificationState = 'verified' | 'unverified' | 'failed'

const VERIFICATION_STYLES: Record<VerificationState, string> = {
  // Quiet. "Verified" is the expected state, and a page where the normal case
  // shouts has no room left to raise its voice for the one that matters.
  verified: 'bg-emerald-900/40 text-emerald-300 ring-emerald-700/50',
  // Also quiet, and deliberately NOT a warning: an artifact whose publisher
  // signs nothing is not a problem anybody can act on, and amber here would be
  // a permanent alarm on every opa binary in the estate.
  unverified: 'bg-slate-700/50 text-slate-300 ring-slate-600/50',
  // The one critical state on this page.
  failed: 'bg-red-900/50 text-red-200 ring-red-600/60 font-semibold',
}

interface VerificationBadgeProps {
  state: VerificationState | string
  /** The already-translated word. Rendered VISIBLY — it is what states the
   *  status, not an accessible-name-only fallback. */
  label: string
}

export function VerificationBadge({ state, label }: VerificationBadgeProps): JSX.Element {
  const style =
    VERIFICATION_STYLES[state as VerificationState] ?? VERIFICATION_STYLES.unverified
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-xs ring-1 ring-inset ${style}`}
    >
      <span aria-hidden="true" className="text-[0.9em] leading-none">
        {state === 'failed' ? '✗' : state === 'verified' ? '✓' : '–'}
      </span>
      {/* The word in its own element, not a bare text node beside the glyph.
          It is what STATES the status -- the colour only reinforces it -- so
          it has to be addressable on its own, by a test and by anything else
          reading the page. As a sibling text node the badge's text content is
          "<glyph>Verified", which an exact match never finds. */}
      <span>{label}</span>
    </span>
  )
}

export interface SupplyChain {
  state: 'inventoried' | 'findings' | 'not-inventoried' | string
  reason: string | null
  findings: number
  critical: number
  malicious: number
  verdict: Record<string, unknown> | null
}

interface SupplyChainCellProps {
  supply: SupplyChain | null | undefined
  /** Pre-translated, so this component holds no copy of its own.
   *
   *  The two counted ones are FUNCTIONS rather than strings with a placeholder:
   *  these are ICU plurals, so the count has to reach next-intl's formatter --
   *  a string interpolated here would already have been resolved against some
   *  other number, and every row would show that one. */
  labels: {
    inventoried: string
    notInventoried: string
    findings: (count: number) => string
    malicious: (count: number) => string
  }
}

/** The quieter of the two indicators.
 *
 * **Three states, never two**, and the third carries its reason. A blank cell
 * must never read as safe: "we did not look" and "we looked and found nothing"
 * are the two facts a reviewer most needs told apart, and only one of them is
 * reassuring.
 */
export function SupplyChainCell({ supply, labels }: SupplyChainCellProps): JSX.Element {
  if (!supply || supply.state === 'not-inventoried') {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-slate-400">
        <span aria-hidden="true">{'—'}</span>
        {labels.notInventoried}
      </span>
    )
  }

  if (supply.state === 'findings') {
    return (
      <span className="inline-flex items-center gap-2 text-xs">
        <span className="text-amber-300">
          {labels.findings(supply.findings)}
        </span>
        {supply.malicious > 0 && (
          // Separate from the count on purpose: a malicious package is not a
          // bug in a dependency, it is the artifact being hostile — and the one
          // finding class that is NOT advisory by design.
          <span className="rounded px-1.5 py-0.5 text-[0.7rem] font-semibold bg-red-900/50 text-red-200 ring-1 ring-inset ring-red-600/60">
            {labels.malicious(supply.malicious)}
          </span>
        )}
      </span>
    )
  }

  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-slate-300">
      <span aria-hidden="true" className="text-emerald-400">
        {'✓'}
      </span>
      {labels.inventoried}
    </span>
  )
}
