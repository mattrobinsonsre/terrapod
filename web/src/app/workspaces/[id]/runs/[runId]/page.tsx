'use client'

import { Suspense, useEffect, useLayoutEffect, useState, useCallback, useMemo, useRef } from 'react'
import { useRouter, useParams, useSearchParams } from 'next/navigation'
import Link from 'next/link'
import Convert from 'ansi-to-html'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { ConnectionStatus } from '@/components/connection-status'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { PlanSummaryBadges } from '@/components/plan-summary-badges'
import { PlanAiSummary } from '@/components/plan-ai-summary'
import { ResourceUsage } from '@/components/resource-usage'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { useRunEvents } from '@/lib/use-run-events'
import { ChevronsUp, ArrowDownToLine, RefreshCw } from 'lucide-react'

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
  'discard-reason': string | null
  'error-message': string | null
  'execution-backend': string
  'created-at': string
  'auto-apply': boolean
  'plan-only': boolean
  'is-destroy': boolean
  'target-addrs': string[]
  'replace-addrs': string[]
  'refresh-only': boolean
  'refresh': boolean
  'allow-empty-apply': boolean
  'vcs-commit-sha': string | null
  'vcs-branch': string | null
  'vcs-pull-request-number': number | null
  'has-changes': boolean
  'plan-summary': {
    add: number
    change: number
    destroy: number
    replace: number
    import: number
  } | null
  'workspace-name': string
  'workspace-has-vcs': boolean
  'module-overrides': Record<string, string> | null
  'status-timestamps': Record<string, string>
  'created-by': string
  'resource-cpu': string
  'resource-memory': string
  'peak-memory-bytes': number | null
  'peak-cpu-usec': number | null
  'runner-exit-code': number | null
  'runner-exit-reason': string
  'runner-exit-status': string
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

/**
 * Log viewer (#722). The log is rendered inline with **no inner scrollbar** —
 * the page itself is the scroll container, so the newest streaming lines can
 * never be trapped inside an off-screen fixed-height box (the old
 * `max-h-[600px] overflow-y-auto` bug: on a tall viewport the tail lived
 * inside a small pane below the fold, and the pane growing from "no output"
 * reflowed the page and shifted the window). Instead:
 *  - the `<pre>` grows with the content and wraps long lines (no horizontal
 *    scroll either — mobile-friendly);
 *  - "follow" pins the **window** to the tail. We scroll in `useLayoutEffect`
 *    (before paint) so an append/resize never shows a visible jump;
 *  - at-bottom is measured against the **window**, so scrolling up to read
 *    disengages follow and a floating "Jump to latest" affordance snaps back.
 */
