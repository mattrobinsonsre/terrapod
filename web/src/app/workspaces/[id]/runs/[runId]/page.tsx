'use client'

import { useEffect, useState, useCallback, useMemo, useRef } from 'react'
import { useRouter, useParams, useSearchParams } from 'next/navigation'
import Link from 'next/link'
import Convert from 'ansi-to-html'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { getAuthState } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { useRunEvents } from '@/lib/use-run-events'
import { ChevronsDown, ChevronsUp, ArrowDownToLine, RefreshCw } from 'lucide-react'

interface RunActions {
  'is-confirmable': boolean
  'is-discardable': boolean
  'is-cancelable': boolean
  'is-retryable': boolean
}

interface RunAttrs {
  status: string
  source: string
  message: string
  'error-message': string | null
  'execution-backend': string
  'created-at': string
  'auto-apply': boolean
  'plan-only': boolean
  'target-addrs': string[]
  'replace-addrs': string[]
  'refresh-only': boolean
  'refresh': boolean
  'allow-empty-apply': boolean
  'vcs-commit-sha': string | null
  'vcs-branch': string | null
  'vcs-pull-request-number': number | null
  'module-overrides': Record<string, string> | null
  'status-timestamps': Record<string, string>
  actions: RunActions
  permissions: Record<string, boolean>
}

interface Run {
  id: string
  attributes: RunAttrs
  relationships?: {
    'configuration-version'?: { data: { id: string; type: string } | null }
    [key: string]: unknown
  }
}

interface PlanApply {
  id: string
  attributes: {
    status: string
    'log-read-url': string | null
  }
}

const ansiConverter = new Convert({
  fg: '#cbd5e1',
  bg: 'transparent',
  escapeXML: true,
})

function stripAnsi(text: string): string {
  // eslint-disable-next-line no-control-regex
  return text.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '')
}

function stripStxEtx(text: string): string {
  return text.replace(/[\x02\x03]/g, '')
}

