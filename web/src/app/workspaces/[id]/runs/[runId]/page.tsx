'use client'

import { Suspense, useEffect, useLayoutEffect, useState, useCallback, useMemo, useRef } from 'react'
import { useRouter, useParams, useSearchParams } from 'next/navigation'
import { useTranslations } from 'next-intl'
import dynamic from 'next/dynamic'
import Link from 'next/link'
import Convert from 'ansi-to-html'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { ConnectionStatus } from '@/components/connection-status'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { PlanAiSummary } from '@/components/plan-ai-summary'
import { ResourceUsage, parseMemoryToBytes, humanBytes } from '@/components/resource-usage'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { useRunEvents } from '@/lib/use-run-events'
import { useIsTouch } from '@/lib/use-media-query'
import { ArrowDownToLine, RefreshCw, Download, Copy, Check, Palette } from 'lucide-react'

// WebGL (three.js) — client-only, never SSR'd. Loaded on demand (#761).
const ImpactGraph = dynamic(() => import('@/components/impact-graph').then((m) => m.ImpactGraph), {
  ssr: false,
  loading: () => <LoadingSpinner />,
})

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
  'has-json-output'?: boolean
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

// Top-level run views (#721). Overview summarises the run; each aspect with
// more to show has its own tab — AI analysis and OPA policy only appear when
// the run actually has them. Details holds the metadata / timeline / run
// options / resource usage.
type RunView = 'overview' | 'ai' | 'opa' | 'impact' | 'plan' | 'apply' | 'details'

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

