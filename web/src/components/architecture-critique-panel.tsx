'use client'

/**
 * Architecture Critique panel (#1036 Part 2) — the workspace "Architecture" tab.
 *
 * Styled to match the run AI tab (plan-ai-summary): a Sparkles-headed card with
 * a risk pill, severity-iconed findings carrying category chips + resource
 * addresses + markdown detail, a model/token footer, and a follow-up chat.
 *
 * Unlike a run's plan summary (which reviews a change), this reviews the
 * workspace's deployed system AS IT EXISTS, inferred from its latest state and
 * grounded in the deterministic scan + cost engine + resource graph.
 */

import type React from 'react'
import { useCallback, useEffect, useState } from 'react'
import { useTranslations, useLocale } from 'next-intl'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Sparkles,
  RefreshCw,
  Info,
  AlertTriangle,
  ShieldAlert,
  ShieldX,
  Boxes,
} from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { useIsTouch } from '@/lib/use-media-query'
import { ArchitectureCritiqueChat } from '@/components/architecture-critique-chat'

type Severity = 'low' | 'medium' | 'high' | 'critical'
type Category = 'reliability' | 'security' | 'cost' | 'operations' | 'scalability'

interface Architecture {
  summary?: string
  tiers?: string[]
  data_stores?: string[]
  blast_radius?: string
}
interface Finding {
  severity: Severity
  category: Category
  title: string
  detail: string
  resource_address: string
  recommendation?: string
  grounded_in?: string
}
interface Critique {
  status: 'ready' | 'pending' | 'skipped' | 'errored'
  'risk-level'?: Severity
  architecture?: Architecture
  findings?: Finding[]
  deferred?: string[]
  'state-serial'?: number
  'error-message'?: string
  translated?: boolean
  model?: string
  'input-tokens'?: number
  'output-tokens'?: number
}

const RISK_STYLES: Record<Severity, { pill: string; icon: typeof AlertTriangle; text: string }> = {
  low: {
    pill: 'bg-emerald-900/40 text-emerald-300 border border-emerald-800/50',
    icon: Info,
    text: 'text-emerald-400',
  },
  medium: {
    pill: 'bg-amber-900/40 text-amber-300 border border-amber-800/50',
    icon: AlertTriangle,
    text: 'text-amber-400',
  },
  high: {
    pill: 'bg-orange-900/40 text-orange-300 border border-orange-800/50',
    icon: ShieldAlert,
    text: 'text-orange-400',
  },
  critical: {
    pill: 'bg-red-900/40 text-red-300 border border-red-800/50',
    icon: ShieldX,
    text: 'text-red-400',
  },
}

// Neutral dimension chips (matching plan-ai-summary) — severity is the icon +
// pill's job; the category just says which lens flagged it.
const CATEGORY_STYLES: Record<Category, string> = {
  reliability: 'bg-sky-900/40 text-sky-300 border border-sky-800/50',
  security: 'bg-purple-900/40 text-purple-300 border border-purple-800/50',
  cost: 'bg-teal-900/40 text-teal-300 border border-teal-800/50',
  operations: 'bg-indigo-900/40 text-indigo-300 border border-indigo-800/50',
  scalability: 'bg-cyan-900/40 text-cyan-300 border border-cyan-800/50',
}
const SEVERITY_RANK: Record<Severity, number> = { critical: 0, high: 1, medium: 2, low: 3 }

