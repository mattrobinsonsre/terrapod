'use client'

/**
 * AI cost-estimate panel (#871) — the AI layer of the run Cost tab.
 *
 * Renders BESIDE the deterministic CostPanel, never blended into it. Its
 * PRIMARY output is `estimated-resources`: the model pricing what the deterministic engine
 * could NOT (the unpriced bucket — unmapped types, providers the engine doesn't cover
 * like Azure/GCP, usage-driven costs). Every figure is an ESTIMATE, stamped
 * `source: "ai-estimate"` server-side, shown distinctly and never summed into
 * the authoritative deterministic total. Savings `advisories` and the prose `narrative`
 * are the secondary, human-readable bonus.
 *
 * Mirrors PlanAiSummary: fetch + status states (pending/ready/errored/skipped) +
 * regenerate + SSE-driven refresh via `refreshKey` (the caller bumps it on the
 * `cost_summary_*` events). Renders nothing when the feature is off (404).
 */

import type React from 'react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslations, useLocale } from 'next-intl'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Sparkles, RefreshCw, TrendingDown, Sigma } from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { useIsTouch } from '@/lib/use-media-query'
import { LoadingSpinner } from '@/components/loading-spinner'
import { CostSummaryChat } from '@/components/cost-summary-chat'

interface Range {
  min: number
  max: number
}

interface EstimatedResource {
  address: string
  type: string
  monthly: Range
  basis: string
  source: string
}

type AdvisoryKind = 'savings_plan' | 'reserved' | 'spot' | 'rightsizing' | 'other'

interface Advisory {
  kind: AdvisoryKind
  title: string
  detail: string
  monthly_saving: Range | null
  source: string
}

interface CostSummary {
  id: string
  type: string
  attributes: {
    status: 'pending' | 'ready' | 'errored' | 'skipped'
    'estimated-resources': EstimatedResource[]
    narrative: string
    advisories: Advisory[]
    model: string
    'input-tokens': number
    'output-tokens': number
    'error-message': string
    /** Canonical language the prose is stored in (#871). */
    language?: string
    /** True when the served prose was translated on view for the reader. */
    translated?: boolean
    'created-at': string
    'updated-at': string
  }
}

interface Props {
  /** Bare run UUID, no `run-` prefix. */
  runId: string
  /** Bump to force refetch (from SSE cost_summary_* events). */
  refreshKey?: number
}

const ADVISORY_BADGE: Record<AdvisoryKind, string> = {
  savings_plan: 'bg-emerald-900/40 text-emerald-300',
  reserved: 'bg-emerald-900/40 text-emerald-300',
  spot: 'bg-sky-900/40 text-sky-300',
  rightsizing: 'bg-amber-900/40 text-amber-300',
  other: 'bg-slate-700 text-slate-300',
}