function downloadFile(content: string, filename: string) {
  const blob = new Blob([content], { type: 'text/plain' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

function LogPanel({
  log,
  loading,
  emptyMessage,
  phase,
  runId,
  isStreaming,
  onRefresh,
}: {
  log: string | null
  loading: boolean
  emptyMessage: string
  phase: 'plan' | 'apply'
  runId: string
  isStreaming: boolean
  onRefresh?: () => void
}) {
  const [colorMode, setColorMode] = useState(true)
  const [following, setFollowing] = useState(true)
  const preRef = useRef<HTMLPreElement>(null)

  const cleanLog = useMemo(() => (log ? stripStxEtx(log) : null), [log])

  const htmlContent = useMemo(() => {
    if (!cleanLog) return ''
    if (!colorMode) return ''
    return ansiConverter.toHtml(cleanLog)
  }, [cleanLog, colorMode])

  const plainContent = useMemo(() => {
    if (!cleanLog) return ''
    return stripAnsi(cleanLog)
  }, [cleanLog])

  // Auto-scroll to bottom when following and content changes
  useEffect(() => {
    if (following && preRef.current) {
      preRef.current.scrollTop = preRef.current.scrollHeight
    }
  }, [log, following])

  const handleScroll = useCallback(() => {
    const el = preRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
    setFollowing(atBottom)
  }, [])

  const scrollToTop = useCallback(() => {
    preRef.current?.scrollTo({ top: 0, behavior: 'smooth' })
  }, [])

  const scrollToEnd = useCallback(() => {
    if (preRef.current) {
      preRef.current.scrollTo({ top: preRef.current.scrollHeight, behavior: 'smooth' })
    }
  }, [])

  if (loading) {
    return (
      <div className="bg-slate-900 rounded-lg border border-slate-700/50 overflow-hidden">
        <div className="p-6"><LoadingSpinner /></div>
      </div>
    )
  }

  if (!log) {
    return (
      <div className="bg-slate-900 rounded-lg border border-slate-700/50 overflow-hidden">
        <div className="p-6 text-sm text-slate-500">{emptyMessage}</div>
      </div>
    )
  }

  const shortId = runId.replace(/^run-/, '').split('-').pop() ?? runId

  return (
    <div className="bg-slate-900 rounded-lg border border-slate-700/50 overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2 border-b border-slate-700/50 bg-slate-800/50">
        <div className="flex items-center gap-2">
          <button
            onClick={() => setColorMode(true)}
            className={`px-2.5 py-1 text-xs rounded font-medium transition-colors ${
              colorMode
                ? 'bg-brand-600 text-white'
                : 'bg-slate-700 text-slate-400 hover:text-slate-200'
            }`}
          >
            Color
          </button>
          <button
            onClick={() => setColorMode(false)}
            className={`px-2.5 py-1 text-xs rounded font-medium transition-colors ${
              !colorMode
                ? 'bg-brand-600 text-white'
                : 'bg-slate-700 text-slate-400 hover:text-slate-200'
            }`}
          >
            Plain
          </button>
        </div>
        <div className="flex items-center gap-2">
          {onRefresh && (
            <button
              onClick={onRefresh}
              className="px-2.5 py-1 text-xs rounded font-medium bg-slate-700 text-slate-400 hover:text-slate-200 transition-colors inline-flex items-center gap-1"
              title="Refresh log"
            >
              <RefreshCw className="w-3 h-3" />
              Refresh
            </button>
          )}
          {isStreaming && (
            <button
              onClick={() => {
                setFollowing(f => !f)
                if (!following && preRef.current) {
                  preRef.current.scrollTop = preRef.current.scrollHeight
                }
              }}
              className={`px-2.5 py-1 text-xs rounded font-medium transition-colors inline-flex items-center gap-1 ${
                following
                  ? 'bg-brand-600 text-white'
                  : 'bg-slate-700 text-slate-400 hover:text-slate-200'
              }`}
              title={following ? 'Following output — click to stop' : 'Click to follow output'}
            >
              <ArrowDownToLine className="w-3 h-3" />
              Follow
            </button>
          )}
          <button
            onClick={scrollToEnd}
            className="px-2.5 py-1 text-xs rounded font-medium bg-slate-700 text-slate-400 hover:text-slate-200 transition-colors inline-flex items-center gap-1"
            title="Jump to end"
          >
            <ChevronsDown className="w-3 h-3" />
            End
          </button>
          <button
            onClick={scrollToTop}
            className="px-2.5 py-1 text-xs rounded font-medium bg-slate-700 text-slate-400 hover:text-slate-200 transition-colors inline-flex items-center gap-1"
            title="Jump to top"
          >
            <ChevronsUp className="w-3 h-3" />
            Top
          </button>
          <button
            onClick={() => downloadFile(cleanLog!, `${shortId}-${phase}.log`)}
            className="px-2.5 py-1 text-xs rounded font-medium bg-slate-700 text-slate-400 hover:text-slate-200 transition-colors"
            title="Download with ANSI color codes"
          >
            Download colored
          </button>
          <button
            onClick={() => downloadFile(plainContent, `${shortId}-${phase}-plain.log`)}
            className="px-2.5 py-1 text-xs rounded font-medium bg-slate-700 text-slate-400 hover:text-slate-200 transition-colors"
            title="Download plain text (no color codes)"
          >
            Download plain
          </button>
        </div>
      </div>

      {colorMode ? (
        <pre
          ref={preRef}
          onScroll={handleScroll}
          className="p-4 text-sm text-slate-300 font-mono overflow-x-auto whitespace-pre-wrap max-h-[600px] overflow-y-auto"
          dangerouslySetInnerHTML={{ __html: htmlContent }}
        />
      ) : (
        <pre
          ref={preRef}
          onScroll={handleScroll}
          className="p-4 text-sm text-slate-300 font-mono overflow-x-auto whitespace-pre-wrap max-h-[600px] overflow-y-auto"
        >
          {plainContent}
        </pre>
      )}
    </div>
  )
}

export default function RunDetailPage() {
  const router = useRouter()
  const params = useParams()
  const workspaceId = params.id as string
  const runId = params.runId as string

  const [run, setRun] = useState<Run | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [actionLoading, setActionLoading] = useState('')

  const [planLog, setPlanLog] = useState<string | null>(null)
  const [applyLog, setApplyLog] = useState<string | null>(null)
  const [planLogLoading, setPlanLogLoading] = useState(false)
  const [applyLogLoading, setApplyLogLoading] = useState(false)

  const searchParams = useSearchParams()
  const tabParam = searchParams.get('tab')
  const [activeSection, setActiveSection] = useState<'plan' | 'apply'>(
    tabParam === 'apply' ? 'apply' : 'plan'
  )

  const switchSection = useCallback((section: 'plan' | 'apply') => {
    setActiveSection(section)
    const url = new URL(window.location.href)
    url.searchParams.set('tab', section)
    window.history.replaceState({}, '', url.toString())
  }, [])

  const loadRun = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/v2/runs/${runId}`)
      if (!res.ok) throw new Error('Failed to load run')
      const data = await res.json()
      setRun(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load run')
    } finally {
      setLoading(false)
    }
  }, [runId])

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadRun()
  }, [router, loadRun])

  // Real-time updates via SSE — reload on any workspace event for this run
  useRunEvents(workspaceId, useCallback((event) => {
    // Reload on run_status_change for this run, or on reconnect (catch-up)
    const bareId = runId.replace(/^run-/, '')
    if (event.event === 'reconnect' || event.run_id === bareId) {
      loadRun()
    }
  }, [runId, loadRun]))

  useEffect(() => {
    if (!run) return
    const status = run.attributes.status
    if (['planning', 'planned', 'confirmed', 'applying', 'applied', 'errored', 'canceled', 'discarded'].includes(status)) {
      loadPlanLog()
    }
    if (['applying', 'applied', 'errored'].includes(status) && !run.attributes['plan-only']) {
      loadApplyLog()
    }
  }, [run?.id, run?.attributes.status])

  async function loadPlanLog() {
    setPlanLogLoading(true)
    try {
      const res = await apiFetch(`/api/v2/runs/${runId}/plan`)
      if (!res.ok) { setPlanLogLoading(false); return }
      const data = await res.json()
      const plan = data.data as PlanApply
      if (plan.attributes['log-read-url']) {
        const logRes = await fetch(plan.attributes['log-read-url'])
        if (logRes.ok) {
          setPlanLog(await logRes.text())
        }
      }
    } catch {
      // Plan log not available yet
    } finally {
      setPlanLogLoading(false)
    }
  }

  async function loadApplyLog() {
    setApplyLogLoading(true)
    try {
      const res = await apiFetch(`/api/v2/runs/${runId}/apply`)
      if (!res.ok) { setApplyLogLoading(false); return }
      const data = await res.json()
      const apply = data.data as PlanApply
      if (apply.attributes['log-read-url']) {
        const logRes = await fetch(apply.attributes['log-read-url'])
        if (logRes.ok) {
          setApplyLog(await logRes.text())
        }
      }
    } catch {
      // Apply log not available yet
    } finally {
      setApplyLogLoading(false)
    }
  }

  async function handleAction(action: 'confirm' | 'discard' | 'cancel' | 'retry') {
    setActionLoading(action)
    setError('')
    try {
      const res = await apiFetch(`/api/v2/runs/${runId}/actions/${action}`, { method: 'POST' })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to ${action} run`)
      }
      if (action === 'retry') {
        const data = await res.json()
        const newRunId = data?.data?.id
        if (newRunId) {
          router.push(`/workspaces/${workspaceId}/runs/${newRunId}`)
          return
        }
      }
      await loadRun()
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${action} run`)
    } finally {
      setActionLoading('')
    }
  }

  function statusColor(status: string): string {
    switch (status) {
      case 'applied': return 'bg-green-900/50 text-green-300'
      case 'planned': case 'confirmed': return 'bg-blue-900/50 text-blue-300'
      case 'planning': case 'applying': case 'queued': return 'bg-yellow-900/50 text-yellow-300'
      case 'errored': return 'bg-red-900/50 text-red-300'
      case 'canceled': case 'discarded': return 'bg-slate-700 text-slate-400'
      case 'pending': return 'bg-slate-700 text-slate-300'
      default: return 'bg-slate-700 text-slate-400'
    }
  }

  function formatTimestamp(iso: string | undefined): string {
    if (!iso) return '-'
    return new Date(iso).toLocaleString()
  }

  if (loading) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>
  if (!run) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><ErrorBanner message="Run not found" /></main></>

  const attrs = run.attributes
  const actions = attrs.actions
  const timestamps = attrs['status-timestamps'] || {}

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <div className="mb-4">
          <Link href={`/workspaces/${workspaceId}?tab=runs`} className="text-sm text-slate-400 hover:text-slate-200">
            &larr; Back to workspace
          </Link>
        </div>

        <PageHeader
          title={`Run ${run.id.replace(/^run-/, '').split('-').pop()}`}
          description={attrs.message || `${attrs.source} run`}
          actions={
            <div className="flex items-center gap-2">
              {attrs['plan-only'] && (
                <span className="inline-flex items-center px-3 py-1 rounded-full text-sm font-medium bg-cyan-900/50 text-cyan-300">
                  plan only
                </span>
              )}
              <span className={`inline-flex items-center px-3 py-1 rounded-full text-sm font-medium ${statusColor(attrs.status)}`}>
                {attrs.status}
              </span>
            </div>
          }
        />

        {error && <ErrorBanner message={error} />}

        {/* Remote plan-only indicator for CLI-sourced runs (has uploaded config version) */}
        {attrs['plan-only'] && attrs.source === 'tfe-api' && run.relationships?.['configuration-version']?.data && (
          <div className="mb-6 p-4 bg-cyan-900/20 rounded-lg border border-cyan-800/50">
            <p className="text-sm text-cyan-300">
              This is a <strong>plan-only</strong> remote run initiated from the CLI. Apply is not available for CLI-uploaded code &mdash; only VCS-managed code can be applied.
            </p>
          </div>
        )}

        {/* Module override banner for module-test runs */}
        {attrs['module-overrides'] && (
          <div className="mb-6 p-4 bg-purple-900/20 rounded-lg border border-purple-800/50">
            <p className="text-sm text-purple-300">
              This run uses <strong>module overrides</strong>
              {attrs['vcs-pull-request-number'] && <> from PR #{attrs['vcs-pull-request-number']}</>}
              {' '}&mdash; {Object.keys(attrs['module-overrides']).map(coord => (
                <code key={coord} className="bg-purple-900/50 px-1.5 py-0.5 rounded text-xs font-mono">{coord}</code>
              ))}
              {' '}will be fetched from the PR branch instead of the published registry version.
            </p>
          </div>
        )}

        {/* Error message */}
        {attrs['error-message'] && (
          <div className="mb-6 p-4 bg-red-900/20 rounded-lg border border-red-800/50">
            <h3 className="text-sm font-medium text-red-400 mb-1">Error</h3>
            <pre className="text-sm text-red-300 whitespace-pre-wrap font-mono">{attrs['error-message']}</pre>
          </div>
        )}

        {/* Action buttons */}
        {(actions['is-confirmable'] || actions['is-discardable'] || actions['is-cancelable'] || actions['is-retryable']) && (
          <div className="flex gap-3 mb-6">
            {actions['is-retryable'] && (
              <button
                onClick={() => handleAction('retry')}
                disabled={!!actionLoading}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
              >
                {actionLoading === 'retry' ? 'Retrying...' : 'Retry Run'}
              </button>
            )}
            {actions['is-confirmable'] && (
              <button
                onClick={() => handleAction('confirm')}
                disabled={!!actionLoading}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-green-600 hover:bg-green-500 disabled:bg-green-800 disabled:text-green-400 text-white transition-colors"
              >
                {actionLoading === 'confirm' ? 'Confirming...' : 'Confirm & Apply'}
              </button>
            )}
            {actions['is-discardable'] && (
              <button
                onClick={() => handleAction('discard')}
                disabled={!!actionLoading}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-slate-600 hover:bg-slate-500 disabled:bg-slate-700 disabled:text-slate-400 text-white transition-colors"
              >
                {actionLoading === 'discard' ? 'Discarding...' : 'Discard'}
              </button>
            )}
            {actions['is-cancelable'] && (
              <button
                onClick={() => handleAction('cancel')}
                disabled={!!actionLoading}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-500 disabled:bg-red-800 disabled:text-red-400 text-white transition-colors"
              >
                {actionLoading === 'cancel' ? 'Canceling...' : 'Cancel Run'}
              </button>
            )}
          </div>
        )}

        {/* Run metadata */}
        <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6 mb-6">
          <h3 className="text-sm font-medium text-slate-300 mb-4">Details</h3>
          <dl className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-4">
            <div>
              <dt className="text-xs text-slate-500">Execution Backend</dt>
              <dd className="mt-1 text-sm text-slate-200">{attrs['execution-backend'] === 'terraform' ? 'Terraform' : 'OpenTofu'}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Source</dt>
              <dd className="mt-1 text-sm text-slate-200">{attrs.source}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Auto Apply</dt>
              <dd className="mt-1 text-sm text-slate-200">{attrs['auto-apply'] ? 'Yes' : 'No'}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Plan Only</dt>
              <dd className="mt-1 text-sm text-slate-200">{attrs['plan-only'] ? 'Yes' : 'No'}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Created</dt>
              <dd className="mt-1 text-sm text-slate-200">{formatTimestamp(attrs['created-at'])}</dd>
            </div>
            {attrs['vcs-commit-sha'] && (
              <div>
                <dt className="text-xs text-slate-500">Commit</dt>
                <dd className="mt-1 text-sm text-slate-200 font-mono">{attrs['vcs-commit-sha'].slice(0, 8)}</dd>
              </div>
            )}
            {attrs['vcs-branch'] && (
              <div>
                <dt className="text-xs text-slate-500">Branch</dt>
                <dd className="mt-1 text-sm text-slate-200">{attrs['vcs-branch']}</dd>
              </div>
            )}
            {attrs['vcs-pull-request-number'] && (
              <div>
                <dt className="text-xs text-slate-500">PR/MR</dt>
                <dd className="mt-1 text-sm text-slate-200">#{attrs['vcs-pull-request-number']}</dd>
              </div>
            )}
          </dl>

          {/* Run options (only show when non-default) */}
          {(attrs['target-addrs']?.length > 0 || attrs['replace-addrs']?.length > 0 || attrs['refresh-only'] || !attrs['refresh'] || attrs['allow-empty-apply']) && (
            <div className="mt-4 pt-4 border-t border-slate-700/50">
              <h4 className="text-xs text-slate-500 mb-2">Run Options</h4>
              <div className="flex flex-wrap gap-2">
                {attrs['target-addrs']?.map((addr: string) => (
                  <span key={addr} className="inline-flex items-center px-2 py-0.5 rounded text-xs font-mono bg-amber-900/40 text-amber-300 border border-amber-800/50">
                    -target={addr}
                  </span>
                ))}
                {attrs['replace-addrs']?.map((addr: string) => (
                  <span key={addr} className="inline-flex items-center px-2 py-0.5 rounded text-xs font-mono bg-orange-900/40 text-orange-300 border border-orange-800/50">
                    -replace={addr}
                  </span>
                ))}
                {attrs['refresh-only'] && (
                  <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-mono bg-purple-900/40 text-purple-300 border border-purple-800/50">
                    -refresh-only
                  </span>
                )}
                {!attrs['refresh'] && (
                  <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-mono bg-slate-700 text-slate-300 border border-slate-600">
                    -refresh=false
                  </span>
                )}
                {attrs['allow-empty-apply'] && (
                  <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-mono bg-teal-900/40 text-teal-300 border border-teal-800/50">
                    -allow-empty-apply
                  </span>
                )}
              </div>
            </div>
          )}
        </div>

        {/* Status timeline */}
        <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6 mb-6">
          <h3 className="text-sm font-medium text-slate-300 mb-4">Timeline</h3>
          <div className="space-y-2">
            {[
              ['queued-at', 'Queued'],
              ['planning-at', 'Planning started'],
              ['planned-at', 'Plan complete'],
              ['confirmed-at', 'Confirmed'],
              ['applying-at', 'Applying started'],
              ['applied-at', 'Applied'],
              ['errored-at', 'Errored'],
              ['canceled-at', 'Canceled'],
              ['discarded-at', 'Discarded'],
            ]
              .filter(([key]) => timestamps[key as string])
              .map(([key, label]) => (
                <div key={key} className="flex items-center gap-3">
                  <div className="w-2 h-2 rounded-full bg-brand-500 flex-shrink-0" />
                  <span className="text-sm text-slate-300 w-36">{label}</span>
                  <span className="text-xs text-slate-500 font-mono">{formatTimestamp(timestamps[key as string])}</span>
                </div>
              ))}
          </div>
        </div>

        {/* Log tabs */}
        <div className="border-b border-slate-700/50 mb-4">
          <div className="flex gap-1 -mb-px">
            <button
              onClick={() => switchSection('plan')}
              className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                activeSection === 'plan'
                  ? 'border-brand-500 text-brand-400'
                  : 'border-transparent text-slate-400 hover:text-slate-200 hover:border-slate-600'
              }`}
            >
              Plan Output
            </button>
            {!attrs['plan-only'] && (
              <button
                onClick={() => switchSection('apply')}
                className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                  activeSection === 'apply'
                    ? 'border-brand-500 text-brand-400'
                    : 'border-transparent text-slate-400 hover:text-slate-200 hover:border-slate-600'
                }`}
              >
                Apply Output
              </button>
            )}
          </div>
        </div>

        {/* Plan output */}
        {activeSection === 'plan' && (
          <LogPanel
            log={planLog}
            loading={planLogLoading}
            emptyMessage="No plan output available yet."
            phase="plan"
            runId={runId}
            isStreaming={attrs.status === 'planning'}
            onRefresh={loadPlanLog}
          />
        )}

        {/* Apply output */}
        {activeSection === 'apply' && (
          <LogPanel
            log={applyLog}
            loading={applyLogLoading}
            emptyMessage="No apply output available yet."
            phase="apply"
            runId={runId}
            isStreaming={attrs.status === 'applying'}
            onRefresh={loadApplyLog}
          />
        )}
      </main>
    </>
  )
}
