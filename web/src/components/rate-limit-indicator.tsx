'use client'

import { useFormatter, useTranslations } from 'next-intl'

/**
 * VCS provider rate-limit budget for one connection (#1334).
 *
 * Three states, and keeping them distinct is the whole point:
 *
 *  - **not reported** — the server sends no rate-limit headers (a self-hosted
 *    GitLab may have them off entirely). Shown as "Not reported", never as a
 *    zero, which would read as a permanent outage, and never as a full budget,
 *    which would hide a real one.
 *  - **a budget** — remaining of limit, coloured by how close to the floor it is.
 *  - **exhausted** — called out by name, because at zero the provider refuses
 *    everything and runs stop appearing across every workspace on the connection.
 *
 * The reading is an observation from the last call Terrapod made, not a live
 * query, so the "as of" time is part of the display rather than a detail: a
 * stale number presented as current would be worse than showing nothing.
 */
export function RateLimitIndicator({
  limit,
  remaining,
  observedAt,
  className = '',
}: {
  limit: number | null | undefined
  remaining: number | null | undefined
  observedAt: string | null | undefined
  className?: string
}) {
  const t = useTranslations('vcsConnections.rateLimit')
  const fmt = useFormatter()

  if (limit == null || remaining == null || limit <= 0) {
    return <span className={`text-xs text-slate-500 ${className}`}>{t('notReported')}</span>
  }

  const ratio = remaining / limit
  const exhausted = remaining <= 0
  const tone = exhausted || ratio <= 0.1
    ? 'bg-red-900/40 text-red-300'
    : ratio <= 0.35
      ? 'bg-amber-900/40 text-amber-300'
      : 'bg-slate-700 text-slate-300'

  return (
    <span className={`inline-flex flex-col gap-0.5 ${className}`}>
      <span className={`inline-flex w-fit items-center px-2 py-0.5 rounded-full text-xs font-medium ${tone}`}>
        {exhausted
          ? t('exhausted')
          : t('remaining', { remaining: fmt.number(remaining), limit: fmt.number(limit) })}
      </span>
      {observedAt ? (
        <span className="text-[11px] text-slate-500">
          {t('observedAt', { time: fmt.relativeTime(new Date(observedAt)) })}
        </span>
      ) : null}
    </span>
  )
}