function LogPanel({
  log,
  precomputedHtml,
  loading,
  emptyMessage,
  phase,
  runId,
  isStreaming,
  onRefresh,
}: {
  log: string | null
  precomputedHtml?: string
  loading: boolean
  emptyMessage: string
  phase: 'plan' | 'apply'
  runId: string
  isStreaming: boolean
  onRefresh?: () => void
}) {
  const [colorMode, setColorMode] = useState(true)
  // Follow the tail by default while streaming; a static (finished) log opens
  // at the top so the operator reads from the start.
  const [following, setFollowing] = useState(isStreaming)
  const [atBottom, setAtBottom] = useState(true)
  const [copied, setCopied] = useState(false)

  const cleanLog = useMemo(() => (log ? stripStxEtx(log) : null), [log])

  const htmlContent = useMemo(() => {
    if (precomputedHtml !== undefined) return precomputedHtml
    if (!cleanLog) return ''
    if (!colorMode) return ''
    return ansiConverter.toHtml(cleanLog)
  }, [cleanLog, colorMode, precomputedHtml])

  const plainContent = useMemo(() => {
    if (!cleanLog) return ''
    return stripAnsi(cleanLog)
  }, [cleanLog])

  const isWindowAtBottom = useCallback(() => {
    const doc = document.documentElement
    return window.innerHeight + window.scrollY >= doc.scrollHeight - 80
  }, [])

  const scrollWindowToBottom = useCallback((smooth = false) => {
    window.scrollTo({
      top: document.documentElement.scrollHeight,
      behavior: smooth ? 'smooth' : 'auto',
    })
  }, [])

  // Pin the window to the tail when following and the content changes. Runs in
  // useLayoutEffect (synchronously before the browser paints) so a fresh chunk
  // or the panel's own resize never flashes an intermediate scroll position.
  useLayoutEffect(() => {
    if (following) scrollWindowToBottom(false)
  }, [log, colorMode, following, scrollWindowToBottom])

  // Track the window's position so scrolling up to read disengages follow and
  // scrolling back to the bottom re-engages it.
  useEffect(() => {
    const onScroll = () => {
      const bottom = isWindowAtBottom()
      setAtBottom(bottom)
      setFollowing(bottom)
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => window.removeEventListener('scroll', onScroll)
  }, [isWindowAtBottom])

  if (loading) {
    return (
      <div className="bg-slate-900 rounded-lg border border-slate-700/50 p-6">
        <LoadingSpinner />
      </div>
    )
  }

  if (!log) {
    return (
      <div className="bg-slate-900 rounded-lg border border-slate-700/50 p-6 text-sm text-slate-500">
        {emptyMessage}
      </div>
    )
  }

  const shortId = runId.replace(/^run-/, '').split('-').pop() ?? runId

  return (
    <div className="bg-slate-900 rounded-lg border border-slate-700/50 overflow-hidden">
      <div className="flex items-center justify-between gap-2 flex-wrap px-4 py-2 border-b border-slate-700/50 bg-slate-800/50">
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
          {isStreaming && (
            <span className="inline-flex items-center gap-1.5 px-2 py-1 text-xs font-medium text-slate-400">
              <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" />
              live
            </span>
          )}
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
          {plainContent && (
            <button
              onClick={() => {
                navigator.clipboard.writeText(plainContent)
                setCopied(true)
                setTimeout(() => setCopied(false), 2000)
              }}
              className="px-2.5 py-1 text-xs rounded font-medium bg-slate-700 text-slate-400 hover:text-slate-200 transition-colors"
              title="Copy plain text to clipboard"
            >
              {copied ? 'Copied!' : 'Copy'}
            </button>
          )}
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
          data-testid={`log-pre-${phase}`}
          className="p-4 text-sm text-slate-300 font-mono whitespace-pre-wrap break-words"
          dangerouslySetInnerHTML={{ __html: htmlContent }}
        />
      ) : (
        <pre
          data-testid={`log-pre-${phase}`}
          className="p-4 text-sm text-slate-300 font-mono whitespace-pre-wrap break-words"
        >
          {plainContent}
        </pre>
      )}

      {/* Floating tail controls — fixed to the viewport so they're always
          reachable no matter how far the page has scrolled. "Jump to latest"
          appears whenever the window isn't already at the tail; it re-engages
          follow. "Top" is always offered for a long finished log. */}
      <div className="fixed bottom-6 right-6 z-20 flex flex-col items-end gap-2">
        {!atBottom && (
          <button
            onClick={() => {
              setFollowing(true)
              scrollWindowToBottom(true)
            }}
            className="px-3 py-2 rounded-full text-xs font-medium shadow-lg bg-brand-600 hover:bg-brand-500 text-white transition-colors inline-flex items-center gap-1.5"
            title="Jump to the newest output and follow it"
          >
            <ArrowDownToLine className="w-3.5 h-3.5" />
            {isStreaming ? 'Jump to latest' : 'Jump to end'}
          </button>
        )}
        {atBottom && isStreaming && (
          <span
            className="px-3 py-2 rounded-full text-xs font-medium shadow-lg bg-slate-800/90 text-slate-300 border border-slate-700 inline-flex items-center gap-1.5"
            title="Following live output"
          >
            <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" />
            Following
          </span>
        )}
        <button
          onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}
          className="px-3 py-2 rounded-full text-xs font-medium shadow-lg bg-slate-800/90 hover:bg-slate-700 text-slate-300 border border-slate-700 transition-colors inline-flex items-center gap-1.5"
          title="Jump to top"
        >
          <ChevronsUp className="w-3.5 h-3.5" />
          Top
        </button>
      </div>
    </div>
  )
}

