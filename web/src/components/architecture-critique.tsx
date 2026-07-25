'use client'

/**
 * AI architecture critic panel (#963/#1036) — the AI layer of the run Security tab.
 *
 * Renders ON TOP of the deterministic SecurityPanel, never blended into it. A
 * senior cloud/platform architect's review of the PROPOSED infrastructure: a
 * prose narrative + an overall risk level + a list of findings (each a
 * {severity, category, title, detail, resource address}). It is an advisory
 * second opinion, not a gate.
 *
 * Near-exact clone of CostAiSummary: fetch + status states
 * (pending/ready/errored/skipped) + regenerate + SSE-driven refresh via
 * `refreshKey` (the caller bumps it on the `architecture_critique_*` events).
 * Renders nothing when the feature is off (404).
 */

import type React from 'react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslations, useLocale } from 'next-intl'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Sparkles, RefreshCw, ShieldAlert, ShieldX, AlertTriangle, Info } from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { useIsTouch } from '@/lib/use-media-query'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ArchitectureCritiqueChat } from '@/components/architecture-critique-chat'

type RiskLevel = 'critical' | 'high' | 'medium' | 'low' | 'none'
type FindingSeverity = 'critical' | 'high' | 'medium' | 'low' | 'info'
type FindingCategory =
  | 'security'
  | 'reliability'
  | 'cost'
  | 'operations'
  | 'scalability'
  | 'other'

interface Finding {
  severity: FindingSeverity
  category: FindingCategory
  title: string
  detail: string
  address?: string
}

