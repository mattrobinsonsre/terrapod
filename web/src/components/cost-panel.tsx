'use client'

// Cost panel (#871) — the Cost tab for BOTH a run and a workspace.
//
//  • run (runId):       the runner-produced estimate of the plan's monthly cost
//                       *delta* — shows Monthly total + This-run + Previous.
//  • workspace (workspaceId): the API-produced estimate of the workspace's
//                       *current* managed-infra cost from its latest state —
//                       shows just Monthly total (state carries no delta) plus
//                       which state version it priced. Every resource is a noop,
//                       so the per-resource Change column is dropped.
//
// Every figure here is DATA — oiq-derived, no AI. The AI enhancement (narrative,
// savings advisories, chat) is a separate follow-up that rides the plan-analysis
// AI switch; it renders alongside, clearly flagged, never blended into these.
import { Fragment, useEffect, useState } from 'react'
import { useLocale, useTranslations } from 'next-intl'
import { apiFetch } from '@/lib/api'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'

type Change = 'add' | 'remove' | 'noop'
interface Range {
  min: number
  max: number
}
interface UsageAssumption {
  description: string
  dimension: string
  unit: string
  low: number
  typical: number
  high: number
}
interface CostResource {
  address: string
  type: string
  name: string
  change: Change
  monthly: Range
  usage_assumptions?: UsageAssumption[]
}
interface UnpricedResource {
  address: string
  type: string
  change: Change
}
interface StateVersionMeta {
  id: string
  serial: number
  'created-at': string
}
interface Estimate {
  currency: string
  total: Range
  previous: Range
  diff: Range
  resources: CostResource[]
  unpriced: UnpricedResource[]
  'state-version'?: StateVersionMeta | null
}

const CHANGE_BADGE: Record<Change, string> = {
  add: 'bg-emerald-900/40 text-emerald-300',
  remove: 'bg-red-900/40 text-red-300',
  noop: 'bg-slate-700 text-slate-300',
}

