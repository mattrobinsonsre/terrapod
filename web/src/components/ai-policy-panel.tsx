'use client'

// AI policy gate panel (#1766) — the model's allow/deny ruling against the
// operator's deny criteria, plus the risk threshold. Mirrors SecurityPanel and
// PolicyPanel: self-fetches, polls while the run is in `planning`, shows a
// blocked banner with an admin override, and lists what matched.
//
// Two things here differ from the other two gates and are the reason this is
// its own component rather than a variant.
//
// A verdict can be ABSENT for three different reasons, and an operator reading
// a held run needs to know which: the gate is off, the budget is spent, or the
// verdict simply has not landed yet. A mandatory gate holds the run through
// that last case, so "nothing here yet" is a state the panel has to render
// rather than hide.
//
// And `errored` BLOCKS under a mandatory gate rather than passing. The gate
// fails closed: a verdict that could not be reached is not consent. That
// inverts the usual reading of an empty finding list, so the panel never shows
// a green "passed" for a ruling that never happened.

import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { apiFetch } from '@/lib/api'
import { isAdmin } from '@/lib/auth'
import { useIsTouch } from '@/lib/use-media-query'

interface VerdictReason {
  criterion?: string
  detail?: string
}

interface AIPolicyVerdict {
  decision?: string // 'allow' | 'deny'
  reasons?: VerdictReason[]
}

interface AIPolicyAttrs {
  'enforcement-level': string
  'risk-threshold': string
  outcome: string // 'passed' | 'failed' | 'errored'
  verdict: AIPolicyVerdict
  'risk-level': string | null
  error?: string | null
  'overridden-by': string | null
  'overridden-at'?: string
}

interface AIPolicyMeta {
  'enforcement-level'?: string
  blocking?: boolean
  // Why no verdict is recorded. Absent when one is.
  'not-evaluated-reason'?: string
}

// Risk level → badge classes, matching the plan-summary's own scale so the two
// surfaces do not disagree about what "high" looks like.
function riskBadge(level?: string | null): string {
  switch ((level || '').toLowerCase()) {
    case 'critical':
      return 'bg-red-900/50 text-red-200'
    case 'high':
      return 'bg-red-900/40 text-red-300'
    case 'medium':
      return 'bg-amber-900/40 text-amber-300'
    case 'low':
      return 'bg-emerald-900/40 text-emerald-300'
    default:
      return 'bg-slate-700 text-slate-300'
  }
}

