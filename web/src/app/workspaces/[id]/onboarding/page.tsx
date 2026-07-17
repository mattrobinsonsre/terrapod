'use client'

/**
 * Onboarding discovery (#824 P2) — point Terrapod at existing, unmanaged cloud
 * resources and get reviewable `resource` + `import {}` config back.
 *
 * The flow is a small state machine driven by the onboarding session status:
 *   (start form) → pending → schema_ready → querying → config_ready
 * The page polls while a session is non-terminal so it advances on its own.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'next/navigation'
import Link from 'next/link'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { apiFetch } from '@/lib/api'

interface DataSource {
  name: string
  has_filter?: boolean
  has_tags?: boolean
  returns_list?: boolean
  required_inputs?: string[] | null
}

interface SessionAttrs {
  status: string
  provider: string
  engine: string
  'engine-version': string
  'selected-types': string[]
  error: string
  'data-source-count': number | null
  'discovery-surface': { count?: number; data_sources?: DataSource[] } | null
  'generated-config': string | null
  'import-blocks': string | null
  'polished-config': string | null
  'polished-import-blocks': string | null
  'paired-config': string | null
  'paired-polished-config': string | null
  'ai-assisted': boolean
  'discovery-run-id': string | null
  'created-at': string
}
interface Session {
  id: string
  attributes: SessionAttrs
}

const TERMINAL = new Set(['config_ready', 'errored', 'canceled', 'run_created'])
const POLL_MS = 3000

export default function OnboardingPage() {
  const params = useParams<{ id: string }>()
  const workspaceId = params.id
  const t = useTranslations('onboarding')

  const [wsName, setWsName] = useState('')
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [surface, setSurface] = useState<{ data_sources?: DataSource[] } | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [provider, setProvider] = useState('aws')
  const [providerVersion, setProviderVersion] = useState('')
  const [starting, setStarting] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [filter, setFilter] = useState('')
  const [discovering, setDiscovering] = useState(false)
  const [copied, setCopied] = useState('')
  // Raw↔polished view toggle for the reviewable config. Defaults to the polished
  // view when the AI polish is available (falls back to raw automatically when
  // it isn't). Never loses the raw view — the operator can flip back any time.
  const [showRaw, setShowRaw] = useState(false)

  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const active = useMemo(
    () => sessions.find((s) => s.id === activeId) ?? null,
    [sessions, activeId],
  )

  // The AI polish exists only when it landed (ai-assisted) AND produced config.
  // `usePolished` is what the toggle + code blocks read; raw is always the
  // fallback, so a missing/rejected polish silently shows the deterministic view.
  const hasPolished =
    !!active?.attributes['ai-assisted'] && !!active?.attributes['polished-config']
  const usePolished = hasPolished && !showRaw

  const loadWorkspace = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}`)
      if (res.ok) {
        const d = await res.json()
        setWsName(d?.data?.attributes?.name ?? '')
      }
    } catch {
      /* header name is best-effort */
    }
  }, [workspaceId])

  const loadSessions = useCallback(async () => {
    const res = await apiFetch(`/api/terrapod/v1/workspaces/${workspaceId}/onboarding-sessions`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const d = await res.json()
    const list: Session[] = d?.data ?? []
    setSessions(list)
    setActiveId((cur) => cur ?? (list.length ? list[0].id : null))
    return list
  }, [workspaceId])

  // Detail fetch for the active session (carries the surface + generated config,
  // which the list omits).
  const loadActiveDetail = useCallback(async (id: string) => {
    const res = await apiFetch(`/api/terrapod/v1/onboarding-sessions/${id}`)
    if (!res.ok) return
    const d = await res.json()
    const s: Session = d.data
    setSessions((prev) => prev.map((p) => (p.id === s.id ? s : p)))
    setSurface(s.attributes['discovery-surface'] ?? null)
  }, [])

  useEffect(() => {
    ;(async () => {
      try {
        setLoading(true)
        await Promise.all([loadWorkspace(), loadSessions()])
        setError('')
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setLoading(false)
      }
    })()
  }, [loadWorkspace, loadSessions])

  // Load the active session's detail when it changes.
  useEffect(() => {
    if (activeId) loadActiveDetail(activeId)
  }, [activeId, loadActiveDetail])

  // Poll while the active session is non-terminal so it advances live.
  useEffect(() => {
    if (pollRef.current) clearTimeout(pollRef.current)
    if (!active || TERMINAL.has(active.attributes.status)) return
    pollRef.current = setTimeout(() => {
      loadActiveDetail(active.id)
    }, POLL_MS)
    return () => {
      if (pollRef.current) clearTimeout(pollRef.current)
    }
  }, [active, loadActiveDetail])

  async function startSession(e: React.FormEvent) {
    e.preventDefault()
    const p = provider.trim().toLowerCase()
    if (!p) return
    setStarting(true)
    setError('')
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/workspaces/${workspaceId}/onboarding-sessions`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            data: {
              attributes: { provider: p, 'provider-version': providerVersion.trim() },
            },
          }),
        },
      )
      if (!res.ok) {
        const b = await res.json().catch(() => ({}))
        throw new Error(b?.detail || `HTTP ${res.status}`)
      }
      const d = await res.json()
      setSelected(new Set())
      await loadSessions()
      setActiveId(d.data.id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setStarting(false)
    }
  }

  async function discover() {
    if (!active || selected.size === 0) return
    setDiscovering(true)
    setError('')
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/onboarding-sessions/${active.id}/discover`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            data: { attributes: { 'selected-types': [...selected] } },
          }),
        },
      )
      if (!res.ok) {
        const b = await res.json().catch(() => ({}))
        throw new Error(b?.detail || `HTTP ${res.status}`)
      }
      await loadActiveDetail(active.id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDiscovering(false)
    }
  }

  function copy(key: string, text: string) {
    navigator.clipboard?.writeText(text).then(() => {
      setCopied(key)
      setTimeout(() => setCopied(''), 1500)
    })
  }

  const dataSources = surface?.data_sources ?? []
  const shown = filter
    ? dataSources.filter((d) => d.name.toLowerCase().includes(filter.toLowerCase()))
    : dataSources

  if (loading)
    return (
      <>
        <NavBar />
        <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-5xl mx-auto">
          <LoadingSpinner />
        </main>
      </>
    )

  const st = active?.attributes.status

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-5xl mx-auto">
        <div className="mb-4">
          <Link
            href={`/workspaces/${workspaceId}`}
            className="text-sm text-slate-400 hover:text-slate-200"
          >
            {/* i18n-ignore — decorative left-arrow glyph, not copy */}&larr;{' '}
            {t('backTo', { workspace: wsName || workspaceId })}
          </Link>
        </div>
        <PageHeader
          title={t('title')}
          description={t('description', { workspace: wsName || workspaceId })}
        />
        {error && <ErrorBanner message={error} />}

        {/* Start a new discovery */}
        <form
          onSubmit={startSession}
          className="mb-6 rounded-lg border border-slate-700/50 bg-slate-800/40 p-4 flex flex-col sm:flex-row sm:items-end gap-3"
        >
          <div className="flex-1">
            <label htmlFor="onb-provider" className="block text-xs font-medium text-slate-400 mb-1">
              {t('providerLabel')}
            </label>
            <input
              id="onb-provider"
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              placeholder="aws" // i18n-ignore — example terraform provider name, not UX copy
              className="w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2.5 text-sm text-slate-100 focus:border-brand-500 focus:outline-none"
            />
          </div>
          <div className="flex-1">
            <label
              htmlFor="onb-provider-version"
              className="block text-xs font-medium text-slate-400 mb-1"
            >
              {t('providerVersionLabel')}
            </label>
            <input
              id="onb-provider-version"
              value={providerVersion}
              onChange={(e) => setProviderVersion(e.target.value)}
              placeholder="< 6.0" // i18n-ignore — example version constraint, not UX copy
              className="w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2.5 text-sm text-slate-100 focus:border-brand-500 focus:outline-none"
            />
          </div>
          <button
            type="submit"
            disabled={starting || !provider.trim()}
            className="px-4 py-2.5 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:opacity-50 text-white whitespace-nowrap"
          >
            {starting ? t('starting') : t('start')}
          </button>
        </form>

        {/* Session picker (past + current) */}
        {sessions.length > 0 && (
          <div className="mb-6 flex flex-wrap gap-2" role="tablist" aria-label={t('sessionsLabel')}>
            {sessions.map((s) => (
              <button
                key={s.id}
                role="tab"
                aria-selected={s.id === activeId}
                onClick={() => setActiveId(s.id)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap ${
                  s.id === activeId
                    ? 'bg-slate-700 text-slate-100'
                    : 'bg-slate-800/60 text-slate-400 hover:bg-slate-700/60'
                }`}
              >
                {s.attributes.provider} · {t(`state.${s.attributes.status}` as never)}
              </button>
            ))}
          </div>
        )}

        {!active && (
          <EmptyState message={t('empty')} />
        )}

        {active && (
          <section className="rounded-lg border border-slate-700/50 bg-slate-800/40 p-4 sm:p-6">
            <StatusBar status={st!} t={t} />

            {st === 'errored' && (
              <div className="mt-4 space-y-3">
                <ErrorBanner message={active.attributes.error || t('erroredFallback')} />
                {active.attributes['discovery-run-id'] && (
                  <Link
                    href={`/workspaces/${workspaceId}/runs/${active.attributes['discovery-run-id']}`}
                    className="inline-block px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200"
                  >
                    {t('viewRunLogs')}
                  </Link>
                )}
              </div>
            )}

            {st === 'pending' && (
              <p className="mt-4 text-sm text-slate-400">{t('analysing')}</p>
            )}

            {/* schema_ready → pick data sources to query */}
            {st === 'schema_ready' && (
              <div className="mt-4">
                <p className="text-sm text-slate-300 mb-3">
                  {t('surfaceIntro', { count: dataSources.length })}
                </p>
                <input
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                  placeholder={t('filterPlaceholder')}
                  className="w-full mb-3 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 focus:border-brand-500 focus:outline-none"
                />
                <div className="max-h-80 overflow-y-auto rounded-lg border border-slate-700/50 divide-y divide-slate-800">
                  {shown.map((d) => (
                    <label
                      key={d.name}
                      className="flex items-center gap-3 px-3 py-2 cursor-pointer hover:bg-slate-800/60"
                    >
                      <input
                        type="checkbox"
                        checked={selected.has(d.name)}
                        onChange={(e) => {
                          const next = new Set(selected)
                          if (e.target.checked) next.add(d.name)
                          else next.delete(d.name)
                          setSelected(next)
                        }}
                        className="accent-brand-500"
                      />
                      <span className="font-mono text-xs text-slate-200">{d.name}</span>
                      {d.has_filter && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-700 text-slate-300">
                          filter{/* i18n-ignore — HCL `filter {}` block name, not UX copy */}
                        </span>
                      )}
                      {d.has_tags && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-700 text-slate-300">
                          tags{/* i18n-ignore — HCL `tags` argument name, not UX copy */}
                        </span>
                      )}
                    </label>
                  ))}
                  {shown.length === 0 && (
                    <p className="px-3 py-4 text-sm text-slate-500">{t('noMatches')}</p>
                  )}
                </div>
                <button
                  onClick={discover}
                  disabled={discovering || selected.size === 0}
                  className="mt-4 px-4 py-2.5 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:opacity-50 text-white"
                >
                  {discovering
                    ? t('discovering')
                    : t('discover', { count: selected.size })}
                </button>
              </div>
            )}

            {/* querying → in flight */}
            {st === 'querying' && (
              <div className="mt-4 flex items-center gap-3 text-sm text-slate-400">
                <LoadingSpinner />
                <span>{t('queryingHint')}</span>
                {active.attributes['discovery-run-id'] && (
                  <Link
                    href={`/workspaces/${workspaceId}/runs/${active.attributes['discovery-run-id']}`}
                    className="text-brand-400 hover:text-brand-300 underline"
                  >
                    {t('viewRun')}
                  </Link>
                )}
              </div>
            )}

            {/* config_ready but empty → discovery ran clean and found nothing */}
            {st === 'config_ready' && !active.attributes['generated-config'] && (
              <div className="mt-4 space-y-3">
                <EmptyState
                  message={t('noResources', {
                    types: active.attributes['selected-types'].join(', '),
                  })}
                />
                {active.attributes['discovery-run-id'] && (
                  <Link
                    href={`/workspaces/${workspaceId}/runs/${active.attributes['discovery-run-id']}`}
                    className="inline-block px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200"
                  >
                    {t('viewRun')}
                  </Link>
                )}
              </div>
            )}

            {/* config_ready → the reviewable payload */}
            {st === 'config_ready' && active.attributes['generated-config'] && (
              <div className="mt-4 space-y-5">
                <p className="text-sm text-slate-300">
                  {t('reviewIntro', { types: active.attributes['selected-types'].join(', ') })}
                </p>
                {hasPolished && (
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="inline-flex items-center gap-1 rounded-full bg-brand-900/50 px-2.5 py-1 text-xs font-medium text-brand-300">
                      ✨ {t('aiPolishedBadge')}
                    </span>
                    <span className="text-xs text-slate-500">{t('aiPolishedNote')}</span>
                    <div className="ml-auto inline-flex overflow-hidden rounded-lg border border-slate-700">
                      <button
                        type="button"
                        onClick={() => setShowRaw(false)}
                        className={`px-3 py-1.5 text-xs font-medium ${
                          usePolished
                            ? 'bg-brand-600 text-white'
                            : 'bg-slate-800 text-slate-300 hover:bg-slate-700'
                        }`}
                      >
                        {t('viewPolished')}
                      </button>
                      <button
                        type="button"
                        onClick={() => setShowRaw(true)}
                        className={`px-3 py-1.5 text-xs font-medium ${
                          !usePolished
                            ? 'bg-slate-600 text-white'
                            : 'bg-slate-800 text-slate-300 hover:bg-slate-700'
                        }`}
                      >
                        {t('viewRaw')}
                      </button>
                    </div>
                  </div>
                )}
                {/* Paired view first: each import{} directly above its resource — the
                    easiest thing to review and paste. The separate import / config
                    blocks below carry the same content, split. */}
                {(usePolished
                  ? active.attributes['paired-polished-config']
                  : active.attributes['paired-config']) && (
                  <div className="space-y-1">
                    <CodeBlock
                      title={t('pairedTitle')}
                      text={
                        (usePolished
                          ? active.attributes['paired-polished-config']
                          : active.attributes['paired-config']) || ''
                      }
                      copyKey="paired"
                      copied={copied}
                      onCopy={copy}
                      copyLabel={t('copy')}
                      copiedLabel={t('copied')}
                    />
                    <p className="text-xs text-slate-500">{t('pairedNote')}</p>
                  </div>
                )}
                <CodeBlock
                  title={t('importBlocks')}
                  text={
                    (usePolished
                      ? active.attributes['polished-import-blocks']
                      : active.attributes['import-blocks']) || ''
                  }
                  copyKey="imports"
                  copied={copied}
                  onCopy={copy}
                  copyLabel={t('copy')}
                  copiedLabel={t('copied')}
                />
                <CodeBlock
                  title={t('generatedConfig')}
                  text={
                    (usePolished
                      ? active.attributes['polished-config']
                      : active.attributes['generated-config']) || ''
                  }
                  copyKey="config"
                  copied={copied}
                  onCopy={copy}
                  copyLabel={t('copy')}
                  copiedLabel={t('copied')}
                />
                <p className="text-xs text-slate-500">{t('nextStepNote')}</p>
              </div>
            )}
          </section>
        )}
      </main>
    </>
  )
}

function StatusBar({ status, t }: { status: string; t: ReturnType<typeof useTranslations> }) {
  const tone: Record<string, string> = {
    pending: 'bg-slate-700 text-slate-300',
    schema_ready: 'bg-blue-900/50 text-blue-300',
    querying: 'bg-amber-900/50 text-amber-300',
    config_ready: 'bg-green-900/50 text-green-300',
    errored: 'bg-red-900/50 text-red-300',
    canceled: 'bg-slate-700 text-slate-400',
    run_created: 'bg-green-900/50 text-green-300',
  }
  return (
    <span
      className={`inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium ${
        tone[status] ?? 'bg-slate-700 text-slate-300'
      }`}
    >
      {t(`state.${status}` as never)}
    </span>
  )
}

function CodeBlock({
  title,
  text,
  copyKey,
  copied,
  onCopy,
  copyLabel,
  copiedLabel,
}: {
  title: string
  text: string
  copyKey: string
  copied: string
  onCopy: (k: string, t: string) => void
  copyLabel: string
  copiedLabel: string
}) {
  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-semibold text-slate-200">{title}</h3>
        <button
          onClick={() => onCopy(copyKey, text)}
          className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200"
        >
          {copied === copyKey ? copiedLabel : copyLabel}
        </button>
      </div>
      <pre className="overflow-x-auto rounded-lg border border-slate-700/50 bg-slate-950 p-3 text-xs text-slate-300 font-mono whitespace-pre">
        {text}
      </pre>
    </div>
  )
}
