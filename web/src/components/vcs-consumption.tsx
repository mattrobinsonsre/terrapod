'use client'

import { useState } from 'react'
import { useTranslations } from 'next-intl'
import { useFormat } from '@/lib/format'

/**
 * VCS provider budget strain for one connection (#1339).
 *
 * This deliberately does not lead with "4,991 of 5,000". A budget level cannot
 * answer the question an operator has, because the budget refills on a fixed
 * window: right after a reset it reads healthy however fast it is being spent.
 * A connection burning 11,400 calls/hour against a 5,000/hour budget looked
 * completely fine for part of every hour while runs were silently not
 * appearing.
 *
 * What answers it is the *rate* against the refill, so that is what leads: a
 * verdict, the consumption rate as a share of the budget, and when it runs out.
 * The breakdown underneath exists because knowing you are over budget is only
 * half of it — the other half is which repos to move, and which line to split
 * them along.
 */

type Consumer = { name: string; kind: string; calls: number }
type LabelTotal = { label: string; key: string; value: string; calls: number }

export type ConnectionConsumption = {
  'rate-limit'?: number | null
  'rate-limit-remaining'?: number | null
  'rate-limit-observed-at'?: string | null
  'calls-per-hour'?: number | null
  'rate-window-minutes'?: number | null
  'seconds-to-reset'?: number | null
  saturation?: string | null
  'exhausts-in-seconds'?: number | null
  'top-consumers'?: Consumer[] | null
  'label-totals'?: LabelTotal[] | null
}

const TONE: Record<string, string> = {
  exhausted: 'bg-red-900/40 text-red-300',
  will_exhaust: 'bg-red-900/40 text-red-300',
  tight: 'bg-amber-900/40 text-amber-300',
  comfortable: 'bg-slate-700 text-slate-300',
  idle: 'bg-slate-700/50 text-slate-400',
}

const VERDICT_KEY: Record<string, string> = {
  exhausted: 'exhausted',
  will_exhaust: 'willExhaust',
  tight: 'tight',
  comfortable: 'comfortable',
  idle: 'idle',
}

/** Whole minutes/hours — a to-the-second countdown would imply precision the
 *  underlying observation does not have. */
function useDuration() {
  const t = useTranslations('adminVcs.rateLimit')
  return (seconds: number | null | undefined) => {
    if (seconds == null || seconds < 0) return null
    if (seconds < 60) return t('durationS', { s: Math.round(seconds) })
    const m = Math.round(seconds / 60)
    if (m < 60) return t('durationM', { m })
    return t('durationHm', { h: Math.floor(m / 60), m: m % 60 })
  }
}

export function VCSConsumption({
  attrs,
  className = '',
}: {
  attrs: ConnectionConsumption
  className?: string
}) {
  const t = useTranslations('adminVcs.rateLimit')
  const fmt = useFormat()
  const duration = useDuration()
  const [open, setOpen] = useState(false)

  const verdict = attrs.saturation ?? null
  const rate = attrs['calls-per-hour'] ?? null
  const limit = attrs['rate-limit'] ?? null
  const observedAt = attrs['rate-limit-observed-at'] ?? null

  // Nothing observed and nothing spent: say so rather than showing a healthy
  // verdict for a connection we have no reading on.
  if (verdict == null && rate == null) {
    return <span className={`text-xs text-slate-500 ${className}`}>{t('notReported')}</span>
  }

  const tone = TONE[verdict ?? 'idle'] ?? TONE.idle
  const verdictLabel = t(`saturation.${VERDICT_KEY[verdict ?? 'idle'] ?? 'idle'}`)
  const strained = verdict === 'tight' || verdict === 'will_exhaust' || verdict === 'exhausted'

  // Rate as a share of the budget is the clearest single statement of strain —
  // "228% of budget" needs no further explanation.
  const share = rate != null && limit != null && limit > 0 ? Math.round((rate / limit) * 100) : null
  const runsOut = duration(attrs['exhausts-in-seconds'])
  const resetsIn = duration(attrs['seconds-to-reset'])

  const consumers = attrs['top-consumers'] ?? []
  const labels = attrs['label-totals'] ?? []
  const hasBreakdown = consumers.length > 0 || labels.length > 0

  return (
    <div className={`flex flex-col gap-1 ${className}`}>
      <span
        className={`inline-flex w-fit items-center px-2 py-0.5 rounded-full text-xs font-medium ${tone}`}
      >
        {verdictLabel}
      </span>

      {rate != null ? (
        <span className="text-xs text-slate-300">
          {t('rate', { calls: fmt.number(rate) })}
          {share != null ? (
            <span className="text-slate-500"> · {t('ofBudget', { percent: share })}</span>
          ) : null}
        </span>
      ) : null}

      <span className="text-[11px] text-slate-500">
        {verdict === 'exhausted' && resetsIn
          ? t('resetsIn', { time: resetsIn })
          : runsOut && strained
            ? t('runsOutIn', { time: runsOut })
            : resetsIn
              ? t('resetsIn', { time: resetsIn })
              : observedAt
                ? t('observedAt', { time: fmt.relativeTime(new Date(observedAt)) })
                : null}
      </span>

      {hasBreakdown ? (
        <>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="w-fit px-2 py-1 rounded-lg text-[11px] font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors"
          >
            {open ? t('hideBreakdown') : t('showBreakdown')}
          </button>
          {open ? (
            <Breakdown
              consumers={consumers}
              labels={labels}
              total={rate ?? 0}
              windowMinutes={attrs['rate-window-minutes'] ?? 60}
              strained={strained}
            />
          ) : null}
        </>
      ) : null}
    </div>
  )
}

