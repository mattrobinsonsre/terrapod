'use client'

/**
 * AI-generated plan summary panel (#401).
 *
 * Renders one of:
 *   - "ready"    → description (markdown) + risk pill + risk factor list
 *   - "pending"  → spinner + "Summarising plan..."
 *   - "skipped"  → grey muted line ("Workspace opted out" / "Daily budget exhausted")
 *   - "errored"  → red banner with the error text
 *   - 404         → nothing renders (caller decides whether feature is on)
 *
 * Refetches on the `plan_summary_ready` SSE event — the caller is
 * responsible for hooking up `useRunEvents` and bumping the refresh
 * counter passed in via `refreshKey`.
 */

import type React from 'react'
import { useCallback, useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Sparkles, AlertTriangle, Info, ShieldAlert, ShieldX, RefreshCw } from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { LoadingSpinner } from '@/components/loading-spinner'

type Severity = 'low' | 'medium' | 'high' | 'critical' | ''

interface RiskFactor {
  severity: Severity
  title: string
  detail: string
  resource_address?: string
}

interface PlanSummary {
  id: string
  type: string
  attributes: {
    kind: 'plan_summary' | 'failure_analysis'
    status: 'pending' | 'ready' | 'skipped' | 'errored'
    description: string
    'risk-level': Severity
    'risk-factors': RiskFactor[]
    model: string
    'input-tokens': number
    'output-tokens': number
    'error-message': string
    'created-at': string
    'updated-at': string
  }
}

interface Props {
  /** Bare run UUID, no `run-` prefix. */
  runId: string
  /** Bump to force refetch (typically from SSE plan_summary_ready). */
  refreshKey?: number
}

const RISK_STYLES: Record<Severity, { pill: string; icon: typeof AlertTriangle }> = {
  '': { pill: 'bg-slate-700 text-slate-300', icon: Info },
  low: { pill: 'bg-emerald-900/40 text-emerald-300 border border-emerald-800/50', icon: Info },
  medium: { pill: 'bg-amber-900/40 text-amber-300 border border-amber-800/50', icon: AlertTriangle },
  high: { pill: 'bg-orange-900/40 text-orange-300 border border-orange-800/50', icon: ShieldAlert },
  critical: { pill: 'bg-red-900/40 text-red-300 border border-red-800/50', icon: ShieldX },
}