interface Critique {
  id: string
  type: string
  attributes: {
    status: 'pending' | 'ready' | 'errored' | 'skipped'
    critique: string
    'risk-level': RiskLevel
    findings: Finding[]
    model: string
    'input-tokens': number
    'output-tokens': number
    'error-message': string
    /** Canonical language the prose is stored in. */
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
  /** Bump to force refetch (from SSE architecture_critique_* events). */
  refreshKey?: number
}

// Risk-level pill styling mirrors the plan summary's risk colours (#767).
const RISK_STYLES: Record<RiskLevel, { pill: string; icon: typeof AlertTriangle }> = {
  none: { pill: 'bg-slate-700 text-slate-300', icon: Info },
  low: { pill: 'bg-emerald-900/40 text-emerald-300 border border-emerald-800/50', icon: Info },
  medium: { pill: 'bg-amber-900/40 text-amber-300 border border-amber-800/50', icon: AlertTriangle },
  high: { pill: 'bg-orange-900/40 text-orange-300 border border-orange-800/50', icon: ShieldAlert },
  critical: { pill: 'bg-red-900/40 text-red-300 border border-red-800/50', icon: ShieldX },
}

// Finding severity badge styling — the same colour scheme as the risk pill.
const SEVERITY_BADGE: Record<FindingSeverity, string> = {
  critical: 'bg-red-900/40 text-red-300',
  high: 'bg-orange-900/40 text-orange-300',
  medium: 'bg-amber-900/40 text-amber-300',
  low: 'bg-emerald-900/40 text-emerald-300',
  info: 'bg-slate-700 text-slate-300',
}

export function ArchitectureCritique({ runId, refreshKey = 0 }: Props) {
  const t = useTranslations('runDetail')
  // "Translating…" + the translated note are the same concept as the plan
  // summary; reuse its already-translated strings rather than duplicate keys.
  const tp = useTranslations('planSummary')
  const locale = useLocale()
  const [critique, setCritique] = useState<Critique | null>(null)
  const [missing, setMissing] = useState(false)
  const [loading, setLoading] = useState(true)
  const [transportError, setTransportError] = useState<string | null>(null)
  const [regenerating, setRegenerating] = useState(false)
  const [regenerateError, setRegenerateError] = useState<string | null>(null)
  // True while a locale change re-fetches the translated view — keep the
  // previous (stale-language) content on screen under a spinner.
  const [translating, setTranslating] = useState(false)
  const hasContentRef = useRef(false)
  const isTouch = useIsTouch()

  const load = useCallback(async () => {
    // A re-fetch while we already have content means the reader switched
    // language — show the translating spinner and keep the old content visible.
    if (hasContentRef.current) setTranslating(true)
    try {
      // Pass the reader's locale so the API translates the canonical-language
      // prose on view. Re-fetches when the locale changes (load is keyed on it).
      const res = await apiFetch(
        `/api/terrapod/v1/runs/run-${runId}/architecture-critique?locale=${encodeURIComponent(locale)}`,
      )
      if (res.status === 404) {
        setMissing(true)
        setCritique(null)
        hasContentRef.current = false
        return
      }
      if (!res.ok) {
        setTransportError(t('architectureCritique.errors.status', { status: res.status }))
        return
      }
      const data = await res.json()
      setCritique(data.data as Critique)
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
    if (isTouch && !window.confirm(t('architectureCritique.regenerate.confirm'))) return
    setRegenerating(true)
    setRegenerateError(null)
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/runs/run-${runId}/architecture-critique/regenerate`,
        { method: 'POST' },
      )
      if (!res.ok) {
        let detail = t('architectureCritique.errors.status', { status: res.status })
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
      setCritique(data.data as Critique)
      setMissing(false)
    } catch (e) {
      setRegenerateError(e instanceof Error ? e.message : String(e))
    } finally {
      setRegenerating(false)
    }
  }, [runId, isTouch, t])

  // Feature off (no row ever) → render nothing at all.
  if (missing) return null
  if (loading && !critique) return null
  if (transportError) return null

  const attrs = critique?.attributes
  const findings = attrs?.findings ?? []
  const riskLevel = attrs?.['risk-level']

  return (
    <div className="rounded-xl border border-brand-800/40 bg-brand-950/10 p-4 sm:p-6">
      {/* Header — ✨ signals AI; the panel is an advisory review, so we say so. */}
      <div className="mb-4 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-brand-400" aria-hidden="true" />
          <h3 className="text-sm font-medium text-slate-200">{t('architectureCritique.heading')}</h3>
          <span className="hidden text-xs text-slate-500 md:inline">
            {t('architectureCritique.aiGenerated')}
          </span>
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
            title={t('architectureCritique.regenerate.tooltip')}
            aria-label={t('architectureCritique.regenerate.ariaLabel')}
          >
            <RefreshCw className={`h-3 w-3 ${regenerating ? 'animate-spin' : ''}`} aria-hidden="true" />
            <span className="hidden md:inline">
              {regenerating
                ? t('architectureCritique.regenerate.queueing')
                : t('architectureCritique.regenerate.label')}
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
          <span>{t('architectureCritique.pending')}</span>
        </div>
      )}

      {attrs?.status === 'skipped' && (
        <p className="text-sm italic text-slate-500">
          {attrs['error-message'] || t('architectureCritique.skipped')}
        </p>
      )}

      {attrs?.status === 'errored' && (
        <div className="rounded border border-red-800/50 bg-red-900/20 p-3 text-sm text-red-300">
          <div className="mb-1 font-medium">{t('architectureCritique.errored')}</div>
          <div className="whitespace-pre-wrap break-all font-mono text-xs text-red-400/80">
            {attrs['error-message']}
          </div>
        </div>
      )}

      {attrs?.status === 'ready' && (
        <div className="flex flex-col gap-5">
          {/* Overall risk level pill. */}
          {riskLevel && (
            <div className="flex items-center gap-2">
              <span className="text-xs uppercase tracking-wide text-slate-500">
                {t('architectureCritique.riskLabel')}
              </span>
              <RiskPill level={riskLevel} label={t(`architectureCritique.risk.${riskLevel}`)} />
            </div>
          )}

          {/* PRIMARY — the prose narrative. */}
          {attrs.critique && (
            <div className="text-sm leading-relaxed text-slate-300">
              <ReactMarkdown
                remarkPlugins={[[remarkGfm, { singleTilde: false }]]}
                components={CRITIQUE_MARKDOWN_COMPONENTS}
              >
                {attrs.critique}
              </ReactMarkdown>
            </div>
          )}

          {/* Findings list. */}
          {findings.length > 0 ? (
            <div className="border-t border-slate-700/50 pt-4">
              <h4 className="mb-3 text-sm font-medium text-slate-200">
                {t('architectureCritique.findingsTitle', { count: findings.length })}
              </h4>
              <ul className="flex flex-col gap-3">
                {findings.map((f, i) => (
                  <li key={i} className="flex flex-col gap-1">
                    <div className="flex flex-wrap items-baseline gap-2">
                      <span
                        className={`inline-block rounded px-2 py-0.5 text-[0.7rem] font-medium uppercase tracking-wide ${SEVERITY_BADGE[f.severity]}`}
                      >
                        {t(`architectureCritique.severity.${f.severity}`)}
                      </span>
                      <span className="text-[0.7rem] uppercase tracking-wide text-slate-500">
                        {t(`architectureCritique.category.${f.category}`)}
                      </span>
                      <span className="text-sm font-medium text-slate-200" dir="ltr">
                        {f.title}
                      </span>
                    </div>
                    {f.detail && (
                      <p className="text-xs leading-relaxed text-slate-400" dir="ltr">
                        {f.detail}
                      </p>
                    )}
                    {f.address && (
                      <span className="break-all font-mono text-xs text-brand-300" dir="ltr">
                        {f.address}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="text-sm text-slate-500">{t('architectureCritique.noFindings')}</p>
          )}

          {attrs.translated && <p className="text-xs italic text-slate-500">{tp('translatedNote')}</p>}

          {/* Provenance + model footer. */}
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-700/50 pt-3 text-xs text-slate-500">
            <span>{t('architectureCritique.reviewNote')}</span>
            {attrs.model && (
              <span className="font-mono" dir="ltr">
                {attrs.model}
              </span>
            )}
          </div>

          {/* Follow-up chat thread — grounded in the architecture review. */}
          <ArchitectureCritiqueChat runId={runId} refreshKey={refreshKey} />
        </div>
      )}
    </div>
  )
}

function RiskPill({ level, label }: { level: RiskLevel; label: string }) {
  const style = RISK_STYLES[level] ?? RISK_STYLES.none
  const Icon = style.icon
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs font-medium uppercase tracking-wide ${style.pill}`}
    >
      <Icon className="h-3 w-3" aria-hidden="true" />
      {label}
    </span>
  )
}

const CRITIQUE_MARKDOWN_COMPONENTS = {
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
