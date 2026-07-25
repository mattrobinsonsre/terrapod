'use client'

// Security-scan results panel (#1036) — the deterministic Checkov/Trivy IaC
// scan surface for a run. Mirrors the OPA PolicyPanel: self-fetches the scan
// result, polls while the run is still in `planning` (the only window where a
// result can first land or an override can unblock), shows a blocked banner
// with an admin override, and lists the findings. The AI architecture critic
// (#963) renders on top of this in the same tab.

import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { apiFetch } from '@/lib/api'
import { isAdmin } from '@/lib/auth'
import { useIsTouch } from '@/lib/use-media-query'

interface SecurityFinding {
  file?: string
  line?: number
  title?: string
  engine?: string
  rule_id?: string
  resource?: string
  severity?: string
  guideline?: string
}

interface SecurityScanAttrs {
  engine: string
  'enforcement-level': string
  'severity-threshold': string
  outcome: string
  findings: SecurityFinding[]
  summary: {
    total?: number
    blocking?: number
    threshold?: string
    by_severity?: Record<string, number>
  }
  'overridden-by': string | null
  'overridden-at'?: string
}

interface SecuritySummaryMeta {
  status?: string // 'blocked' | 'passed' | ...
  outcome?: string
  engine?: string
}

// Severity → badge classes. Unrated Checkov findings are normalised to "high"
// server-side, so an unknown/empty severity is treated as high, not neutral.
function severityBadge(sev?: string): string {
  switch ((sev || 'high').toLowerCase()) {
    case 'critical':
      return 'bg-red-900/50 text-red-200'
    case 'high':
      return 'bg-red-900/40 text-red-300'
    case 'medium':
      return 'bg-amber-900/40 text-amber-300'
    case 'low':
      return 'bg-slate-700 text-slate-300'
    default:
      return 'bg-red-900/40 text-red-300'
  }
}

export function SecurityPanel({
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
  const [attrs, setAttrs] = useState<SecurityScanAttrs | null>(null)
  const [summary, setSummary] = useState<SecuritySummaryMeta | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [overriding, setOverriding] = useState(false)
  const [err, setErr] = useState('')

  const load = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/terrapod/v1/runs/${runId}/security-scan`)
      if (res.ok) {
        const data = await res.json()
        setAttrs(data.data?.attributes ?? null)
        setSummary(data.meta?.summary ?? null)
      } else {
        // 404 = no scan recorded (scanning off / not yet run) — stay hidden.
        setAttrs(null)
      }
    } catch {
      /* the scan panel is non-critical chrome — stay quiet on failure */
    } finally {
      setLoaded(true)
    }
  }, [runId])

  useEffect(() => {
    load()
  }, [load])

  // Poll only while the run is still in `planning` — the sole window where a
  // scan result first lands, a runner re-post arrives, or an override from
  // another tab unblocks. Once the run settles we stop (a scanning-off
  // workspace would otherwise poll forever, since "no result" is stable).
  useEffect(() => {
    if (!loaded) return
    if (runStatus !== 'planning') return
    const needsPoll = attrs === null || summary?.status === 'blocked'
    if (!needsPoll) return
    const handle = window.setInterval(load, 10_000)
    return () => window.clearInterval(handle)
  }, [loaded, runStatus, attrs, summary?.status, load])

  if (!loaded || attrs === null) return null

  const blocked = summary?.status === 'blocked'
  const findings = attrs.findings || []
  const overriddenBy = attrs['overridden-by']

  async function override() {
    // Overriding lets a blocking run apply anyway — irreversible enough to
    // guard. Native confirm() in touch mode (mis-tap hazard); precise pointer
    // proceeds (an explicit admin-only button click).
    if (isTouch && !window.confirm(t('securityPanel.overrideConfirm'))) return
    setOverriding(true)
    setErr('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/runs/${runId}/actions/override-security-scan`, {
        method: 'POST',
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        throw new Error(d.detail || t('securityPanel.overrideFailedStatus', { status: res.status }))
      }
      await load()
      onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('securityPanel.overrideFailed'))
    } finally {
      setOverriding(false)
    }
  }

  return (
    <div className="mb-6 bg-slate-800/50 rounded-lg border border-slate-700/50 p-4">
      <div className="flex items-center justify-between mb-3 gap-2 flex-wrap">
        <h3 className="text-sm font-semibold text-slate-200">{t('securityPanel.heading')}</h3>
        <span className="text-xs text-slate-400">
          {t('securityPanel.engineLabel', { engine: attrs.engine })}
          {' · '}
          {t('securityPanel.thresholdLabel', { threshold: attrs['severity-threshold'] })}
        </span>
      </div>

      {blocked && (
        <div className="mb-3 p-3 bg-red-900/20 rounded-lg border border-red-800/50">
          <p className="text-sm text-red-300">
            {t.rich('securityPanel.blockedMessage', {
              count: attrs.summary?.blocking ?? findings.length,
              strong: (chunks) => <strong>{chunks}</strong>,
            })}
          </p>
          {isAdmin() && (
            <button
              onClick={override}
              disabled={overriding}
              className="mt-2 px-3 py-1.5 rounded-lg text-sm font-medium bg-red-900/60 hover:bg-red-800 disabled:opacity-50 text-red-100 transition-colors"
            >
              {overriding ? t('securityPanel.overriding') : t('securityPanel.overrideContinue')}
            </button>
          )}
        </div>
      )}
      {overriddenBy && (
        <p className="mb-3 text-xs text-slate-500">
          {t('securityPanel.overriddenBy', { by: overriddenBy })}
        </p>
      )}
      {!blocked && findings.length === 0 && (
        <p className="text-sm text-emerald-400">{t('securityPanel.passed')}</p>
      )}
      {err && <p className="mb-3 text-sm text-red-400">{err}</p>}

      {findings.length > 0 && (
        <div className="space-y-2">
          {findings.map((f, i) => (
            <div
              key={`${f.rule_id}-${f.resource}-${i}`}
              className="border border-slate-700/40 rounded-lg p-3"
            >
              <div className="flex items-center gap-2 flex-wrap">
                <span
                  className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${severityBadge(f.severity)}`}
                >
                  {t.has(`securityPanel.severity.${(f.severity || 'high').toLowerCase()}`)
                    ? t(`securityPanel.severity.${(f.severity || 'high').toLowerCase()}`)
                    : f.severity || 'high'}
                </span>
                {f.rule_id && (
                  <span className="text-xs font-mono text-slate-400" dir="ltr">
                    {f.rule_id}
                  </span>
                )}
                {f.resource && (
                  <span className="text-xs font-mono text-slate-300" dir="ltr">
                    {f.resource}
                  </span>
                )}
              </div>
              {f.title && <p className="mt-1 text-sm text-slate-200">{f.title}</p>}
              {f.guideline && (
                <a
                  href={f.guideline}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-1 inline-block text-xs text-brand-400 hover:text-brand-300 underline"
                >
                  {t('securityPanel.guideline')}
                </a>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