export function PlanAiSummary({ runId, refreshKey = 0 }: Props) {
  const [summary, setSummary] = useState<PlanSummary | null>(null)
  const [missing, setMissing] = useState(false)
  const [loading, setLoading] = useState(true)
  const [transportError, setTransportError] = useState<string | null>(null)
  const [regenerating, setRegenerating] = useState(false)
  const [regenerateError, setRegenerateError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/terrapod/v1/runs/run-${runId}/plan-summary`)
      if (res.status === 404) {
        setMissing(true)
        setSummary(null)
        return
      }
      if (!res.ok) {
        setTransportError(`HTTP ${res.status}`)
        return
      }
      const data = await res.json()
      setSummary(data.data as PlanSummary)
      setMissing(false)
      setTransportError(null)
    } catch (e) {
      setTransportError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [runId])

  useEffect(() => {
    load()
  }, [load, refreshKey])

  const regenerate = useCallback(async () => {
    // Skip confirmation modal: errored/skipped clicks are clearly an
    // operator asking for a retry; ready→regen is also unambiguous —
    // they're saying "this one wasn't good enough".
    setRegenerating(true)
    setRegenerateError(null)
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/runs/run-${runId}/plan-summary/regenerate`,
        { method: 'POST' },
      )
      if (!res.ok) {
        // Surface the API's structured detail when available.
        let detail = `HTTP ${res.status}`
        try {
          const body = await res.json()
          if (body?.detail) detail = body.detail
        } catch {
          /* fall through to status code */
        }
        setRegenerateError(detail)
        return
      }
      // 202: server upserted a pending row + enqueued. Refetch
      // immediately so the UI shows the pending state; the SSE
      // `plan_summary_ready` event drives the next refresh when the
      // handler finishes.
      const data = await res.json()
      setSummary(data.data as PlanSummary)
      setMissing(false)
    } catch (e) {
      setRegenerateError(e instanceof Error ? e.message : String(e))
    } finally {
      setRegenerating(false)
    }
  }, [runId])

  // When the feature is globally disabled (no row will ever appear) we
  // render nothing — operators on a non-AI deployment don't see this
  // panel at all.
  if (missing) return null
  if (loading && !summary) return null
  if (transportError) return null

  const attrs = summary?.attributes
  const kind = attrs?.kind ?? 'plan_summary'
  const heading = kind === 'failure_analysis' ? 'Failure analysis' : 'Plan summary'

  return (
    <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6 mb-6">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Sparkles className="w-4 h-4 text-brand-400" aria-hidden="true" />
          <h3 className="text-sm font-medium text-slate-300">{heading}</h3>
          <span className="text-xs text-slate-500">AI generated</span>
        </div>
        <div className="flex items-center gap-3">
          {attrs && attrs.status === 'ready' && attrs['risk-level'] && (
            <RiskPill level={attrs['risk-level']} />
          )}
          {/* Regenerate is gated on the summary existing — once we know
              the feature is enabled. While in `pending` we lock the
              button so concurrent clicks can't double-enqueue. */}
          {attrs && attrs.status !== 'pending' && (
            <button
              type="button"
              onClick={regenerate}
              disabled={regenerating}
              className="inline-flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 disabled:opacity-50 disabled:cursor-not-allowed px-2 py-1 rounded border border-slate-700/50 hover:border-slate-600"
              title="Re-run the AI summary against the same plan inputs"
            >
              <RefreshCw className={`w-3 h-3 ${regenerating ? 'animate-spin' : ''}`} />
              {regenerating ? 'Queueing…' : 'Regenerate'}
            </button>
          )}
        </div>
      </div>

      {regenerateError && (
        <div className="mb-3 text-xs text-red-300 bg-red-900/20 border border-red-800/50 rounded p-2">
          Could not regenerate: <span className="font-mono">{regenerateError}</span>
        </div>
      )}

      {attrs?.status === 'pending' && (
        <div className="flex items-center gap-3 text-sm text-slate-400">
          <LoadingSpinner />
          <span>
            {kind === 'failure_analysis' ? 'Analysing failure…' : 'Summarising plan…'}
          </span>
        </div>
      )}

      {attrs?.status === 'skipped' && (
        <p className="text-sm text-slate-500 italic">
          {attrs['error-message'] || 'Summary skipped for this run.'}
        </p>
      )}

      {attrs?.status === 'errored' && (
        <div className="text-sm text-red-300 bg-red-900/20 border border-red-800/50 rounded p-3">
          <div className="font-medium mb-1">Summariser failed</div>
          <div className="text-red-400/80 text-xs font-mono whitespace-pre-wrap break-all">
            {attrs['error-message']}
          </div>
        </div>
      )}

      {attrs?.status === 'ready' && (
        <>
          {/* Same xs / slate-400 baseline as risk_factor.detail so the two
              sections read as one coherent voice. Description gets a touch
              more vertical breathing room between paragraphs since it's
              usually multi-paragraph; everything else (inline code, lists,
              links) shares typography with the risk-factor detail. */}
          <div className="text-xs text-slate-400 leading-relaxed">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={SUMMARY_MARKDOWN_COMPONENTS}
            >
              {attrs.description}
            </ReactMarkdown>
          </div>

          {attrs['risk-factors'].length > 0 && (
            <div className="mt-5 pt-4 border-t border-slate-700/50">
              <h4 className="text-xs font-medium text-slate-400 mb-3">
                {kind === 'failure_analysis' ? 'Suggested fixes' : 'Risk factors'}
              </h4>
              <ul className="space-y-3">
                {attrs['risk-factors'].map((rf, idx) => (
                  <RiskFactorRow key={idx} factor={rf} />
                ))}
              </ul>
            </div>
          )}

          {attrs.model && (
            <div className="mt-4 pt-3 border-t border-slate-700/50 flex items-center justify-between text-xs text-slate-500">
              <span className="font-mono">{attrs.model}</span>
              <span>
                {attrs['input-tokens']} in / {attrs['output-tokens']} out tokens
              </span>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function RiskPill({ level }: { level: Severity }) {
  const style = RISK_STYLES[level] ?? RISK_STYLES['']
  const Icon = style.icon
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium uppercase tracking-wide ${style.pill}`}
    >
      <Icon className="w-3 h-3" aria-hidden="true" />
      {level || 'unknown'}
    </span>
  )
}

function RiskFactorRow({ factor }: { factor: RiskFactor }) {
  const style = RISK_STYLES[factor.severity] ?? RISK_STYLES['']
  const Icon = style.icon
  return (
    <li className="flex gap-3">
      <Icon
        className={`w-4 h-4 mt-0.5 flex-shrink-0 ${
          factor.severity === 'critical'
            ? 'text-red-400'
            : factor.severity === 'high'
              ? 'text-orange-400'
              : factor.severity === 'medium'
                ? 'text-amber-400'
                : 'text-emerald-400'
        }`}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2 flex-wrap">
          <span className="text-sm text-slate-200 font-medium">{factor.title}</span>
          {factor.resource_address && (
            <span className="font-mono text-xs text-brand-300">{factor.resource_address}</span>
          )}
        </div>
        {/* Match the description: ReactMarkdown so backticks render as
            inline <code>, same xs / slate-400 typography. */}
        <div className="text-xs text-slate-400 mt-1 leading-relaxed">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={SUMMARY_MARKDOWN_COMPONENTS}
          >
            {factor.detail}
          </ReactMarkdown>
        </div>
      </div>
    </li>
  )
}

// Shared markdown components — paragraphs collapse to plain `<p>` with
// no extra margin so detail blocks stay tight; description gets vertical
// spacing from a `space-y-2` on its first paragraph via the prose layout.
// Inline `<code>` uses the same monospace pill in both places.
const SUMMARY_MARKDOWN_COMPONENTS = {
  code: ({ children, ...props }: React.ComponentPropsWithoutRef<'code'>) => (
    <code
      {...props}
      className="px-1 py-0.5 rounded bg-slate-900 text-brand-300 font-mono text-[0.7rem]"
    >
      {children}
    </code>
  ),
  a: ({ children, ...props }: React.ComponentPropsWithoutRef<'a'>) => (
    <a {...props} className="text-brand-400 hover:text-brand-300 underline">
      {children}
    </a>
  ),
  ul: ({ children, ...props }: React.ComponentPropsWithoutRef<'ul'>) => (
    <ul {...props} className="list-disc list-inside space-y-1 my-1.5">
      {children}
    </ul>
  ),
  ol: ({ children, ...props }: React.ComponentPropsWithoutRef<'ol'>) => (
    <ol {...props} className="list-decimal list-inside space-y-1 my-1.5">
      {children}
    </ol>
  ),
  p: ({ children, ...props }: React.ComponentPropsWithoutRef<'p'>) => (
    <p {...props} className="my-2 first:mt-0 last:mb-0 leading-relaxed">
      {children}
    </p>
  ),
}
