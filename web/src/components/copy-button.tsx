'use client'

import { Check, Copy, TriangleAlert } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslations } from 'next-intl'

interface CopyButtonProps {
  /** The text to put on the clipboard. */
  value: string
  /** Overrides the idle label. Omit for the shared "Copy". */
  label?: string
  /** Extra LAYOUT classes, appended. Colour is owned by the component, so
   *  that the failure state can always render — a caller-supplied colour would
   *  collide with it and the outcome would be invisible. */
  className?: string
  /** `data-testid`, for the specs that assert a particular copy control. */
  testId?: string
  /** Hide the text label below `sm`, as the run-log control does. */
  responsiveLabel?: boolean
}

type Outcome = 'idle' | 'copied' | 'failed'

const RESET_MS = 2000

/**
 * Copy-to-clipboard with an outcome the user can actually see.
 *
 * Four call sites grew four behaviours, and the two with none were the two that
 * mattered most: the API token and the agent-pool join token, both shown once
 * and never again. A bare `navigator.clipboard.writeText(...)` looks like a dead
 * click, which invites a second click and then navigating away believing the
 * secret is held (#2448).
 *
 * Three things this does that none of the four did:
 *
 * * **Reports a FAILURE, not just a success.** `writeText` returns a promise
 *   that rejects on an insecure context or a denied permission, and three of the
 *   four sites ignored it — so a failed copy rendered identically to a good one.
 *   On a write-once secret that is the difference between having it and losing
 *   it. A failure is also sticky rather than timing out, because it needs
 *   acting on, and it says to select the text by hand instead.
 * * **Survives a missing clipboard object.** `navigator.clipboard` is undefined
 *   on an insecure origin, where the bare calls would throw.
 * * **Announces the outcome.** The existing feedback was an icon swap plus a
 *   label, which a screen reader does not report. The live region does.
 */
export function CopyButton({
  value,
  label,
  className,
  testId,
  responsiveLabel = false,
}: CopyButtonProps) {
  const t = useTranslations('common')
  const [outcome, setOutcome] = useState<Outcome>('idle')
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // A pending reset outliving the component would set state after unmount.
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    [],
  )

  const copy = useCallback(async () => {
    if (timer.current) clearTimeout(timer.current)
    try {
      if (!navigator.clipboard?.writeText) throw new Error('no clipboard')
      await navigator.clipboard.writeText(value)
      setOutcome('copied')
      // Only a SUCCESS times out. A failure stays until the next attempt.
      timer.current = setTimeout(() => setOutcome('idle'), RESET_MS)
    } catch {
      setOutcome('failed')
    }
  }, [value])

  const text =
    outcome === 'copied' ? t('copy.copied') : outcome === 'failed' ? t('copy.failed') : (label ?? t('copy.button'))

  const Icon = outcome === 'copied' ? Check : outcome === 'failed' ? TriangleAlert : Copy

  return (
    <>
      <button
        type="button"
        onClick={copy}
        data-testid={testId}
        aria-label={outcome === 'failed' ? t('copy.failedAria') : (label ?? t('copy.button'))}
        title={outcome === 'failed' ? t('copy.failedTitle') : undefined}
        className={
          'px-3 py-2 min-h-[2.25rem] rounded text-xs font-medium transition-colors inline-flex items-center gap-1.5 ' +
          (outcome === 'failed'
            ? 'bg-red-900/40 text-red-200 hover:bg-red-900/60'
            : 'bg-slate-700 text-slate-200 hover:bg-slate-600') +
          (className ? ' ' + className : '')
        }
      >
        <Icon
          className={
            'w-3.5 h-3.5 flex-shrink-0 ' +
            (outcome === 'copied' ? 'text-emerald-300' : outcome === 'failed' ? 'text-red-300' : '')
          }
          aria-hidden="true"
        />
        <span className={responsiveLabel ? 'hidden sm:inline' : undefined}>{text}</span>
      </button>
      {/* Announced rather than only shown: the icon swap is invisible to a screen reader. */}
      <span aria-live="polite" className="sr-only">
        {outcome === 'copied' ? t('copy.copied') : outcome === 'failed' ? t('copy.failedAria') : ''}
      </span>
    </>
  )
}