export function AIPolicyPanel({
  runId,
  runStatus,
  onChanged,
}: {
  runId: string
  runStatus: string
  onChanged: () => void
}) {
  const t = useTranslations('runDetail')
  const isTouch = useIsTouch()
  const [attrs, setAttrs] = useState<AIPolicyAttrs | null>(null)
  const [meta, setMeta] = useState<AIPolicyMeta | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [overriding, setOverriding] = useState(false)
  const [err, setErr] = useState('')

  const load = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/terrapod/v1/runs/${runId}/ai-policy`)
      if (res.ok) {
        const data = await res.json()
        setAttrs(data.data?.attributes ?? null)
        setMeta(data.meta ?? null)
      } else {
        setAttrs(null)
        setMeta(null)
      }
    } catch {
      /* a gate panel is chrome — stay quiet rather than break the run page */
    } finally {
      setLoaded(true)
    }
  }, [runId])

  useEffect(() => {
    load()
  }, [load])

  // Poll only while the run is in `planning`. Unlike the scan, a pending
  // verdict is an expected state here: a mandatory gate holds the run until the
  // summariser lands, so "no verdict yet" is exactly when polling matters.
  useEffect(() => {
    if (!loaded) return
    if (runStatus !== 'planning') return
    const needsPoll = attrs === null || meta?.blocking === true
    if (!needsPoll) return
    const handle = window.setInterval(load, 10_000)
    return () => window.clearInterval(handle)
  }, [loaded, runStatus, attrs, meta?.blocking, load])

  if (!loaded) return null

  const reason = meta?.['not-evaluated-reason']
  const blocking = meta?.blocking === true

  // No verdict recorded. Render only when that is worth explaining — a run
  // still waiting on one, or a gate holding it. A workspace with the gate off
  // stays hidden entirely rather than showing an empty panel on every run.
  if (attrs === null) {
    if (!blocking && !reason) return null
    if (reason && !blocking) {
      // The gate is off for this workspace. Say so quietly.
      return (
        <div className="mb-6 bg-slate-800/50 rounded-lg border border-slate-700/50 p-4">
          <div className="flex items-center justify-between mb-2 gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-slate-200">{t('aiPolicyPanel.heading')}</h3>
          </div>
          <p className="text-sm text-slate-400">{reason}</p>
        </div>
      )
    }
    return (
      <div className="mb-6 bg-slate-800/50 rounded-lg border border-slate-700/50 p-4">
        <div className="flex items-center justify-between mb-2 gap-2 flex-wrap">
          <h3 className="text-sm font-semibold text-slate-200">{t('aiPolicyPanel.heading')}</h3>
        </div>
        <p className="text-sm text-amber-300">{t('aiPolicyPanel.awaitingVerdict')}</p>
        {reason && <p className="mt-1 text-xs text-slate-400">{reason}</p>}
      </div>
    )
  }

  const verdict = attrs.verdict || {}
  const reasons = verdict.reasons || []
  const overriddenBy = attrs['overridden-by']
  const errored = attrs.outcome === 'errored'
  const denied = verdict.decision === 'deny'
  const riskLevel = attrs['risk-level']

  async function override() {
    if (isTouch && !window.confirm(t('aiPolicyPanel.overrideConfirm'))) return
    setOverriding(true)
    setErr('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/runs/${runId}/actions/override-ai-policy`, {
        method: 'POST',
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        throw new Error(d.detail || t('aiPolicyPanel.overrideFailedStatus', { status: res.status }))
      }
      await load()
      onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('aiPolicyPanel.overrideFailed'))
    } finally {
      setOverriding(false)
    }
  }

  return (
    <div className="mb-6 bg-slate-800/50 rounded-lg border border-slate-700/50 p-4">
      <div className="flex items-center justify-between mb-3 gap-2 flex-wrap">
        <h3 className="text-sm font-semibold text-slate-200">{t('aiPolicyPanel.heading')}</h3>
        <span className="text-xs text-slate-400">
          {t('aiPolicyPanel.enforcementLabel', { level: attrs['enforcement-level'] })}
          {attrs['risk-threshold'] && attrs['risk-threshold'] !== 'off' && (
            <>
              {' · '}
              {t('aiPolicyPanel.thresholdLabel', { threshold: attrs['risk-threshold'] })}
            </>
          )}
        </span>
      </div>

      {blocking && (
        <div className="mb-3 p-3 bg-red-900/20 rounded-lg border border-red-800/50">
          <p className="text-sm text-red-300">
            {/* Three different ways this gate blocks, and they are not
                interchangeable to someone deciding what to do next: a ruling
                against a criterion, a risk score over the threshold, or no
                usable ruling at all. */}
            {errored
              ? t('aiPolicyPanel.blockedErroredMessage')
              : denied
                ? t.rich('aiPolicyPanel.blockedDeniedMessage', {
                    count: reasons.length,
                    strong: (chunks) => <strong>{chunks}</strong>,
                  })
                : t.rich('aiPolicyPanel.blockedRiskMessage', {
                    level: riskLevel || 'unknown',
                    threshold: attrs['risk-threshold'],
                    strong: (chunks) => <strong>{chunks}</strong>,
                  })}
          </p>
          {isAdmin() && (
            <button
              onClick={override}
              disabled={overriding}
              className="mt-2 px-3 py-1.5 rounded-lg text-sm font-medium bg-red-900/60 hover:bg-red-800 disabled:opacity-50 text-red-100 transition-colors"
            >
              {overriding ? t('aiPolicyPanel.overriding') : t('aiPolicyPanel.overrideContinue')}
            </button>
          )}
        </div>
      )}
      {overriddenBy && (
        <p className="mb-3 text-xs text-slate-500">
          {t('aiPolicyPanel.overriddenBy', { by: overriddenBy })}
        </p>
      )}
      {errored && (
        <div className="mb-3 p-3 bg-amber-900/20 rounded-lg border border-amber-800/50">
          <p className="text-sm text-amber-300">{t('aiPolicyPanel.erroredMessage')}</p>
          {attrs.error && (
            <p className="mt-1 text-xs text-amber-400/80 break-words">{attrs.error}</p>
          )}
        </div>
      )}

      {!errored && (
        <div className="flex items-center gap-2 flex-wrap mb-3">
          <span
            className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
              denied ? 'bg-red-900/40 text-red-300' : 'bg-emerald-900/40 text-emerald-300'
            }`}
          >
            {denied ? t('aiPolicyPanel.decisionDeny') : t('aiPolicyPanel.decisionAllow')}
          </span>
          {riskLevel && (
            <span
              className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${riskBadge(riskLevel)}`}
            >
              {t.has(`aiPolicyPanel.risk.${riskLevel.toLowerCase()}`)
                ? t(`aiPolicyPanel.risk.${riskLevel.toLowerCase()}`)
                : riskLevel}
            </span>
          )}
        </div>
      )}

      {!errored && !denied && reasons.length === 0 && !blocking && (
        <p className="text-sm text-emerald-400">{t('aiPolicyPanel.passed')}</p>
      )}
      {err && <p className="mb-3 text-sm text-red-400">{err}</p>}

      {reasons.length > 0 && (
        <div className="space-y-2">
          {reasons.map((r, i) => (
            <div
              key={`${r.criterion}-${i}`}
              className="border border-slate-700/40 rounded-lg p-3"
            >
              {r.criterion && (
                <p className="text-sm text-slate-200 font-medium">{r.criterion}</p>
              )}
              {r.detail && <p className="mt-1 text-sm text-slate-400">{r.detail}</p>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