// useSearchParams() requires a Suspense boundary or `next build` fails to
// statically analyse the route (Next 16). Matches the convention used by every
// other useSearchParams page (login, workspaces, labels, …).
export default function RunDetailPage() {
  return (
    <Suspense fallback={null}>
      <RunDetailPageInner />
    </Suspense>
  )
}

function RunDetailPageInner() {
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
  const [planHtml, setPlanHtml] = useState('')
  const [applyHtml, setApplyHtml] = useState('')
  const [planLogLoading, setPlanLogLoading] = useState(false)
  const [applyLogLoading, setApplyLogLoading] = useState(false)
  // Bumped when the SSE `plan_summary_ready` event arrives so the
  // PlanAiSummary component refetches without forcing the whole run to
  // reload.
  const [aiSummaryRefresh, setAiSummaryRefresh] = useState(0)

  // Offset tracking for incremental log fetching (byte position in raw log data)
  const planLogOffset = useRef(0)
  const applyLogOffset = useRef(0)
  // Cached log-read-url to avoid re-fetching plan/apply object each cycle
  const planLogUrl = useRef<string | null>(null)
  const applyLogUrl = useRef<string | null>(null)
  // Lock to prevent concurrent fetches from racing on offsets
  const planFetchLock = useRef(false)
  const applyFetchLock = useRef(false)

  const searchParams = useSearchParams()
  const tabParam = searchParams.get('tab')
  const [activeSection, setActiveSection] = useState<'plan' | 'apply'>(
    tabParam === 'apply' ? 'apply' : 'plan'
  )

  // Top-level view (#721): the run page is split into three focused,
  // URL-driven views instead of one long scroll. Deep-link preservation — a
  // legacy `?tab=plan|apply` link with no explicit `?view` lands on Logs (that
  // is where the plan/apply tabs used to live), so old links from the runs
  // list and the confirm-redirect keep working.
  const viewParam = searchParams.get('view')
  const [activeView, setActiveView] = useState<'overview' | 'logs' | 'details'>(
    viewParam === 'logs' || viewParam === 'details'
      ? viewParam
      : tabParam
        ? 'logs'
        : 'overview'
  )

  const switchView = useCallback((view: 'overview' | 'logs' | 'details') => {
    setActiveView(view)
    const url = new URL(window.location.href)
    url.searchParams.set('view', view)
    window.history.replaceState({}, '', url.toString())
    window.scrollTo({ top: 0 })
  }, [])

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

  // Real-time updates via SSE — reload run on status change, refresh logs on log_updated
  const { connected: sseConnected } = useRunEvents(workspaceId, useCallback((event) => {
    const bareId = runId.replace(/^run-/, '')
    if (event.event === 'reconnect' || (event.event === 'run_status_change' && event.run_id === bareId)) {
      loadRun()
    }
    if (event.event === 'log_updated' && event.run_id === bareId) {
      if (event.phase === 'plan') loadPlanLog()
      else if (event.phase === 'apply') loadApplyLog()
    }
    // Any of the summary-lifecycle events should trigger a refetch
    // (pending, ready, errored, skipped, message_posted). The
    // component handles rendering for each status.
    if (
      [
        'plan_summary_ready',
        'plan_summary_pending',
        'plan_summary_errored',
        'plan_summary_skipped',
        'plan_summary_message_posted',
      ].includes(event.event) && event.run_id === bareId
    ) {
      setAiSummaryRefresh((n) => n + 1)
    }
  }, [runId, loadRun]))

  useEffect(() => {
    if (!run) return
    const status = run.attributes.status
    if (['planning', 'planned', 'confirmed', 'applying', 'canceling', 'applied', 'errored', 'canceled', 'discarded'].includes(status)) {
      setPlanLogLoading(prev => planLog === null ? true : prev)
      loadPlanLog(true).finally(() => setPlanLogLoading(false))
    }
    if (['applying', 'canceling', 'applied', 'errored'].includes(status) && !run.attributes['plan-only']) {
      setApplyLogLoading(prev => applyLog === null ? true : prev)
      loadApplyLog(true).finally(() => setApplyLogLoading(false))
    }
  }, [run?.id, run?.attributes.status])

  // Poll the streaming phase's log on a short interval (#722). SSE
  // `log_updated` events are the primary trigger, but they can be missed
  // (dropped connection, coalesced bursts) and only fire when the server
  // relays new bytes; a lightweight incremental poll guarantees the client
  // catches up even if an event is lost. The fetch is offset-based and
  // fetch-locked, so a poll with nothing new is a cheap empty read. It only
  // runs while the relevant phase is actively streaming.
  useEffect(() => {
    const status = run?.attributes.status
    if (status !== 'planning' && status !== 'applying') return
    const handle = window.setInterval(() => {
      if (status === 'planning') loadPlanLog()
      else if (status === 'applying') loadApplyLog()
    }, 2500)
    return () => window.clearInterval(handle)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- loadPlanLog/loadApplyLog are stable function declarations
  }, [run?.attributes.status])

  async function fetchLogUrl(phase: 'plan' | 'apply'): Promise<string | null> {
    const urlRef = phase === 'plan' ? planLogUrl : applyLogUrl
    if (urlRef.current) return urlRef.current
    const endpoint = phase === 'plan' ? 'plan' : 'apply'
    const res = await apiFetch(`/api/terrapod/v1/runs/${runId}/${endpoint}`)
    if (!res.ok) return null
    const data = await res.json()
    const obj = data.data as PlanApply
    const url = obj.attributes['log-read-url']
    if (url) urlRef.current = url
    return url
  }

  async function loadPlanLog(reset = false) {
    if (planFetchLock.current) return
    planFetchLock.current = true
    try {
      if (reset) {
        planLogOffset.current = 0
        planLogUrl.current = null
      }
      const url = await fetchLogUrl('plan')
      if (!url) return
      const offset = planLogOffset.current
      const logRes = await fetch(`${url}?offset=${offset}`)
      if (!logRes.ok) return
      const buffer = await logRes.arrayBuffer()
      if (buffer.byteLength === 0) return
      const bytes = new Uint8Array(buffer)
      let dataStart = 0
      let dataEnd = bytes.length
      if (offset === 0 && bytes.length > 0 && bytes[0] === 0x02) dataStart = 1
      if (bytes.length > 0 && bytes[bytes.length - 1] === 0x03) dataEnd -= 1
      const rawDataBytes = dataEnd - dataStart
      if (rawDataBytes <= 0) return
      planLogOffset.current += rawDataBytes
      const chunk = new TextDecoder().decode(bytes.slice(dataStart, dataEnd))
      const html = ansiConverter.toHtml(chunk)
      if (reset || offset === 0) {
        setPlanLog(chunk || null)
        setPlanHtml(html)
      } else {
        setPlanLog(prev => (prev ?? '') + chunk)
        setPlanHtml(prev => prev + html)
      }
    } catch {
      // Plan log not available yet
    } finally {
      planFetchLock.current = false
    }
  }

  async function loadApplyLog(reset = false) {
    if (applyFetchLock.current) return
    applyFetchLock.current = true
    try {
      if (reset) {
        applyLogOffset.current = 0
        applyLogUrl.current = null
      }
      const url = await fetchLogUrl('apply')
      if (!url) return
      const offset = applyLogOffset.current
      const logRes = await fetch(`${url}?offset=${offset}`)
      if (!logRes.ok) return
      const buffer = await logRes.arrayBuffer()
      if (buffer.byteLength === 0) return
      const bytes = new Uint8Array(buffer)
      let dataStart = 0
      let dataEnd = bytes.length
      if (offset === 0 && bytes.length > 0 && bytes[0] === 0x02) dataStart = 1
      if (bytes.length > 0 && bytes[bytes.length - 1] === 0x03) dataEnd -= 1
      const rawDataBytes = dataEnd - dataStart
      if (rawDataBytes <= 0) return
      applyLogOffset.current += rawDataBytes
      const chunk = new TextDecoder().decode(bytes.slice(dataStart, dataEnd))
      const html = ansiConverter.toHtml(chunk)
      if (reset || offset === 0) {
        setApplyLog(chunk || null)
        setApplyHtml(html)
      } else {
        setApplyLog(prev => (prev ?? '') + chunk)
        setApplyHtml(prev => prev + html)
      }
    } catch {
      // Apply log not available yet
    } finally {
      applyFetchLock.current = false
    }
  }

  async function handleAction(action: 'confirm' | 'discard' | 'cancel' | 'retry') {
    setActionLoading(action)
    setError('')
    try {
      // TFE V2 API uses "apply" to confirm a planned run.
      // confirm / discard / cancel are on the TFE V2 CLI contract surface
      // (terraform/go-tfe call them) and live permanently at /api/v2/.
      // retry is a Terrapod extension and lives at /api/terrapod/v1/.
      const apiAction = action === 'confirm' ? 'apply' : action
      const prefix = action === 'retry' ? '/api/terrapod/v1' : '/api/v2'
      const res = await apiFetch(`${prefix}/runs/${runId}/actions/${apiAction}`, { method: 'POST' })
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
      // Confirm = applies — jump straight to the Logs view's apply tab so the
      // user watches the apply log stream instead of staying on the overview.
      if (action === 'confirm') {
        switchView('logs')
        switchSection('apply')
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
      case 'planning': case 'applying': case 'canceling': case 'queued': return 'bg-yellow-900/50 text-yellow-300'
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
            &larr; Back to {attrs['workspace-name'] || 'workspace'}
          </Link>
        </div>

        <PageHeader
          title={
            attrs['workspace-name']
              ? `${attrs['workspace-name']} — run ${run.id.replace(/^run-/, '').split('-').pop()}`
              : `Run ${run.id.replace(/^run-/, '').split('-').pop()}`
          }
          description={attrs.message || `${attrs.source} run`}
          actions={
            <div className="flex items-center gap-2">
              <ConnectionStatus connected={sseConnected} />
              {attrs['is-destroy'] && (
                <span className="inline-flex items-center px-3 py-1 rounded-full text-sm font-medium bg-red-900/50 text-red-300">
                  destroy
                </span>
              )}
              {attrs['plan-only'] && !attrs['is-destroy'] && (
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

        {/* View tabs (#721) — the run detail is split into three focused,
            URL-driven views instead of one long scroll. Overview is the
            decision surface (status, change summary, policy, actions, AI);
            Logs is the full-height streaming surface; Details holds the
            metadata / timeline / resource usage. Same components adapt to a
            drill-down on narrow viewports in the mobile pass. */}
        <div className="border-b border-slate-700/50 mb-6">
          <div className="flex gap-1 -mb-px">
            {(['overview', 'logs', 'details'] as const).map((v) => (
              <button
                key={v}
                onClick={() => switchView(v)}
                aria-current={activeView === v ? 'page' : undefined}
                className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors capitalize ${
                  activeView === v
                    ? 'border-brand-500 text-brand-400'
                    : 'border-transparent text-slate-400 hover:text-slate-200 hover:border-slate-600'
                }`}
              >
                {v}
              </button>
            ))}
          </div>
        </div>

        {activeView === 'overview' && (
        <>
        {/* Destroy run warning */}
        {attrs['is-destroy'] && (
          <div className="mb-6 p-4 bg-red-900/20 rounded-lg border border-red-800/50">
            <p className="text-sm text-red-300">
              This is a <strong>destroy</strong>{' '}run &mdash; all managed resources will be destroyed when applied.
            </p>
          </div>
        )}

        {/* Agent plan-only indicator for CLI-sourced runs on VCS-connected workspaces */}
        {attrs['plan-only'] && attrs.source === 'tfe-api' && attrs['workspace-has-vcs'] && run.relationships?.['configuration-version']?.data && (
          <div className="mb-6 p-4 bg-cyan-900/20 rounded-lg border border-cyan-800/50">
            <p className="text-sm text-cyan-300">
              This is a <strong>plan-only</strong>{' '}run initiated from the CLI on a VCS-connected workspace. Apply is not available for CLI-uploaded code &mdash; only VCS-managed code can be applied.
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

        {/* Discard reason (#646/#647): why a discarded plan can no longer apply. */}
        {attrs.status === 'discarded' && attrs['discard-reason'] && (
          <div className="mb-6 p-4 bg-amber-900/20 rounded-lg border border-amber-800/50">
            <h3 className="text-sm font-medium text-amber-400 mb-1">Discarded — re-plan required</h3>
            <p className="text-sm text-amber-200">{attrs['discard-reason']}</p>
          </div>
        )}

        {/* OPA policy evaluations (#343) — kept on Overview: a mandatory
            policy failure blocks apply and carries the admin override action,
            so it belongs with the decision controls. */}
        <PolicyPanel runId={runId} runStatus={attrs.status} onChanged={loadRun} />

        {/* No-changes notice — explains why Confirm & Apply isn't shown.
            Only relevant for plan-and-apply runs (plan-only runs simply
            report the plan and have no concept of an apply phase). */}
        {attrs['has-changes'] === false && !attrs['plan-only'] && ['planned', 'applied'].includes(attrs.status) && (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 text-sm text-slate-300">
            <span className="font-medium text-slate-100">No changes.</span>{' '}
            {attrs.status === 'applied'
              ? 'Plan reported nothing to do; the apply was skipped automatically.'
              : 'Plan reported nothing to do — there is nothing to apply.'}
          </div>
        )}

        {/* Plan summary badges — render whenever the runner has uploaded
            and parsed the JSON plan, regardless of run status. Sits
            immediately above the action row so the operator sees the
            shape of the change next to Confirm & Apply.
            Suppress when the bigger No-changes callout above will already
            render (non-plan-only runs in planned/applied with has-changes
            false) — the callout is the more explanatory surface and the
            pill would just duplicate it. */}
        {attrs['plan-summary'] &&
          !(
            attrs['has-changes'] === false &&
            !attrs['plan-only'] &&
            ['planned', 'applied'].includes(attrs.status)
          ) && <PlanSummaryBadges summary={attrs['plan-summary']} />}

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

        {/* AI plan summary / failure analysis (#401) — renders nothing
            when the feature is off or no row exists for this plan. */}
        <PlanAiSummary runId={runId.replace(/^run-/, '')} refreshKey={aiSummaryRefresh} />
        </>
        )}

        {activeView === 'details' && (
        <>
        {/* Resource usage panel (#430) — peak memory/CPU alongside the
            workspace's requested/limit, plus an OOM tag when the listener
            observed an OOMKilled / exit-137 termination. Returns null when no
            peak data is present (pre-#430 runs). */}
        <div className="mb-6">
          <ResourceUsage
            resourceMemory={attrs['resource-memory']}
            peakMemoryBytes={attrs['peak-memory-bytes']}
            runnerExitStatus={attrs['runner-exit-status']}
          />
        </div>

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
            {attrs['created-by'] && (
              <div>
                <dt className="text-xs text-slate-500">Triggered By</dt>
                <dd className="mt-1 text-sm text-slate-200">{attrs['created-by']}</dd>
              </div>
            )}
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
            {(run.relationships?.['created-state-version'] as { data: { id: string } | null } | undefined)?.data && (
              <div>
                <dt className="text-xs text-slate-500">State Version</dt>
                <dd className="mt-1 text-sm">
                  <Link href={`/workspaces/${workspaceId}?tab=state`} className="text-brand-400 hover:text-brand-300">
                    View State
                  </Link>
                </dd>
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
        </>
        )}

        {activeView === 'logs' && (
        <>
        {/* Plan / Apply sub-tabs */}
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
            precomputedHtml={planHtml}
            loading={planLogLoading}
            emptyMessage="No plan output available yet."
            phase="plan"
            runId={runId}
            isStreaming={attrs.status === 'planning'}
            onRefresh={() => loadPlanLog(true)}
          />
        )}

        {/* Apply output */}
        {activeSection === 'apply' && (
          <LogPanel
            log={applyLog}
            precomputedHtml={applyHtml}
            loading={applyLogLoading}
            emptyMessage="No apply output available yet."
            phase="apply"
            runId={runId}
            isStreaming={attrs.status === 'applying'}
            onRefresh={() => loadApplyLog(true)}
          />
        )}
        </>
        )}
      </main>
    </>
  )
}

// ── OPA policy evaluations panel (#343) ────────────────────────────────

interface PolicyEvalResult {
  policy: string
  passed: boolean
  violations: string[]
  warnings: string[]
  error: string | null
}

interface PolicyEval {
  id: string
  attributes: {
    'policy-set-name': string
    'enforcement-level': string
    outcome: string
    result: { policies?: PolicyEvalResult[]; error?: string }
    'overridden-by': string | null
  }
}

interface PolicySummary {
  status: string
  total: number
  passed: number
  failed: number
}

function outcomeBadge(outcome: string): string {
  if (outcome === 'passed') return 'bg-green-900/50 text-green-300'
  if (outcome === 'failed') return 'bg-red-900/50 text-red-300'
  return 'bg-amber-900/50 text-amber-300' // errored
}

function PolicyPanel({
  runId,
  runStatus,
  onChanged,
}: {
  runId: string
  runStatus: string
  onChanged: () => void
}) {
  const [evals, setEvals] = useState<PolicyEval[]>([])
  const [summary, setSummary] = useState<PolicySummary | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [overriding, setOverriding] = useState(false)
  const [err, setErr] = useState('')

  const load = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/terrapod/v1/runs/${runId}/policy-evaluations`)
      if (res.ok) {
        const data = await res.json()
        setEvals(data.data || [])
        setSummary(data.meta?.summary || null)
      }
    } catch {
      /* policy checks are non-critical chrome — stay quiet on failure */
    } finally {
      setLoaded(true)
    }
  }, [runId])

  useEffect(() => {
    load()
  }, [load])

  // Poll while the run is still in `planning` — that's the only window
  // where evaluations could land for the first time, an override could
  // come from another tab and unblock, or a runner re-post could land.
  // Once the run leaves `planning` (planned, applying, applied, errored,
  // cancelled, discarded), the policy state is settled and we stop —
  // otherwise a workspace with no applicable policy sets would poll
  // forever, since "no evals" looks the same as "evals not yet recorded".
  useEffect(() => {
    if (!loaded) return
    if (runStatus !== 'planning') return
    const needsPoll =
      evals.length === 0 || summary?.status === 'blocked'
    if (!needsPoll) return
    const handle = window.setInterval(load, 10_000)
    return () => window.clearInterval(handle)
  }, [loaded, runStatus, evals.length, summary?.status, load])

  if (!loaded || evals.length === 0) return null

  const blocked = summary?.status === 'blocked'

  async function override() {
    setOverriding(true)
    setErr('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/runs/${runId}/actions/override-policy`, {
        method: 'POST',
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        throw new Error(d.detail || `Override failed (${res.status})`)
      }
      await load()
      onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Override failed')
    } finally {
      setOverriding(false)
    }
  }

  return (
    <div className="mb-6 bg-slate-800/50 rounded-lg border border-slate-700/50 p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-slate-200">Policy Checks</h3>
        {summary && (
          <span className="text-xs text-slate-400">
            {summary.passed}/{summary.total} passed
          </span>
        )}
      </div>

      {blocked && (
        <div className="mb-3 p-3 bg-red-900/20 rounded-lg border border-red-800/50">
          <p className="text-sm text-red-300">
            This run is <strong>blocked by a mandatory policy set</strong>. It will not apply until
            the failure is resolved or an admin overrides it.
          </p>
          {isAdmin() && (
            <button
              onClick={override}
              disabled={overriding}
              className="mt-2 px-3 py-1.5 rounded-lg text-sm font-medium bg-red-900/60 hover:bg-red-800 disabled:opacity-50 text-red-100 transition-colors"
            >
              {overriding ? 'Overriding...' : 'Override & Continue'}
            </button>
          )}
        </div>
      )}
      {err && <p className="mb-3 text-sm text-red-400">{err}</p>}

      <div className="space-y-3">
        {evals.map((ev) => {
          const a = ev.attributes
          const policies = a.result?.policies || []
          return (
            <div key={ev.id} className="border border-slate-700/40 rounded-lg p-3">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm font-medium text-slate-200">{a['policy-set-name']}</span>
                <span
                  className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                    a['enforcement-level'] === 'mandatory'
                      ? 'bg-red-900/40 text-red-300'
                      : 'bg-amber-900/40 text-amber-300'
                  }`}
                >
                  {a['enforcement-level']}
                </span>
                <span
                  className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${outcomeBadge(a.outcome)}`}
                >
                  {a.outcome}
                </span>
                {a['overridden-by'] && (
                  <span className="text-xs text-slate-500">overridden by {a['overridden-by']}</span>
                )}
              </div>
              {a.result?.error && (
                <div className="mt-2 p-2 bg-red-900/20 rounded border border-red-800/40">
                  <p className="text-xs text-red-300 font-mono whitespace-pre-wrap">{a.result.error}</p>
                </div>
              )}
              {policies.length > 0 && (
                <div className="mt-3 space-y-2">
                  {policies.map((p) => (
                    <div key={p.policy} className="text-xs border-l-2 pl-3 py-1" style={{borderColor: p.passed ? '#4ade80' : p.error ? '#f59e0b' : '#f87171'}}>
                      <div className="flex items-center gap-2">
                        <span className={p.passed ? 'text-green-400' : p.error ? 'text-amber-400' : 'text-red-400'}>
                          {p.passed ? '✓' : p.error ? '⚠' : '✗'}
                        </span>
                        <span className="text-slate-200 font-medium">{p.policy}</span>
                        {p.passed && <span className="text-green-500">passed</span>}
                      </div>
                      {p.error && (
                        <div className="mt-1 ml-5 p-2 bg-amber-900/20 rounded border border-amber-800/40">
                          <p className="text-amber-300 font-mono whitespace-pre-wrap">{p.error}</p>
                        </div>
                      )}
                      {!p.passed && !p.error && (!p.violations || p.violations.length === 0) && (
                        <p className="mt-1 ml-5 text-slate-400 italic">Policy failed without producing a deny message or error output</p>
                      )}
                      {p.violations && p.violations.length > 0 && (
                        <ul className="mt-1 ml-5 space-y-0.5">
                          {p.violations.map((v: string, i: number) => (
                            <li key={`v${i}`} className="text-red-300 font-mono">
                              &bull; {v}
                            </li>
                          ))}
                        </ul>
                      )}
                      {p.warnings && p.warnings.length > 0 && (
                        <ul className="mt-1 ml-5 space-y-0.5">
                          {p.warnings.map((w: string, i: number) => (
                            <li key={`w${i}`} className="text-amber-300 font-mono">
                              &bull; {w}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