export function CostAiSummary({ runId, refreshKey = 0 }: Props) {
  const t = useTranslations('runDetail')
  // "Translating…" + the translated note are the same concept as the plan
  // summary; reuse its already-translated strings rather than duplicate keys.
  const tp = useTranslations('planSummary')
  const locale = useLocale()
  const [summary, setSummary] = useState<CostSummary | null>(null)
  const [missing, setMissing] = useState(false)
  const [loading, setLoading] = useState(true)
  const [transportError, setTransportError] = useState<string | null>(null)
  const [regenerating, setRegenerating] = useState(false)
  const [regenerateError, setRegenerateError] = useState<string | null>(null)
  // True while a locale change re-fetches the translated view — keep the
  // previous (stale-language) content on screen under a spinner (#871/#767).
  const [translating, setTranslating] = useState(false)
  const hasContentRef = useRef(false)
  const isTouch = useIsTouch()

  const load = useCallback(async () => {
    // A re-fetch while we already have content means the reader switched
    // language — show the translating spinner and keep the old content visible.
    if (hasContentRef.current) setTranslating(true)
    try {
      // Pass the reader's locale so the API translates the canonical-language
      // prose on view (#871). Re-fetches when the locale changes (load is keyed
      // on it).
      const res = await apiFetch(
        `/api/terrapod/v1/runs/run-${runId}/cost-summary?locale=${encodeURIComponent(locale)}`,
      )
      if (res.status === 404) {
        setMissing(true)
        setSummary(null)
        hasContentRef.current = false
        return
      }
      if (!res.ok) {
        setTransportError(t('costAi.errors.status', { status: res.status }))
        return
      }
      const data = await res.json()
      setSummary(data.data as CostSummary)
      hasContentRef.current = true
      setMissing(false)
      setTransportError(null)
    } catch (e) {
      setTransportError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
      setTranslating(false)
    }
  }, [runId, t, locale])

  useEffect(() => {
    load()
  }, [load, refreshKey])

  const regenerate = useCallback(async () => {
    // Touch-only confirm: a mis-tap re-runs the model (#719 tier-2 mutation).
    if (isTouch && !window.confirm(t('costAi.regenerate.confirm'))) return
    setRegenerating(true)
    setRegenerateError(null)
    try {
      const res = await apiFetch(`/api/terrapod/v1/runs/run-${runId}/cost-summary/regenerate`, {
        method: 'POST',
      })
      if (!res.ok) {
        let detail = t('costAi.errors.status', { status: res.status })
        try {
          const body = await res.json()
          if (body?.detail) detail = body.detail
        } catch {
          /* fall through to status code */
        }
        setRegenerateError(detail)
        return
      }
      const data = await res.json()
      setSummary(data.data as CostSummary)
      setMissing(false)
    } catch (e) {
      setRegenerateError(e instanceof Error ? e.message : String(e))
    } finally {
      setRegenerating(false)
    }
  }, [runId, isTouch, t])

  // Feature off (no row ever) → render nothing at all.
  if (missing) return null
  if (loading && !summary) return null
  if (transportError) return null

  const attrs = summary?.attributes
  const money = (n: number) =>
    new Intl.NumberFormat(locale, { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(n)
  const range = (r: Range) => (r.min === r.max ? money(r.min) : `${money(r.min)} – ${money(r.max)}`)
  const perMonth = (r: Range) => t('cost.perMonth', { amount: range(r) })

  const estimated = attrs?.['estimated-resources'] ?? []
  const advisories = attrs?.advisories ?? []

  return (
    <div className="rounded-xl border border-brand-800/40 bg-brand-950/10 p-4 sm:p-6">
      {/* Header — ✨ signals AI; the whole panel is estimate, so we say so. */}
      <div className="mb-4 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-brand-400" aria-hidden="true" />
          <h3 className="text-sm font-medium text-slate-200">{t('costAi.heading')}</h3>
          <span className="hidden text-xs text-slate-500 md:inline">{t('costAi.aiGenerated')}</span>
          {translating && (
            <span className="flex items-center gap-1.5 text-xs text-brand-400">
              <RefreshCw className="h-3 w-3 animate-spin" aria-hidden="true" />
              {tp('translating')}
            </span>
          )}
        </div>
        {attrs && attrs.status !== 'pending' && (
          <button
            type="button"
            onClick={regenerate}
            disabled={regenerating}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 px-3 py-1.5 text-xs font-medium text-slate-300 hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
            title={t('costAi.regenerate.tooltip')}
            aria-label={t('costAi.regenerate.ariaLabel')}
          >
            <RefreshCw className={`h-3 w-3 ${regenerating ? 'animate-spin' : ''}`} aria-hidden="true" />
            <span className="hidden md:inline">
              {regenerating ? t('costAi.regenerate.queueing') : t('costAi.regenerate.label')}
            </span>
          </button>
        )}
      </div>

      {regenerateError && (
        <div className="mb-3 rounded border border-red-800/50 bg-red-900/20 p-2 text-xs text-red-300">
          {regenerateError}
        </div>
      )}

      {attrs?.status === 'pending' && (
        <div className="flex items-center gap-3 text-sm text-slate-400">
          <LoadingSpinner />
          <span>{t('costAi.pending')}</span>
        </div>
      )}

      {attrs?.status === 'skipped' && (
        <p className="text-sm italic text-slate-500">{attrs['error-message'] || t('costAi.skipped')}</p>
      )}

      {attrs?.status === 'errored' && (
        <div className="rounded border border-red-800/50 bg-red-900/20 p-3 text-sm text-red-300">
          <div className="mb-1 font-medium">{t('costAi.errored')}</div>
          <div className="whitespace-pre-wrap break-all font-mono text-xs text-red-400/80">
            {attrs['error-message']}
          </div>
        </div>
      )}

      {attrs?.status === 'ready' && (
        <div className="flex flex-col gap-5">
          {/* PRIMARY — estimated resources (what the engine couldn't price). */}
          {estimated.length > 0 ? (
            <div>
              <div className="mb-1 flex items-center gap-2">
                <Sigma className="h-3.5 w-3.5 text-brand-400" aria-hidden="true" />
                <h4 className="text-sm font-medium text-slate-200">
                  {t('costAi.estimatedTitle', { count: estimated.length })}
                </h4>
              </div>
              <p className="mb-3 text-xs text-slate-500">{t('costAi.estimatedHint')}</p>

              {/* Mobile: stacked cards (Monthly stays visible, no inner scroll). */}
              <div className="flex flex-col gap-2 sm:hidden">
                {estimated.map((e) => (
                  <div key={e.address} className="rounded-lg border border-slate-800 bg-slate-800/30 p-3">
                    <div className="flex items-start justify-between gap-2">
                      <span className="break-all font-mono text-xs text-slate-200" dir="ltr">
                        {e.address}
                      </span>
                      <span className="whitespace-nowrap tabular-nums text-sm text-brand-200" dir="ltr">
                        ~{perMonth(e.monthly)}
                      </span>
                    </div>
                    {e.basis && <p className="mt-1.5 text-xs text-slate-400">{e.basis}</p>}
                  </div>
                ))}
              </div>

              {/* Desktop: table with a Basis column. */}
              <div className="hidden overflow-x-auto rounded-xl border border-slate-700 sm:block">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-slate-700 text-left text-xs uppercase tracking-wide text-slate-400">
                      <th className="px-4 py-2 font-medium">{t('cost.colResource')}</th>
                      <th className="px-4 py-2 font-medium">{t('costAi.colBasis')}</th>
                      <th className="px-4 py-2 text-right font-medium">{t('costAi.colEstimated')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {estimated.map((e) => (
                      <tr key={e.address} className="border-b border-slate-800 last:border-0">
                        <td className="px-4 py-2 align-top">
                          <div className="break-all font-mono text-xs text-slate-200" dir="ltr">
                            {e.address}
                          </div>
                          <div className="font-mono text-[0.7rem] text-slate-500" dir="ltr">
                            {e.type}
                          </div>
                        </td>
                        <td className="px-4 py-2 align-top text-xs text-slate-400">{e.basis}</td>
                        <td className="px-4 py-2 text-right align-top tabular-nums text-brand-200" dir="ltr">
                          ~{perMonth(e.monthly)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : (
            <p className="text-sm text-slate-500">{t('costAi.nothingToEstimate')}</p>
          )}

          {/* SECONDARY — savings advisories. */}
          {advisories.length > 0 && (
            <div className="border-t border-slate-700/50 pt-4">
              <div className="mb-3 flex items-center gap-2">
                <TrendingDown className="h-3.5 w-3.5 text-emerald-400" aria-hidden="true" />
                <h4 className="text-sm font-medium text-slate-200">{t('costAi.advisoriesTitle')}</h4>
              </div>
              <ul className="flex flex-col gap-3">
                {advisories.map((a, i) => (
                  <li key={i} className="flex flex-col gap-1">
                    <div className="flex flex-wrap items-baseline gap-2">
                      <span
                        className={`inline-block rounded px-2 py-0.5 text-[0.7rem] font-medium uppercase tracking-wide ${ADVISORY_BADGE[a.kind]}`}
                      >
                        {t(`costAi.advisoryKind.${a.kind}`)}
                      </span>
                      <span className="text-sm font-medium text-slate-200">{a.title}</span>
                      {a.monthly_saving && (
                        <span className="tabular-nums text-xs text-emerald-300" dir="ltr">
                          {t('costAi.saveUpTo', { amount: range(a.monthly_saving) })}
                        </span>
                      )}
                    </div>
                    <p className="text-xs leading-relaxed text-slate-400">{a.detail}</p>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* TERTIARY — narrative prose, demoted. */}
          {attrs.narrative && (
            <div className="border-t border-slate-700/50 pt-4">
              <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">
                {t('costAi.narrativeTitle')}
              </h4>
              <div className="text-sm leading-relaxed text-slate-400">
                {/* singleTilde:false — "~$X" (approximately) must not become
                    a GFM strikethrough span; cost prose uses it constantly. */}
                <ReactMarkdown
                  remarkPlugins={[[remarkGfm, { singleTilde: false }]]}
                  components={COST_MARKDOWN_COMPONENTS}
                >
                  {attrs.narrative}
                </ReactMarkdown>
              </div>
            </div>
          )}

          {attrs.translated && (
            <p className="text-xs italic text-slate-500">{tp('translatedNote')}</p>
          )}

          {/* Provenance + model footer. */}
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-700/50 pt-3 text-xs text-slate-500">
            <span>{t('costAi.estimateNote')}</span>
            {attrs.model && (
              <span className="font-mono" dir="ltr">
                {attrs.model}
              </span>
            )}
          </div>

          {/* Follow-up chat thread — grounded in the cost estimate (#871). */}
          <CostSummaryChat runId={runId} refreshKey={refreshKey} />
        </div>
      )}
    </div>
  )
}

const COST_MARKDOWN_COMPONENTS = {
  code: ({ children, ...props }: React.ComponentPropsWithoutRef<'code'>) => (
    <code {...props} className="rounded bg-slate-900 px-1 py-0.5 font-mono text-[0.7rem] text-brand-300">
      {children}
    </code>
  ),
  a: ({ children, ...props }: React.ComponentPropsWithoutRef<'a'>) => (
    <a {...props} className="text-brand-400 underline hover:text-brand-300">
      {children}
    </a>
  ),
  p: ({ children, ...props }: React.ComponentPropsWithoutRef<'p'>) => (
    <p {...props} className="my-2 leading-relaxed first:mt-0 last:mb-0">
      {children}
    </p>
  ),
}