function Breakdown({
  consumers,
  labels,
  total,
  windowMinutes,
  strained,
}: {
  consumers: Consumer[]
  labels: LabelTotal[]
  total: number
  windowMinutes: number
  strained: boolean
}) {
  const t = useTranslations('adminVcs.rateLimit')
  const fmt = useFormat()
  const share = (calls: number) => (total > 0 ? Math.round((calls / total) * 100) : null)

  return (
    <div className="mt-1 p-3 rounded-lg bg-slate-900/60 border border-slate-700/50 flex flex-col gap-3 max-w-md">
      <p className="text-[11px] text-slate-500">{t('windowNote', { minutes: windowMinutes })}</p>

      {consumers.length > 0 ? (
        <div className="flex flex-col gap-1">
          <p className="text-xs font-medium text-slate-300">{t('consumers')}</p>
          <ul className="flex flex-col gap-1">
            {consumers.map((c) => (
              <li key={`${c.kind}/${c.name}`} className="flex items-baseline justify-between gap-3">
                <span className="flex items-baseline gap-2 min-w-0">
                  <span className="shrink-0 px-1.5 py-0.5 rounded text-[10px] font-medium bg-slate-700/70 text-slate-400">
                    {t(`kinds.${kindKey(c.kind)}`)}
                  </span>
                  <span className="text-xs text-slate-300 truncate" dir="ltr">
                    {c.name}
                  </span>
                </span>
                <span className="shrink-0 text-xs text-slate-400 tabular-nums">
                  {fmt.number(c.calls)}
                  {share(c.calls) != null ? (
                    <span className="text-slate-500"> ({share(c.calls)}%)</span>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {labels.length > 0 ? (
        <div className="flex flex-col gap-1">
          <p className="text-xs font-medium text-slate-300">{t('byLabel')}</p>
          <ul className="flex flex-col gap-1">
            {labels.map((l) => (
              <li key={l.label} className="flex items-baseline justify-between gap-3">
                <span className="text-xs text-slate-300 truncate" dir="ltr">
                  {l.label}
                </span>
                <span className="shrink-0 text-xs text-slate-400 tabular-nums">
                  {fmt.number(l.calls)}
                  {share(l.calls) != null ? (
                    <span className="text-slate-500"> ({share(l.calls)}%)</span>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* Only when it matters. Visibility is worth little on its own — the
          point of showing the strain is that there are two things to do about
          it, and the breakdown above is what tells you which. */}
      {strained ? (
        <div className="pt-2 border-t border-slate-700/50 flex flex-col gap-1">
          <p className="text-xs font-medium text-slate-300">{t('remedies.title')}</p>
          <p className="text-[11px] text-slate-400">{t('remedies.pollLess')}</p>
          <p className="text-[11px] text-slate-400">{t('remedies.split')}</p>
        </div>
      ) : null}
    </div>
  )
}

function kindKey(kind: string): string {
  if (kind === 'workspace') return 'workspace'
  if (kind === 'module') return 'module'
  if (kind === 'policy-set') return 'policySet'
  return 'other'
}