export function CostPanel({ runId, workspaceId }: { runId?: string; workspaceId?: string }) {
  const t = useTranslations('runDetail')
  const locale = useLocale()
  const [est, setEst] = useState<Estimate | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Workspace (current state cost) vs run (plan delta) — the only differences
  // are the endpoint, the extra delta headline cards, and the Change column.
  const isWorkspace = !!workspaceId
  const endpoint = isWorkspace
    ? `/api/terrapod/v1/workspaces/${workspaceId}/cost-estimate`
    : `/api/terrapod/v1/runs/${runId}/cost-estimate`

  useEffect(() => {
    let cancelled = false
    apiFetch(endpoint)
      .then(async (res) => {
        if (!res.ok) throw new Error(t('cost.notAvailable'))
        const body = await res.json()
        if (!cancelled) setEst(body.data.attributes as Estimate)
      })
      .catch((e: Error) => !cancelled && setError(e.message))
    return () => {
      cancelled = true
    }
  }, [endpoint, t])

  if (error) return <ErrorBanner message={error} />
  if (!est) return <LoadingSpinner />

  // Numbers stay LTR + tabular even under an RTL locale (money/addresses are
  // structural, not translated prose).
  const money = (n: number) =>
    new Intl.NumberFormat(locale, {
      style: 'currency',
      currency: est.currency || 'USD',
      maximumFractionDigits: 2,
    }).format(n)
  // A range renders as one figure when min == max, else "min – max".
  const range = (r: Range) => (r.min === r.max ? money(r.min) : `${money(r.min)} – ${money(r.max)}`)
  const perMonth = (r: Range) => t('cost.perMonth', { amount: range(r) })

  // Usage-driven resources (NAT data, Lambda invocations, S3 storage…) can't be
  // priced deterministically from the plan — the cost depends on runtime usage
  // the plan doesn't declare. We surface the assumption band directly (not only
  // to the AI layer, which is optional) so the raw estimate is honest: this cost
  // assumes `typical`, and could sit anywhere in `low`–`high` (#962).
  const qty = (n: number) => new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(n)
  const bandLine = (a: UsageAssumption) =>
    t('cost.usageBand', {
      dimension: a.dimension,
      typical: qty(a.typical),
      low: qty(a.low),
      high: qty(a.high),
      unit: a.unit,
    })
  const Bands = ({ list }: { list?: UsageAssumption[] }) =>
    list && list.length ? (
      <ul className="mt-1 space-y-0.5">
        {list.map((a) => (
          <li key={a.dimension} className="flex items-start gap-1 text-xs text-amber-300/80">
            <span aria-hidden>≈</span>
            <span>{bandLine(a)}</span>
          </li>
        ))}
      </ul>
    ) : null

  const priced = est.resources.length
  const unpriced = est.unpriced.length
  const diffPositive = est.diff.max > 0
  const diffNegative = est.diff.max < 0 && est.diff.min < 0
  const sv = est['state-version'] ?? null

  // Workspace with no state yet: a friendly empty state, not a $0 table.
  if (isWorkspace && !sv) {
    return (
      <div className="rounded-xl border border-slate-700 bg-slate-800/30 p-6 text-sm text-slate-400">
        {t('cost.noState')}
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      {/* Headline stat cards. Run: total + this-run delta + previous. Workspace:
          just the current total (state carries no delta). */}
      <div className={`grid grid-cols-1 gap-3 ${isWorkspace ? 'sm:max-w-xs' : 'sm:grid-cols-3'}`}>
        <div className="rounded-xl border border-slate-700 bg-slate-800/50 p-4">
          <div className="flex items-center justify-between gap-2">
            <div className="text-xs uppercase tracking-wide text-slate-400">
              {t('cost.monthlyTotal')}
            </div>
            {isWorkspace && sv && (
              <span className="text-xs text-slate-500">{t('cost.asOfState', { serial: sv.serial })}</span>
            )}
          </div>
          <div className="mt-1 text-2xl font-semibold tabular-nums text-slate-100" dir="ltr">
            {perMonth(est.total)}
          </div>
          <div className="mt-1 text-xs text-slate-500">{t('cost.pricedCount', { count: priced })}</div>
        </div>
        {!isWorkspace && (
          <>
            <div className="rounded-xl border border-slate-700 bg-slate-800/50 p-4">
              <div className="text-xs uppercase tracking-wide text-slate-400">{t('cost.thisRun')}</div>
              <div
                className={`mt-1 text-2xl font-semibold tabular-nums ${
                  diffPositive ? 'text-emerald-300' : diffNegative ? 'text-red-300' : 'text-slate-100'
                }`}
                dir="ltr"
              >
                {diffPositive ? '+' : ''}
                {perMonth(est.diff)}
              </div>
              <div className="mt-1 text-xs text-slate-500">{t('cost.thisRunHint')}</div>
            </div>
            <div className="rounded-xl border border-slate-700 bg-slate-800/50 p-4">
              <div className="text-xs uppercase tracking-wide text-slate-400">{t('cost.previous')}</div>
              <div className="mt-1 text-2xl font-semibold tabular-nums text-slate-100" dir="ltr">
                {perMonth(est.previous)}
              </div>
              <div className="mt-1 text-xs text-slate-500">{t('cost.previousHint')}</div>
            </div>
          </>
        )}
      </div>

      {/* Per-resource breakdown */}
      {priced > 0 && (
        <>
          {/* Mobile (< sm): stacked cards — no inner horizontal scroll, and the
              Monthly figure (a primary signal) stays fully visible (#719). */}
          <div className="flex flex-col gap-2 sm:hidden">
            {est.resources.map((r) => (
              <div key={r.address} className="rounded-lg border border-slate-800 bg-slate-800/30 p-3">
                <div className="break-all font-mono text-xs text-slate-200" dir="ltr">
                  {r.address}
                </div>
                <div className="mt-2 flex items-center justify-between gap-2">
                  {isWorkspace ? (
                    <span className="font-mono text-xs text-slate-400" dir="ltr">
                      {r.type}
                    </span>
                  ) : (
                    <span
                      className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${CHANGE_BADGE[r.change]}`}
                    >
                      {t(`cost.change.${r.change}`)}
                    </span>
                  )}
                  <span className="tabular-nums text-sm text-slate-200" dir="ltr">
                    {perMonth(r.monthly)}
                  </span>
                </div>
                <Bands list={r.usage_assumptions} />
              </div>
            ))}
          </div>

          {/* Desktop (sm+): full table. */}
          <div className="hidden overflow-x-auto rounded-xl border border-slate-700 sm:block">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-700 text-left text-xs uppercase tracking-wide text-slate-400">
                  <th className="px-4 py-2 font-medium">{t('cost.colResource')}</th>
                  <th className="px-4 py-2 font-medium">{t('cost.colType')}</th>
                  {!isWorkspace && <th className="px-4 py-2 font-medium">{t('cost.colChange')}</th>}
                  <th className="px-4 py-2 text-right font-medium">{t('cost.colMonthly')}</th>
                </tr>
              </thead>
              <tbody>
                {est.resources.map((r) => {
                  const bands = r.usage_assumptions
                  return (
                    <Fragment key={r.address}>
                      <tr
                        className={
                          bands?.length ? 'border-slate-800' : 'border-b border-slate-800 last:border-0'
                        }
                      >
                        <td className="px-4 py-2 font-mono text-xs text-slate-200" dir="ltr">
                          {r.address}
                        </td>
                        <td className="px-4 py-2 font-mono text-xs text-slate-400" dir="ltr">
                          {r.type}
                        </td>
                        {!isWorkspace && (
                          <td className="px-4 py-2">
                            <span
                              className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${CHANGE_BADGE[r.change]}`}
                            >
                              {t(`cost.change.${r.change}`)}
                            </span>
                          </td>
                        )}
                        <td className="px-4 py-2 text-right tabular-nums text-slate-200" dir="ltr">
                          {perMonth(r.monthly)}
                        </td>
                      </tr>
                      {bands?.length ? (
                        <tr className="border-b border-slate-800 last:border-0">
                          <td colSpan={isWorkspace ? 3 : 4} className="px-4 pb-2">
                            <Bands list={bands} />
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        </>
      )}

      {/* Unpriced resources — nothing in the pricesheet matched them. */}
      {unpriced > 0 && (
        <div className="rounded-xl border border-slate-700 bg-slate-800/30 p-4">
          <div className="text-sm font-medium text-slate-300">
            {t('cost.unpricedTitle', { count: unpriced })}
          </div>
          <p className="mt-1 text-xs text-slate-500">{t('cost.unpricedHint')}</p>
          <ul className="mt-2 flex flex-wrap gap-2">
            {est.unpriced.map((u) => (
              <li
                key={u.address}
                className="rounded bg-slate-800 px-2 py-1 font-mono text-xs text-slate-400"
                dir="ltr"
              >
                {u.address}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Provenance + credit — cost data comes from OpenInfraQuote. */}
      <div className="text-xs text-slate-500">
        <p>{t('cost.indicative')}</p>
        <p className="mt-1">
          {t.rich('cost.credit', {
            oiq: (chunks) => (
              <a
                href="https://github.com/terrateamio/openinfraquote"
                target="_blank"
                rel="noopener noreferrer"
                className="text-brand-400 hover:underline"
                dir="ltr"
              >
                {chunks}
              </a>
            ),
          })}
        </p>
      </div>
    </div>
  )
}