function fmtDuration(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const rs = s % 60
  if (m < 60) return `${m}m ${rs}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

function relTime(iso: string, now: number, t: ReturnType<typeof useTranslations>): string {
  const ts = Date.parse(iso)
  if (Number.isNaN(ts)) return ''
  const s = Math.floor((now - ts) / 1000)
  if (s < 60) return t('relTime.justNow')
  const m = Math.floor(s / 60)
  if (m < 60) return t('relTime.minutesAgo', { count: m })
  const h = Math.floor(m / 60)
  if (h < 24) return t('relTime.hoursAgo', { count: h })
  const d = Math.floor(h / 24)
  return t('relTime.daysAgo', { count: d })
}

// The "at a glance" strip at the top of Overview (#721) — the run's current
// state and what it's doing right now. Live phases pulse and show a ticking
// elapsed timer; terminal states show how long ago they finished.
function RunActivityHeader({
  status,
  timestamps,
  planOnly,
  isConfirmable,
}: {
  status: string
  timestamps: Record<string, string>
  planOnly: boolean
  isConfirmable: boolean
}) {
  const t = useTranslations('runDetail')
  const live = ['pending', 'queued', 'planning', 'confirmed', 'applying', 'canceling'].includes(status)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!live) return
    const h = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(h)
  }, [live])

  type Info = { label: string; activity: string; dot: string; card: string; sinceKey?: string }
  const map: Record<string, Info> = {
    pending: { label: t('status.pending'), activity: t('activity.pending'), dot: 'bg-slate-400', card: 'border-slate-700/50 bg-slate-800/40' },
    queued: { label: t('status.queued'), activity: t('activity.queued'), dot: 'bg-yellow-400', card: 'border-yellow-800/40 bg-yellow-900/10', sinceKey: 'queued-at' },
    planning: { label: t('status.planning'), activity: t('activity.planning'), dot: 'bg-yellow-400', card: 'border-yellow-800/40 bg-yellow-900/10', sinceKey: 'planning-at' },
    planned: {
      label: t('status.planned'),
      activity: isConfirmable ? t('activity.plannedConfirmable') : planOnly ? t('activity.plannedSpeculative') : t('activity.plannedComplete'),
      dot: 'bg-blue-400',
      card: isConfirmable ? 'border-blue-800/40 bg-blue-900/10' : 'border-slate-700/50 bg-slate-800/40',
      sinceKey: 'planned-at',
    },
    confirmed: { label: t('status.confirmed'), activity: t('activity.confirmed'), dot: 'bg-blue-400', card: 'border-blue-800/40 bg-blue-900/10', sinceKey: 'confirmed-at' },
    applying: { label: t('status.applying'), activity: t('activity.applying'), dot: 'bg-yellow-400', card: 'border-yellow-800/40 bg-yellow-900/10', sinceKey: 'applying-at' },
    canceling: { label: t('status.canceling'), activity: t('activity.canceling'), dot: 'bg-yellow-400', card: 'border-yellow-800/40 bg-yellow-900/10' },
    applied: { label: t('status.applied'), activity: t('activity.applied'), dot: 'bg-green-400', card: 'border-green-800/40 bg-green-900/10', sinceKey: 'applied-at' },
    errored: { label: t('status.errored'), activity: t('activity.errored'), dot: 'bg-red-400', card: 'border-red-800/40 bg-red-900/10', sinceKey: 'errored-at' },
    canceled: { label: t('status.canceled'), activity: t('activity.canceled'), dot: 'bg-slate-400', card: 'border-slate-700/50 bg-slate-800/40', sinceKey: 'canceled-at' },
    discarded: { label: t('status.discarded'), activity: t('activity.discarded'), dot: 'bg-slate-400', card: 'border-slate-700/50 bg-slate-800/40', sinceKey: 'discarded-at' },
  }
  const info = map[status] ?? { label: status, activity: '', dot: 'bg-slate-400', card: 'border-slate-700/50 bg-slate-800/40' }
  const sinceTs = info.sinceKey ? timestamps[info.sinceKey] : undefined
  const sinceMs = sinceTs ? Date.parse(sinceTs) : NaN
  const elapsed = !Number.isNaN(sinceMs) ? now - sinceMs : undefined

  return (
    <div className={`mb-6 rounded-lg border p-4 flex items-center gap-3 ${info.card}`}>
      <span className="relative flex h-3 w-3 flex-shrink-0">
        {live && (
          <span className={`animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 ${info.dot}`} />
        )}
        <span className={`relative inline-flex rounded-full h-3 w-3 ${info.dot}`} />
      </span>
      <div className="min-w-0 flex-1">
        <span className="text-sm font-semibold text-slate-100">{info.label}</span>
        {info.activity && <span className="text-sm text-slate-400"> — {info.activity}</span>}
      </div>
      {live && elapsed !== undefined && (
        <span className="text-xs text-slate-400 tabular-nums flex-shrink-0" title={t('activity.elapsedTitle')}>
          {fmtDuration(elapsed)}
        </span>
      )}
      {!live && sinceTs && (
        <span className="text-xs text-slate-500 flex-shrink-0">{relTime(sinceTs, now, t)}</span>
      )}
    </div>
  )
}

type CardTone = 'neutral' | 'good' | 'warn' | 'bad' | 'active'

const CARD_TONE: Record<CardTone, string> = {
  neutral: 'text-slate-300',
  good: 'text-green-300',
  warn: 'text-amber-300',
  bad: 'text-red-300',
  active: 'text-blue-300',
}

// One at-a-glance summary card on Overview. When `onClick` is set the whole
// card is a button that drills into the matching tab (#721).
function SummaryCard({
  label,
  value,
  sub,
  tone = 'neutral',
  onClick,
}: {
  label: string
  value: React.ReactNode
  sub?: string
  tone?: CardTone
  onClick?: () => void
}) {
  const base = 'rounded-lg border border-slate-700/50 bg-slate-800/40 p-4 text-left w-full'
  const body = (
    <>
      <div className="text-xs uppercase tracking-wider text-slate-500 mb-1 flex items-center justify-between">
        <span>{label}</span>
        {onClick && <span className="text-slate-600" aria-hidden="true">›</span>}
      </div>
      <div className={`text-sm font-semibold ${CARD_TONE[tone]}`}>{value}</div>
      {sub && <div className="text-xs text-slate-500 mt-0.5">{sub}</div>}
    </>
  )
  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className={`${base} transition-colors hover:bg-slate-700/50 hover:border-slate-600 focus:outline-none focus:ring-2 focus:ring-brand-500`}
      >
        {body}
      </button>
    )
  }
  return <div className={base}>{body}</div>
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
  // Scroll model is viewport-driven (#722, #719): on desktop the log is a
  // normal inner-scroll pane (the familiar CI-log behaviour — it scrolls
  // independently of the page); on a phone a nested scroll region is a touch
  // trap, so the pane just expands and the *page* is the scroll container.
  // Same component; the scroll target branches on POINTER, not width — a nested
  // scroll region is a touch trap regardless of how wide the screen is, so a
  // touch tablet/foldable page-scrolls too, while a mouse (any width) keeps the
  // inner pane. Mirrors the CSS `fine:` overflow on the <pre> below.
  const t = useTranslations('runDetail')
  const isTouch = useIsTouch()
  const [colorMode, setColorMode] = useState(true)
  // Follow the tail by default while streaming; a static (finished) log opens
  // at the top so the operator reads from the start.
  const [following, setFollowing] = useState(isStreaming)
  const [copied, setCopied] = useState(false)
  const preRef = useRef<HTMLPreElement>(null)

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

  const isAtBottom = useCallback(() => {
    if (isTouch) {
      const doc = document.documentElement
      return window.innerHeight + window.scrollY >= doc.scrollHeight - 80
    }
    const el = preRef.current
    if (!el) return true
    return el.scrollHeight - el.scrollTop - el.clientHeight < 60
  }, [isTouch])

  const scrollToBottom = useCallback(
    (smooth = false) => {
      const opts: ScrollToOptions = { behavior: smooth ? 'smooth' : 'auto' }
      if (isTouch) window.scrollTo({ top: document.documentElement.scrollHeight, ...opts })
      else preRef.current?.scrollTo({ top: preRef.current.scrollHeight, ...opts })
    },
    [isTouch],
  )

  // Pin the active scroller to the tail when following and the content changes.
  // Runs in useLayoutEffect (synchronously before paint) so a fresh chunk or
  // the panel's own resize never flashes an intermediate scroll position.
  useLayoutEffect(() => {
    if (following) scrollToBottom(false)
  }, [log, colorMode, following, scrollToBottom])

  // Track the scroller's position so scrolling up to read disengages follow and
  // scrolling back to the bottom re-engages it. The listener is on the window
  // (touch — the page scrolls) or the inner pane (mouse).
  useEffect(() => {
    const onScroll = () => {
      setFollowing(isAtBottom())
    }
    const el = isTouch ? window : preRef.current
    if (!el) return
    el.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => el.removeEventListener('scroll', onScroll)
  }, [isTouch, isAtBottom])

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
          {/* Single Color toggle (checkbox-style): on = ANSI colours, off =
              plain text. Consolidated from the old two-button Color/Plain pair
              to free a slot on a phone-width toolbar row. */}
          <button
            onClick={() => setColorMode((c) => !c)}
            aria-pressed={colorMode}
            className={`px-2 py-1 text-xs rounded font-medium transition-colors inline-flex items-center gap-1 ${
              colorMode
                ? 'bg-brand-600 text-white'
                : 'bg-slate-700 text-slate-400 hover:text-slate-200'
            }`}
            title={colorMode ? t('log.colorOnTitle') : t('log.colorOffTitle')}
          >
            <Palette className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">{t('log.color')}</span>
          </button>
        </div>
        {/* Two groups: utility icons (refresh/copy/download — icon-only on a
            phone) and, separated by a divider, the scroll-nav controls
            (Follow/End). End keeps its text label at every width — an unlabelled
            down-arrow reads as a second Download next to the real one. */}
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1.5">
            {onRefresh && (
              <button
                onClick={onRefresh}
                className="px-2 py-1 text-xs rounded font-medium bg-slate-700 text-slate-400 hover:text-slate-200 transition-colors inline-flex items-center gap-1"
                title={t('log.refreshTitle')}
                aria-label={t('log.refreshTitle')}
              >
                <RefreshCw className="w-3.5 h-3.5" />
                <span className="hidden sm:inline">{t('log.refresh')}</span>
              </button>
            )}
            {plainContent && (
              <button
                onClick={() => {
                  navigator.clipboard.writeText(plainContent)
                  setCopied(true)
                  setTimeout(() => setCopied(false), 2000)
                }}
                className="px-2 py-1 text-xs rounded font-medium bg-slate-700 text-slate-400 hover:text-slate-200 transition-colors inline-flex items-center gap-1"
                title={t('log.copyTitle')}
                aria-label={t('log.copyAria')}
              >
                {copied ? <Check className="w-3.5 h-3.5 text-green-400" /> : <Copy className="w-3.5 h-3.5" />}
                <span className="hidden sm:inline">{copied ? t('log.copied') : t('log.copy')}</span>
              </button>
            )}
            {/* One download button — the current Color/Plain mode decides what
                it saves: colored (ANSI codes preserved) in Color mode, stripped
                plain text in Plain mode. */}
            <button
              onClick={() =>
                colorMode
                  ? downloadFile(cleanLog ?? '', `${shortId}-${phase}.log`)
                  : downloadFile(plainContent, `${shortId}-${phase}-plain.log`)
              }
              className="px-2 py-1 text-xs rounded font-medium bg-slate-700 text-slate-400 hover:text-slate-200 transition-colors inline-flex items-center gap-1"
              title={colorMode ? t('log.downloadColorTitle') : t('log.downloadPlainTitle')}
              aria-label={t('log.downloadAria')}
            >
              <Download className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">{t('log.download')}</span>
            </button>
          </div>
          {/* Scroll-nav group, divider-separated from the utilities. Both keep
              their text label at all widths so they never read as another icon. */}
          <div className="flex items-center gap-1.5 border-l border-slate-700/60 pl-2">
            {/* Follow appears only while the log is streaming: a persistent
                auto-tail toggle. Green when engaged; the scroll listener also
                flips it as the operator scrolls up/down. */}
            {isStreaming && (
              <button
                onClick={() => {
                  const next = !following
                  setFollowing(next)
                  if (next) scrollToBottom(true)
                }}
                aria-pressed={following}
                className={`px-2 py-1 text-xs rounded font-medium transition-colors inline-flex items-center gap-1 ${
                  following
                    ? 'bg-green-600/20 text-green-300'
                    : 'bg-slate-700 text-slate-400 hover:text-slate-200'
                }`}
                title={following ? t('log.followOnTitle') : t('log.followOffTitle')}
              >
                <span className={`w-1.5 h-1.5 rounded-full bg-green-400 ${following ? 'animate-pulse' : ''}`} />
                {t('log.follow')}
              </button>
            )}
            {/* "End" jumps to the tail (one-shot). Distinct from Follow: End is a
                single scroll-to-bottom, always available; Follow is the streaming
                auto-tail mode. Phones have a built-in scroll-to-top, so no Top. */}
            <button
              onClick={() => scrollToBottom(true)}
              className="px-2 py-1 text-xs rounded font-medium bg-slate-700 text-slate-400 hover:text-slate-200 transition-colors inline-flex items-center gap-1"
              title={t('log.endTitle')}
            >
              <ArrowDownToLine className="w-3.5 h-3.5" />
              {t('log.end')}
            </button>
          </div>
        </div>
      </div>

      {/* With a precise pointer (`fine:`) the pane is a bounded inner-scroll
          region — it scrolls independently of the page, the familiar CI-log
          behaviour. On a touch device it has no max-height and no inner
          overflow, so it expands and the *page* scrolls (no nested-scroll touch
          trap) — regardless of width, so a wide touch tablet is safe too. The
          `fine:` CSS variant matches `useIsTouch()`, so the CSS scroller and
          the JS tail-follow logic agree. */}
      {colorMode ? (
        <pre
          ref={preRef}
          data-testid={`log-pre-${phase}`}
          className="p-4 text-sm text-slate-300 font-mono whitespace-pre-wrap break-words fine:max-h-[70vh] fine:overflow-y-auto"
          dangerouslySetInnerHTML={{ __html: htmlContent }}
        />
      ) : (
        <pre
          ref={preRef}
          data-testid={`log-pre-${phase}`}
          className="p-4 text-sm text-slate-300 font-mono whitespace-pre-wrap break-words fine:max-h-[70vh] fine:overflow-y-auto"
        >
          {plainContent}
        </pre>
      )}

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
  const t = useTranslations('runDetail')
  const router = useRouter()
  const params = useParams()
  const workspaceId = params.id as string
  const runId = params.runId as string

  const isTouch = useIsTouch()
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

  // Lightweight status of the AI analysis + policy checks, used by the
  // Overview summary cards and to decide whether the AI / OPA tabs appear.
  // The full panels in those tabs self-fetch as before; these are just the
  // at-a-glance rollups. `present:false` means the run has no such data (AI
  // disabled / no policy sets), so the corresponding tab is hidden.
  const [aiInfo, setAiInfo] = useState<{ present: boolean; status?: string; risk?: string } | null>(null)
  const [policyInfo, setPolicyInfo] = useState<
    { present: boolean; status?: string; passed?: number; total?: number; failed?: number } | null
  >(null)

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

  // Top-level view (#721): this is a faithful split — everything that was on
  // the single-scroll run page stays on the default "Overview" view exactly as
  // before (banners, policy, plan-summary, AI, actions, details, timeline,
  // resource usage); ONLY the plan and apply logs move to their own top-level
  // tabs (Plan Log / Apply Log). Nothing that was visible becomes hidden.
  // Deep-link preservation: a legacy `?tab=plan|apply` link (and the
  // transitional `?view=logs`/`?view=details`) with no explicit log view maps
  // onto the matching tab, so old links from the runs list and the
  // confirm-redirect keep working.
  const viewParam = searchParams.get('view')
  const KNOWN_VIEWS: RunView[] = ['overview', 'ai', 'opa', 'impact', 'plan', 'apply', 'details']
  const initialView: RunView = KNOWN_VIEWS.includes(viewParam as RunView)
    ? (viewParam as RunView)
    : viewParam === 'logs' || tabParam === 'plan'
      ? 'plan'
      : tabParam === 'apply'
        ? 'apply'
        : 'overview'
  const [activeView, setActiveView] = useState<RunView>(initialView)

  const switchView = useCallback((view: RunView) => {
    setActiveView(view)
    const url = new URL(window.location.href)
    url.searchParams.set('view', view)
    url.searchParams.delete('tab') // superseded by `view`
    window.history.replaceState({}, '', url.toString())
    window.scrollTo({ top: 0 })
  }, [])

  const loadRun = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/v2/runs/${runId}`)
      if (!res.ok) throw new Error(t('errors.loadRun'))
      const data = await res.json()
      setRun(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadRun'))
    } finally {
      setLoading(false)
    }
  }, [runId, t])

  // Overview rollups for AI + policy (see aiInfo/policyInfo above).
  const loadAiInfo = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/terrapod/v1/runs/${runId}/plan-summary`)
      if (res.status === 404) { setAiInfo({ present: false }); return }
      if (!res.ok) return
      const d = await res.json()
      const a = d.data?.attributes
      setAiInfo({ present: true, status: a?.status, risk: a?.['risk-level'] })
    } catch {
      /* rollup is best-effort chrome */
    }
  }, [runId])

  const loadPolicyInfo = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/terrapod/v1/runs/${runId}/policy-evaluations`)
      if (!res.ok) return
      const d = await res.json()
      const evals = (d.data || []) as unknown[]
      const s = d.meta?.summary
      setPolicyInfo({
        present: evals.length > 0,
        status: s?.status,
        passed: s?.passed,
        total: s?.total,
        failed: s?.failed,
      })
    } catch {
      /* rollup is best-effort chrome */
    }
  }, [runId])

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadRun()
  }, [router, loadRun])

  // Keep the Overview rollups fresh: AI on mount + whenever a summary
  // lifecycle SSE event bumps aiSummaryRefresh; policy on mount + whenever the
  // run status changes (evals land during planning / an override can unblock).
  useEffect(() => { loadAiInfo() }, [loadAiInfo, aiSummaryRefresh])
  useEffect(() => { loadPolicyInfo() }, [loadPolicyInfo, run?.attributes.status])

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
        throw new Error(data.detail || t(`errors.action.${action}`))
      }
      if (action === 'retry') {
        const data = await res.json()
        const newRunId = data?.data?.id
        if (newRunId) {
          router.push(`/workspaces/${workspaceId}/runs/${newRunId}`)
          return
        }
      }
      // Confirm = applies — jump straight to the Apply Log tab so the user
      // watches the apply stream instead of staying on the overview.
      if (action === 'confirm') {
        switchView('apply')
      }
      await loadRun()
    } catch (err) {
      setError(err instanceof Error ? err.message : t(`errors.action.${action}`))
    } finally {
      setActionLoading('')
    }
  }

  // Touch is a fat-finger danger zone — a stray tap on Apply/Discard/Cancel/
  // Retry mutates infra or state. On a touch device (phone, tablet, foldable —
  // any width) gate every state-changing action behind the browser-native
  // confirm(); with a precise pointer (mouse/trackpad) execute immediately.
  // Keyed on pointer, not width, so a wide touch tablet is still guarded.
  function requestAction(action: 'confirm' | 'discard' | 'cancel' | 'retry') {
    if (isTouch) {
      const prompts: Record<string, string> = {
        confirm: t('confirm.apply'),
        discard: t('confirm.discard'),
        cancel: t('confirm.cancel'),
        retry: t('confirm.retry'),
      }
      if (!window.confirm(prompts[action])) return
    }
    handleAction(action)
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
  if (!run) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><ErrorBanner message={t('notFound')} /></main></>

  const attrs = run.attributes
  const actions = attrs.actions
  const timestamps = attrs['status-timestamps'] || {}

  // ── Tabs (#721) — AI + OPA appear only when the run has them; Apply only
  // for plan+apply runs. `view` clamps to Overview if the active tab isn't
  // available (e.g. a deep link to ?view=ai on a run with no AI summary).
  // Labels shorten below `md` so all six tabs need less horizontal scroll on a
  // phone ("Plan"/"Apply" vs "Plan Log"/"Apply Log"); desktop keeps the full
  // words. The " Log" suffix is CSS-hidden on mobile — one DRY label.
  const planLabel = t.rich('tabs.planLabel', {
    log: (chunks) => <span className="hidden md:inline"> {chunks}</span>,
  })
  const applyLabel = t.rich('tabs.applyLabel', {
    log: (chunks) => <span className="hidden md:inline"> {chunks}</span>,
  })
  // Each tab carries a rich label (for the desktop bar) AND a plain-text label
  // (for the mobile <select>, whose <option>s can't hold JSX).
  const tabs: [RunView, React.ReactNode, string][] = [
    ['overview', t('tabs.overview'), t('tabs.overview')],
    ...((aiInfo?.present ? [['ai', t('tabs.ai'), t('tabs.aiFull')]] : []) as [RunView, React.ReactNode, string][]),
    ...((policyInfo?.present ? [['opa', t('tabs.opa'), t('tabs.opaFull')]] : []) as [RunView, React.ReactNode, string][]),
    ...((attrs['has-json-output']
      ? [['impact', t('tabs.impact'), t('tabs.impactFull')]]
      : []) as [RunView, React.ReactNode, string][]),
    ['plan', planLabel, t('tabs.planFull')],
    ...((attrs['plan-only'] ? [] : [['apply', applyLabel, t('tabs.applyFull')]]) as [RunView, React.ReactNode, string][]),
    ['details', t('tabs.details'), t('tabs.details')],
  ]
  const availableViews = new Set(tabs.map((t) => t[0]))
  const view: RunView = availableViews.has(activeView) ? activeView : 'overview'

  // ── Overview summary cards ──────────────────────────────────────────
  const ps = attrs['plan-summary']
  const changeCard: { value: React.ReactNode; sub?: string; tone: CardTone } = (() => {
    if (attrs['has-changes'] === false && !attrs['plan-only'] && ['planned', 'applied'].includes(attrs.status)) {
      return { value: t('changes.noChanges'), tone: 'neutral' }
    }
    if (ps) {
      // Colour-coded counts (add=green, change=amber, destroy=red,
      // replace=orange, import=blue) read far faster than a flat "+2 ~1 -3",
      // with a plain-English breakdown underneath now there's room for it.
      const segs: { k: string; sym: string; n: number; cls: string; word: string }[] = [
        { k: 'add', sym: '+', n: ps.add, cls: 'text-green-400', word: t('changes.toAdd') },
        { k: 'change', sym: '~', n: ps.change, cls: 'text-amber-400', word: t('changes.toChange') },
        { k: 'destroy', sym: '−', n: ps.destroy, cls: 'text-red-400', word: t('changes.toDestroy') },
        { k: 'replace', sym: '±', n: ps.replace, cls: 'text-orange-400', word: t('changes.toReplace') },
        { k: 'import', sym: '↓', n: ps.import, cls: 'text-blue-400', word: t('changes.toImport') },
      ].filter((s) => s.n > 0)
      if (segs.length === 0) return { value: t('changes.noChanges'), tone: 'neutral' }
      return {
        value: (
          <span className="flex flex-wrap gap-x-3 gap-y-0.5 tabular-nums">
            {segs.map((s) => (
              <span key={s.k} className={s.cls}>
                {s.sym}
                {s.n}
              </span>
            ))}
          </span>
        ),
        sub: segs.map((s) => `${s.n} ${s.word}`).join(', '),
        tone: 'neutral',
      }
    }
    if (['pending', 'queued', 'planning'].includes(attrs.status)) return { value: t('changes.planning'), tone: 'neutral' }
    return { value: '—', tone: 'neutral' }
  })()

  const aiCard: { value: string; sub?: string; tone: CardTone; clickable: boolean } = (() => {
    if (!aiInfo) return { value: '…', tone: 'neutral', clickable: false }
    if (!aiInfo.present) return { value: t('ai.notAvailable'), tone: 'neutral', clickable: false }
    switch (aiInfo.status) {
      case 'ready':
        return {
          value: t('ai.ready'),
          sub: aiInfo.risk ? t('ai.riskSub', { risk: aiInfo.risk }) : undefined,
          tone: aiInfo.risk === 'high' || aiInfo.risk === 'critical' ? 'bad' : aiInfo.risk === 'medium' ? 'warn' : 'good',
          clickable: true,
        }
      case 'pending':
        return { value: t('ai.generating'), tone: 'active', clickable: true }
      case 'skipped':
        return { value: t('ai.skipped'), tone: 'neutral', clickable: true }
      case 'errored':
        return { value: t('ai.failed'), tone: 'bad', clickable: true }
      default:
        return { value: aiInfo.status ?? t('ai.available'), tone: 'neutral', clickable: true }
    }
  })()

  const policyCard: { value: string; sub?: string; tone: CardTone; clickable: boolean } = (() => {
    if (!policyInfo) return { value: '…', tone: 'neutral', clickable: false }
    if (!policyInfo.present) return { value: t('policy.none'), tone: 'neutral', clickable: false }
    if (policyInfo.status === 'blocked') return { value: t('policy.blocked'), sub: t('policy.failedSub', { count: policyInfo.failed ?? 0 }), tone: 'bad', clickable: true }
    if ((policyInfo.failed ?? 0) > 0)
      return { value: t('policy.advisoryIssues'), sub: t('policy.passedSub', { passed: policyInfo.passed ?? 0, total: policyInfo.total ?? 0 }), tone: 'warn', clickable: true }
    return { value: t('policy.passed'), sub: `${policyInfo.passed}/${policyInfo.total}`, tone: 'good', clickable: true }
  })()

  const resourceCard: { value: string; sub?: string; tone: CardTone } = (() => {
    const exit = attrs['runner-exit-status']
    const peak = attrs['peak-memory-bytes']
    if (exit === 'oom' || exit === 'killed') return { value: t('resources.overLimit'), sub: t('resources.oomKilled'), tone: 'bad' }
    if (peak != null) {
      const limit = parseMemoryToBytes(attrs['resource-memory']) * 2
      const pct = Number.isFinite(limit) && limit > 0 ? Math.round((peak / limit) * 100) : null
      const tone: CardTone = pct == null ? 'neutral' : pct >= 95 ? 'bad' : pct >= 80 ? 'warn' : 'good'
      const label = pct == null ? t('resources.recorded') : pct >= 95 ? t('resources.nearLimit') : pct >= 80 ? t('resources.high') : t('resources.withinLimits')
      return { value: label, sub: `${humanBytes(peak)}${pct != null ? ` · ${pct}%` : ''}`, tone }
    }
    return { value: '—', tone: 'neutral' }
  })()

  const hasActions =
    actions['is-confirmable'] ||
    actions['is-discardable'] ||
    actions['is-cancelable'] ||
    actions['is-retryable']

  // Status pills — reused in the desktop header (top-right) and, on mobile, in
  // the combined status+actions row below the title (one row, not two).
  const statusBadges = (
    <>
      {attrs['is-destroy'] && (
        <span className="inline-flex items-center px-3 py-1 rounded-full text-sm font-medium bg-red-900/50 text-red-300">
          {t('badge.destroy')}
        </span>
      )}
      {attrs['plan-only'] && !attrs['is-destroy'] && (
        <span className="inline-flex items-center px-3 py-1 rounded-full text-sm font-medium bg-cyan-900/50 text-cyan-300">
          {t('badge.planOnly')}
        </span>
      )}
      <span className={`inline-flex items-center px-3 py-1 rounded-full text-sm font-medium ${statusColor(attrs.status)}`}>
        {t.has(`status.${attrs.status}`) ? t(`status.${attrs.status}`) : attrs.status}
      </span>
    </>
  )

  // Primary run actions — shared between the desktop bar and the mobile row.
  // Labels go terse below `md` (Apply / Retry / Cancel) and full at `md+`
  // (Confirm & Apply / Retry Run / Cancel Run). `requestAction` inserts the
  // mobile confirm step; desktop runs immediately.
  const actionButtons = (
    <>
      {actions['is-retryable'] && (
        <button
          onClick={() => requestAction('retry')}
          disabled={!!actionLoading}
          className="px-3 md:px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
        >
          {actionLoading === 'retry' ? t('actions.retrying') : (
            <>
              <span className="md:hidden">{t('actions.retryShort')}</span>
              <span className="hidden md:inline">{t('actions.retryFull')}</span>
            </>
          )}
        </button>
      )}
      {actions['is-confirmable'] && (
        <button
          onClick={() => requestAction('confirm')}
          disabled={!!actionLoading}
          className="px-3 md:px-4 py-2 rounded-lg text-sm font-medium bg-green-600 hover:bg-green-500 disabled:bg-green-800 disabled:text-green-400 text-white transition-colors"
        >
          {actionLoading === 'confirm' ? t('actions.confirming') : (
            <>
              <span className="md:hidden">{t('actions.confirmShort')}</span>
              <span className="hidden md:inline">{t('actions.confirmFull')}</span>
            </>
          )}
        </button>
      )}
      {actions['is-discardable'] && (
        <button
          onClick={() => requestAction('discard')}
          disabled={!!actionLoading}
          className="px-3 md:px-4 py-2 rounded-lg text-sm font-medium bg-slate-600 hover:bg-slate-500 disabled:bg-slate-700 disabled:text-slate-400 text-white transition-colors"
        >
          {actionLoading === 'discard' ? t('actions.discarding') : t('actions.discard')}
        </button>
      )}
      {actions['is-cancelable'] && (
        <button
          onClick={() => requestAction('cancel')}
          disabled={!!actionLoading}
          className="px-3 md:px-4 py-2 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-500 disabled:bg-red-800 disabled:text-red-400 text-white transition-colors"
        >
          {actionLoading === 'cancel' ? t('actions.canceling') : (
            <>
              <span className="md:hidden">{t('actions.cancelShort')}</span>
              <span className="hidden md:inline">{t('actions.cancelFull')}</span>
            </>
          )}
        </button>
      )}
    </>
  )

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <div className="mb-4">
          <Link href={`/workspaces/${workspaceId}?tab=runs`} className="text-sm text-slate-400 hover:text-slate-200">
            &larr; {t('backTo', { name: attrs['workspace-name'] || t('workspaceFallback') })}
          </Link>
        </div>

        <PageHeader
          title={
            attrs['workspace-name']
              ? t('header.titleWithWorkspace', { workspace: attrs['workspace-name'], id: run.id.replace(/^run-/, '').split('-').pop() ?? '' })
              : t('header.title', { id: run.id.replace(/^run-/, '').split('-').pop() ?? '' })
          }
          description={attrs.message || t('header.description', { source: attrs.source })}
          actions={
            <div className="flex items-center gap-2">
              <ConnectionStatus connected={sseConnected} />
              {/* Desktop keeps the status pills top-right beside the title. On
                  mobile they move down to share one row with the action buttons
                  (see below), so they're hidden here below `md`. */}
              <div className="hidden md:flex items-center gap-2">{statusBadges}</div>
            </div>
          }
        />

        {error && <ErrorBanner message={error} />}

        {/* Primary run actions live OUTSIDE the tab structure (#721) so they
            stay reachable from any tab — confirm an apply while watching the
            plan log, cancel from Details, retry from anywhere.

            Mobile (`< md`): the status pills + action buttons share ONE row
            below the title (the header pills are desktop-only). A tapped
            state-changing action is gated behind a native confirm() (see
            requestAction). This row always renders on mobile so the status pill
            has a home even when there are no actions. */}
        <div className="md:hidden flex flex-wrap items-center gap-x-4 gap-y-2 mb-6">
          <div className="flex items-center gap-2">{statusBadges}</div>
          <div className="flex flex-wrap items-center gap-2">{actionButtons}</div>
        </div>
        {/* Desktop (`md+`): buttons only (pills are in the header), single
            click — unchanged. */}
        {hasActions && (
          <div className="hidden md:flex flex-wrap gap-3 mb-6">{actionButtons}</div>
        )}

        {/* View tabs (#721) — Overview summarises the run; AI and OPA appear
            only when the run has them; each log gets its own full-height tab;
            Details holds metadata / timeline / resource usage / run options.
            Six tabs don't fit a phone, so the strip scrolls horizontally with a
            right-edge fade cueing there's more (mobile only — desktop fits). */}
        {/* Mobile (`< md`): a native <select> view picker — one tap, native
            picker, no awful horizontal-scroll strip. Desktop (`md+`): the tab
            bar (all tabs fit, so no scroll/fade needed). One `tabs` source, two
            viewport-driven presentations; the URL stays the source of truth via
            switchView either way. */}
        <div className="mb-6 md:hidden">
          <label htmlFor="run-view-select" className="sr-only">
            {t('runSection')}
          </label>
          <select
            id="run-view-select"
            value={view}
            onChange={(e) => switchView(e.target.value as RunView)}
            className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2.5 text-sm font-medium text-slate-100 focus:border-brand-500 focus:outline-none"
          >
            {tabs.map(([v, , text]) => (
              <option key={v} value={v}>
                {text}
              </option>
            ))}
          </select>
        </div>
        <div className="hidden border-b border-slate-700/50 mb-6 md:block">
          <div className="flex gap-1 -mb-px">
            {tabs.map(([v, label]) => (
              <button
                key={v}
                onClick={() => switchView(v)}
                aria-current={view === v ? 'page' : undefined}
                className={`px-4 py-2 text-sm font-medium border-b-2 whitespace-nowrap transition-colors ${
                  view === v
                    ? 'border-brand-500 text-brand-400'
                    : 'border-transparent text-slate-400 hover:text-slate-200 hover:border-slate-600'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {view === 'overview' && (
        <>
        {/* Run status + live activity at a glance (#721). */}
        <RunActivityHeader
          status={attrs.status}
          timestamps={timestamps}
          planOnly={attrs['plan-only']}
          isConfirmable={actions['is-confirmable']}
        />

        {/* Destroy run warning */}
        {attrs['is-destroy'] && (
          <div className="mb-6 p-4 bg-red-900/20 rounded-lg border border-red-800/50">
            <p className="text-sm text-red-300">
              {t.rich('banner.destroy', { strong: (chunks) => <strong>{chunks}</strong> })}
            </p>
          </div>
        )}

        {/* Agent plan-only indicator for CLI-sourced runs on VCS-connected workspaces */}
        {attrs['plan-only'] && attrs.source === 'tfe-api' && attrs['workspace-has-vcs'] && run.relationships?.['configuration-version']?.data && (
          <div className="mb-6 p-4 bg-cyan-900/20 rounded-lg border border-cyan-800/50">
            <p className="text-sm text-cyan-300">
              {t.rich('banner.planOnlyCli', { strong: (chunks) => <strong>{chunks}</strong> })}
            </p>
          </div>
        )}

        {/* Module override banner for module-test runs */}
        {attrs['module-overrides'] && (
          <div className="mb-6 p-4 bg-purple-900/20 rounded-lg border border-purple-800/50">
            <p className="text-sm text-purple-300">
              {t.rich('banner.moduleOverridesPrefix', { strong: (chunks) => <strong>{chunks}</strong> })}
              {attrs['vcs-pull-request-number'] && <> {t('banner.moduleOverridesFromPr', { number: attrs['vcs-pull-request-number'] })}</>}
              {' '}&mdash; {Object.keys(attrs['module-overrides']).map(coord => (
                <code key={coord} className="bg-purple-900/50 px-1.5 py-0.5 rounded text-xs font-mono">{coord}</code>
              ))}
              {' '}{t('banner.moduleOverridesSuffix')}
            </p>
          </div>
        )}

        {/* Error message */}
        {attrs['error-message'] && (
          <div className="mb-6 p-4 bg-red-900/20 rounded-lg border border-red-800/50">
            <h3 className="text-sm font-medium text-red-400 mb-1">{t('errorHeading')}</h3>
            <pre className="text-sm text-red-300 whitespace-pre-wrap font-mono">{attrs['error-message']}</pre>
          </div>
        )}

        {/* Discard reason (#646/#647): why a discarded plan can no longer apply. */}
        {attrs.status === 'discarded' && attrs['discard-reason'] && (
          <div className="mb-6 p-4 bg-amber-900/20 rounded-lg border border-amber-800/50">
            <h3 className="text-sm font-medium text-amber-400 mb-1">{t('discardedHeading')}</h3>
            <p className="text-sm text-amber-200">{attrs['discard-reason']}</p>
          </div>
        )}

        {/* At-a-glance summary cards (#721) — each drills into its own tab. */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-6">
          <SummaryCard
            label={t('cards.changes')}
            value={changeCard.value}
            sub={changeCard.sub}
            tone={changeCard.tone}
            onClick={() => switchView('plan')}
          />
          <SummaryCard
            label={t('cards.aiAnalysis')}
            value={aiCard.value}
            sub={aiCard.sub}
            tone={aiCard.tone}
            onClick={aiCard.clickable ? () => switchView('ai') : undefined}
          />
          <SummaryCard
            label={t('cards.policyChecks')}
            value={policyCard.value}
            sub={policyCard.sub}
            tone={policyCard.tone}
            onClick={policyCard.clickable ? () => switchView('opa') : undefined}
          />
          <SummaryCard
            label={t('cards.resources')}
            value={resourceCard.value}
            sub={resourceCard.sub}
            tone={resourceCard.tone}
            onClick={() => switchView('details')}
          />
        </div>
        </>
        )}

        {/* AI analysis tab (#401) — its own full panel; the tab only appears
            when the run has an AI summary. */}
        {view === 'ai' && (
          <PlanAiSummary runId={runId.replace(/^run-/, '')} refreshKey={aiSummaryRefresh} />
        )}

        {/* OPA policy tab (#343) — full evaluations + admin override; the tab
            only appears when the run has policy checks. */}
        {view === 'opa' && (
          <PolicyPanel
            runId={runId}
            runStatus={attrs.status}
            onChanged={() => {
              loadRun()
              loadPolicyInfo()
            }}
          />
        )}

        {/* Impact graph tab (#761) — interactive plan dependency + blast-radius
            view; the tab only appears when the run produced a JSON plan. */}
        {view === 'impact' && <ImpactGraph runId={runId.replace(/^run-/, '')} />}

        {view === 'details' && (
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
          <h3 className="text-sm font-medium text-slate-300 mb-4">{t('details.heading')}</h3>
          <dl className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-4">
            <div>
              <dt className="text-xs text-slate-500">{t('details.executionBackend')}</dt>
              <dd className="mt-1 text-sm text-slate-200">{attrs['execution-backend'] === 'terraform' ? 'Terraform' : 'OpenTofu'}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">{t('details.source')}</dt>
              <dd className="mt-1 text-sm text-slate-200">{attrs.source}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">{t('details.autoApply')}</dt>
              <dd className="mt-1 text-sm text-slate-200">{attrs['auto-apply'] ? t('common.yes') : t('common.no')}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">{t('details.planOnly')}</dt>
              <dd className="mt-1 text-sm text-slate-200">{attrs['plan-only'] ? t('common.yes') : t('common.no')}</dd>
            </div>
            {attrs['created-by'] && (
              <div>
                <dt className="text-xs text-slate-500">{t('details.triggeredBy')}</dt>
                <dd className="mt-1 text-sm text-slate-200">{attrs['created-by']}</dd>
              </div>
            )}
            <div>
              <dt className="text-xs text-slate-500">{t('details.created')}</dt>
              <dd className="mt-1 text-sm text-slate-200">{formatTimestamp(attrs['created-at'])}</dd>
            </div>
            {attrs['vcs-commit-sha'] && (
              <div>
                <dt className="text-xs text-slate-500">{t('details.commit')}</dt>
                <dd className="mt-1 text-sm text-slate-200 font-mono">{attrs['vcs-commit-sha'].slice(0, 8)}</dd>
              </div>
            )}
            {attrs['vcs-branch'] && (
              <div>
                <dt className="text-xs text-slate-500">{t('details.branch')}</dt>
                <dd className="mt-1 text-sm text-slate-200">{attrs['vcs-branch']}</dd>
              </div>
            )}
            {attrs['vcs-pull-request-number'] && (
              <div>
                <dt className="text-xs text-slate-500">{t('details.prMr')}</dt>
                <dd className="mt-1 text-sm text-slate-200">#{attrs['vcs-pull-request-number']}</dd>
              </div>
            )}
            {(run.relationships?.['created-state-version'] as { data: { id: string } | null } | undefined)?.data && (
              <div>
                <dt className="text-xs text-slate-500">{t('details.stateVersion')}</dt>
                <dd className="mt-1 text-sm">
                  <Link href={`/workspaces/${workspaceId}?tab=state`} className="text-brand-400 hover:text-brand-300">
                    {t('details.viewState')}
                  </Link>
                </dd>
              </div>
            )}
          </dl>

          {/* Run options (only show when non-default) */}
          {(attrs['target-addrs']?.length > 0 || attrs['replace-addrs']?.length > 0 || attrs['refresh-only'] || !attrs['refresh'] || attrs['allow-empty-apply']) && (
            <div className="mt-4 pt-4 border-t border-slate-700/50">
              <h4 className="text-xs text-slate-500 mb-2">{t('details.runOptions')}</h4>
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
          <h3 className="text-sm font-medium text-slate-300 mb-4">{t('timeline.heading')}</h3>
          <div className="space-y-2">
            {[
              ['queued-at', t('timeline.queued')],
              ['planning-at', t('timeline.planningStarted')],
              ['planned-at', t('timeline.planComplete')],
              ['confirmed-at', t('timeline.confirmed')],
              ['applying-at', t('timeline.applyingStarted')],
              ['applied-at', t('timeline.applied')],
              ['errored-at', t('timeline.errored')],
              ['canceled-at', t('timeline.canceled')],
              ['discarded-at', t('timeline.discarded')],
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

        {/* Plan log */}
        {view === 'plan' && (
          <LogPanel
            log={planLog}
            precomputedHtml={planHtml}
            loading={planLogLoading}
            emptyMessage={t('log.planEmpty')}
            phase="plan"
            runId={runId}
            isStreaming={attrs.status === 'planning'}
            onRefresh={() => loadPlanLog(true)}
          />
        )}

        {/* Apply log */}
        {view === 'apply' && (
          <LogPanel
            log={applyLog}
            precomputedHtml={applyHtml}
            loading={applyLogLoading}
            emptyMessage={t('log.applyEmpty')}
            phase="apply"
            runId={runId}
            isStreaming={attrs.status === 'applying'}
            onRefresh={() => loadApplyLog(true)}
          />
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
  const t = useTranslations('runDetail')
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
        throw new Error(d.detail || t('policyPanel.overrideFailedStatus', { status: res.status }))
      }
      await load()
      onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('policyPanel.overrideFailed'))
    } finally {
      setOverriding(false)
    }
  }

  return (
    <div className="mb-6 bg-slate-800/50 rounded-lg border border-slate-700/50 p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-slate-200">{t('policyPanel.heading')}</h3>
        {summary && (
          <span className="text-xs text-slate-400">
            {t('policyPanel.passedCount', { passed: summary.passed, total: summary.total })}
          </span>
        )}
      </div>

      {blocked && (
        <div className="mb-3 p-3 bg-red-900/20 rounded-lg border border-red-800/50">
          <p className="text-sm text-red-300">
            {t.rich('policyPanel.blockedMessage', { strong: (chunks) => <strong>{chunks}</strong> })}
          </p>
          {isAdmin() && (
            <button
              onClick={override}
              disabled={overriding}
              className="mt-2 px-3 py-1.5 rounded-lg text-sm font-medium bg-red-900/60 hover:bg-red-800 disabled:opacity-50 text-red-100 transition-colors"
            >
              {overriding ? t('policyPanel.overriding') : t('policyPanel.overrideContinue')}
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
                  {t.has(`policyPanel.enforcement.${a['enforcement-level']}`) ? t(`policyPanel.enforcement.${a['enforcement-level']}`) : a['enforcement-level']}
                </span>
                <span
                  className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${outcomeBadge(a.outcome)}`}
                >
                  {t.has(`policyPanel.outcome.${a.outcome}`) ? t(`policyPanel.outcome.${a.outcome}`) : a.outcome}
                </span>
                {a['overridden-by'] && (
                  <span className="text-xs text-slate-500">{t('policyPanel.overriddenBy', { by: a['overridden-by'] })}</span>
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
                        {p.passed && <span className="text-green-500">{t('policyPanel.policyPassed')}</span>}
                      </div>
                      {p.error && (
                        <div className="mt-1 ml-5 p-2 bg-amber-900/20 rounded border border-amber-800/40">
                          <p className="text-amber-300 font-mono whitespace-pre-wrap">{p.error}</p>
                        </div>
                      )}
                      {!p.passed && !p.error && (!p.violations || p.violations.length === 0) && (
                        <p className="mt-1 ml-5 text-slate-400 italic">{t('policyPanel.noDenyMessage')}</p>
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