export function ArchitectureCritiquePanel({
  workspaceId,
  refreshKey = 0,
}: {
  workspaceId: string
  refreshKey?: number
}) {
  const t = useTranslations('runDetail')
  const locale = useLocale()
  const isTouch = useIsTouch()
  const [critique, setCritique] = useState<Critique | null>(null)
  const [absent, setAbsent] = useState(false)
  const [disabled, setDisabled] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [regenerating, setRegenerating] = useState(false)

  const endpoint = `/api/terrapod/v1/workspaces/${workspaceId}/architecture-critique`

  useEffect(() => {
    let cancelled = false
    setError(null)
    // Pass the reader's locale so a ready critique's prose is translated on view
    // (#767); re-fetches when the locale changes because the effect keys on it.
    apiFetch(`${endpoint}?locale=${encodeURIComponent(locale)}`)
      .then(async (res) => {
        if (res.status === 404) {
          let notEnabled = false
          try {
            const b = await res.json()
            notEnabled = /not enabled/i.test(b?.detail ?? '')
          } catch {
            /* no body */
          }
          if (!cancelled) {
            setDisabled(notEnabled)
            setAbsent(!notEnabled)
            setCritique(null)
          }
          return
        }
        if (!res.ok) throw new Error(t('architecture.loadError'))
        const body = await res.json()
        if (!cancelled) {
          setAbsent(false)
          setDisabled(false)
          setCritique(body.data.attributes as Critique)
        }
      })
      .catch((e: Error) => !cancelled && setError(e.message))
    return () => {
      cancelled = true
    }
  }, [endpoint, refreshKey, locale, t])

  const regenerate = useCallback(async () => {
    if (isTouch && !window.confirm(t('architecture.regenerateConfirm'))) return
    setRegenerating(true)
    setError(null)
    try {
      const res = await apiFetch(`${endpoint}/regenerate`, { method: 'POST' })
      if (!res.ok) throw new Error(t('architecture.regenerateError'))
      setAbsent(false)
      setCritique((c) => ({ ...(c ?? {}), status: 'pending' }) as Critique)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setRegenerating(false)
    }
  }, [endpoint, isTouch, t])

  if (error) return <ErrorBanner message={error} />
  if (disabled) {
    return (
      <div className="rounded-xl border border-slate-700 bg-slate-800/30 p-6 text-sm text-slate-400">
        {t('architecture.notEnabled')}
      </div>
    )
  }
  if (!critique && !absent) return <LoadingSpinner />

  const RegenerateButton = ({ label }: { label: string }) => (
    <button
      type="button"
      onClick={regenerate}
      disabled={regenerating}
      className="inline-flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 disabled:opacity-50 disabled:cursor-not-allowed px-2 py-1 rounded border border-slate-700/50 hover:border-slate-600"
    >
      <RefreshCw className={`w-3 h-3 ${regenerating ? 'animate-spin' : ''}`} />
      {regenerating ? t('architecture.regenerating') : label}
    </button>
  )

  const Shell = ({ children }: { children: React.ReactNode }) => (
    <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Sparkles className="w-4 h-4 text-brand-400" aria-hidden="true" />
          <h3 className="text-sm font-medium text-slate-300">{t('architecture.heading')}</h3>
          <span className="text-xs text-slate-500 hidden md:inline">{t('architecture.aiGenerated')}</span>
        </div>
        <div className="flex items-center gap-3">
          {critique?.status === 'ready' && critique['risk-level'] && (
            <RiskPill level={critique['risk-level']} />
          )}
          {critique?.status !== 'pending' && (
            <RegenerateButton label={t('architecture.regenerate')} />
          )}
        </div>
      </div>
      {children}
    </div>
  )

  if (absent) {
    return (
      <Shell>
        <p className="text-sm text-slate-400">{t('architecture.absent')}</p>
        <div className="mt-3">
          <RegenerateButton label={t('architecture.generate')} />
        </div>
      </Shell>
    )
  }

  const c = critique!

  if (c.status === 'pending') {
    return (
      <Shell>
        <div className="flex items-center gap-3 text-sm text-slate-400">
          <LoadingSpinner />
          <span>{t('architecture.pending')}</span>
        </div>
      </Shell>
    )
  }
  if (c.status === 'errored') {
    return (
      <Shell>
        <div className="text-sm text-red-300 bg-red-900/20 border border-red-800/50 rounded p-3">
          <div className="font-medium mb-1">{t('architecture.errored')}</div>
          <div className="text-red-400/80 text-xs font-mono whitespace-pre-wrap break-all">
            {c['error-message']}
          </div>
        </div>
      </Shell>
    )
  }
  if (c.status === 'skipped') {
    return (
      <Shell>
        <p className="text-sm text-slate-500 italic">{t('architecture.skipped')}</p>
      </Shell>
    )
  }

  // ready
  const arch = c.architecture ?? {}
  const findings = [...(c.findings ?? [])].sort(
    (a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity],
  )
  const deferred = c.deferred ?? []

  return (
    <Shell>
      {typeof c['state-serial'] === 'number' && (
        <div className="mb-3 text-xs text-slate-500">
          {t('architecture.asOfState', { serial: c['state-serial'] })}
        </div>
      )}

      {/* Inferred architecture — the "what is this system" prose. */}
      {arch.summary && (
        <div className="rounded-lg border border-slate-700/50 bg-slate-900/30 p-4">
          <div className="flex items-center gap-1.5 text-xs font-medium text-slate-400 mb-2">
            <Boxes className="w-3.5 h-3.5" aria-hidden="true" />
            {t('architecture.systemTitle')}
          </div>
          <div className="text-sm text-slate-300 leading-relaxed">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD}>
              {arch.summary}
            </ReactMarkdown>
          </div>
          {(arch.tiers?.length || arch.data_stores?.length || arch.blast_radius) && (
            <dl className="mt-3 space-y-2 text-xs">
              {arch.tiers?.length ? <MetaRow label={t('architecture.tiers')} items={arch.tiers} /> : null}
              {arch.data_stores?.length ? (
                <MetaRow label={t('architecture.dataStores')} items={arch.data_stores} />
              ) : null}
              {arch.blast_radius ? (
                <div>
                  <dt className="text-slate-400 font-medium">{t('architecture.blastRadius')}</dt>
                  <dd className="text-slate-300 mt-0.5">{arch.blast_radius}</dd>
                </div>
              ) : null}
            </dl>
          )}
        </div>
      )}

      {/* Findings — flat, severity-ranked, with category chips (matching the run AI tab). */}
      <div className="mt-5 pt-4 border-t border-slate-700/50">
        <h4 className="text-xs font-medium text-slate-400 mb-3">{t('architecture.findingsHeading')}</h4>
        {findings.length === 0 ? (
          <div className="rounded border border-emerald-800/40 bg-emerald-900/15 p-3 text-sm text-emerald-300">
            {t('architecture.noFindings')}
          </div>
        ) : (
          <ul className="space-y-3">
            {findings.map((f, i) => (
              <FindingRow key={`${f.resource_address}-${i}`} f={f} />
            ))}
          </ul>
        )}
      </div>

      {/* Deferred — the honest "couldn't judge this" gaps. */}
      {deferred.length > 0 && (
        <div className="mt-5 pt-4 border-t border-slate-700/50">
          <h4 className="text-xs font-medium text-slate-400 mb-1">{t('architecture.deferredTitle')}</h4>
          <p className="text-[0.7rem] text-slate-500 mb-2">{t('architecture.deferredHint')}</p>
          <ul className="space-y-1">
            {deferred.map((d, i) => (
              <li key={i} className="flex items-start gap-1.5 text-xs text-slate-400">
                <span aria-hidden>•</span>
                <span>{d}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {c.model && (
        <div className="mt-4 pt-3 border-t border-slate-700/50 flex items-center justify-between text-xs text-slate-500">
          <span className="font-mono">{c.model}</span>
          <span>
            {t('architecture.tokens', { input: c['input-tokens'] ?? 0, output: c['output-tokens'] ?? 0 })}
          </span>
        </div>
      )}

      {c.translated && (
        <p className="mt-3 text-xs text-slate-500 italic">{t('architecture.translatedNote')}</p>
      )}

      {/* Provenance */}
      <p className="mt-3 text-xs text-slate-500">{t('architecture.provenance')}</p>

      {/* Follow-up chat — only against a ready critique. */}
      <ArchitectureCritiqueChat workspaceId={workspaceId} refreshKey={refreshKey} />
    </Shell>
  )
}

function RiskPill({ level }: { level: Severity }) {
  const t = useTranslations('runDetail')
  const style = RISK_STYLES[level]
  const Icon = style.icon
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium uppercase tracking-wide ${style.pill}`}
    >
      <Icon className="w-3 h-3" aria-hidden="true" />
      {t(`architecture.severity.${level}`)}
    </span>
  )
}

function MetaRow({ label, items }: { label: string; items: string[] }) {
  return (
    <div>
      <dt className="text-slate-400 font-medium">{label}</dt>
      <dd className="mt-0.5">
        <ul className="space-y-0.5 text-slate-300">
          {items.map((x, i) => (
            <li key={i}>{x}</li>
          ))}
        </ul>
      </dd>
    </div>
  )
}

function FindingRow({ f }: { f: Finding }) {
  const t = useTranslations('runDetail')
  const style = RISK_STYLES[f.severity]
  const Icon = style.icon
  return (
    <li className="flex gap-3">
      <Icon className={`w-4 h-4 mt-0.5 flex-shrink-0 ${style.text}`} aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2 flex-wrap">
          <span
            className={`inline-flex items-center px-1.5 py-0.5 rounded text-[0.65rem] font-medium uppercase tracking-wide ${CATEGORY_STYLES[f.category]}`}
          >
            {t(`architecture.category.${f.category}`)}
          </span>
          <span className="text-sm text-slate-200 font-medium">{f.title}</span>
          {f.resource_address && (
            <span className="font-mono text-xs text-brand-300" dir="ltr">
              {f.resource_address}
            </span>
          )}
        </div>
        <div className="text-xs text-slate-400 mt-1 leading-relaxed">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD}>
            {f.detail}
          </ReactMarkdown>
        </div>
        {f.recommendation && (
          <p className="mt-1.5 text-xs text-brand-300 leading-relaxed">
            <span className="font-medium">{t('architecture.recommendation')}:</span> {f.recommendation}
          </p>
        )}
        {f.grounded_in && (
          <p className="mt-1 text-[0.65rem] text-slate-500">
            {t('architecture.groundedIn', { source: f.grounded_in })}
          </p>
        )}
      </div>
    </li>
  )
}

const MD = {
  code: ({ children, ...props }: React.ComponentPropsWithoutRef<'code'>) => (
    <code {...props} className="px-1 py-0.5 rounded bg-slate-900 text-brand-300 font-mono text-[0.7rem]">
      {children}
    </code>
  ),
  p: ({ children, ...props }: React.ComponentPropsWithoutRef<'p'>) => (
    <p {...props} className="my-1.5 first:mt-0 last:mb-0 leading-relaxed">
      {children}
    </p>
  ),
  ul: ({ children, ...props }: React.ComponentPropsWithoutRef<'ul'>) => (
    <ul {...props} className="list-disc list-inside space-y-1 my-1.5">
      {children}
    </ul>
  ),
}
