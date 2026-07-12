'use client'

import { Suspense, useEffect, useRef, useState, useCallback, useMemo, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { useTranslations } from 'next-intl'
import Link from 'next/link'
import { useFormat } from '@/lib/format'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { ConnectionStatus } from '@/components/connection-status'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { WorkspaceStatusBadges } from '@/components/workspace-status-badges'
import { StatChip } from '@/components/stat-chip'
import { getAuthState } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { useSortable } from '@/lib/use-sortable'
import { useWorkspaceListEvents } from '@/lib/use-workspace-list-events'
import {
  hasLabelTerm,
  hasStatusTerm,
  HEALTH_ISSUE_STATUS,
  LOCKED_STATUS,
  matchWorkspace,
  parseFilterQuery,
  removeTerm,
  serializeFilter,
  toggleLabelTerm,
  toggleStatusTerm,
} from '@/lib/workspace-filter'
import { WORKSPACE_STATUSES, resolveStatus } from '@/lib/workspace-status'

interface LatestRun {
  id: string
  status: string
  'plan-only': boolean
  'created-at': string
}

interface HealthCondition {
  code: string
  severity: string
  title: string
  detail: string
}

interface Workspace {
  id: string
  attributes: {
    name: string
    'execution-mode': string
    'auto-apply': boolean
    'terraform-version': string
    locked: boolean
    'resource-cpu': string
    'resource-memory': string
    'agent-pool-name': string | null
    'drift-detection-enabled': boolean
    'drift-status': string
    'drift-latest-run-id': string | null
    'state-diverged': boolean
    'vcs-last-error': string | null
    'health-conditions': HealthCondition[]
    'lifecycle-state': 'active' | 'pending_deletion' | 'archived'
    'lifecycle-reason': string
    'latest-run': LatestRun | null
    'created-at': string
    labels?: Record<string, string> | null
  }
}

// Wrapped in <Suspense> at the bottom — `useSearchParams()` triggers Next.js's
// CSR bailout, and without a boundary the whole page fails to build statically.
function WorkspacesPageInner() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const t = useTranslations('workspaces')
  const ts = useTranslations('status')
  const fmt = useFormat()
  // Translate a filter-suggestion's category hint (its stable English key stays
  // the internal value; only the displayed label is localized).
  const hintLabel = (hint: string) => {
    switch (hint) {
      case 'status': return t('filter.hintStatus')
      case 'health': return t('filter.hintHealth')
      case 'label': return t('filter.hintLabel')
      case 'label key': return t('filter.hintLabelKey')
      default: return t('filter.hintName')
    }
  }
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Filter input. Tokens are space-separated; bare words match the workspace
  // name (case-insensitive substring), `key:value` / `key=value` match a
  // label exactly, and `key:` / `key=` matches any workspace that has the
  // label key set. Mirrored to the URL via ?q=… so refresh + share work.
  const [filterInput, setFilterInput] = useState(searchParams.get('q') || '')
  const parsedFilter = useMemo(() => parseFilterQuery(filterInput), [filterInput])

  // Status dropdown state. Closes on outside-click and Escape — same pattern
  // used for the run-actions menu so the page feels consistent.
  const [statusMenuOpen, setStatusMenuOpen] = useState(false)
  const statusMenuRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!statusMenuOpen) return
    const onClick = (e: MouseEvent) => {
      if (statusMenuRef.current && !statusMenuRef.current.contains(e.target as Node)) {
        setStatusMenuOpen(false)
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setStatusMenuOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [statusMenuOpen])

  // Label dropdown — same outside-click + Escape pattern as Status. The
  // dropdown is a two-level picker: first pick a label key, then a value.
  const [labelMenuOpen, setLabelMenuOpen] = useState(false)
  const [labelMenuKey, setLabelMenuKey] = useState<string | null>(null)
  const labelMenuRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!labelMenuOpen) return
    const onClick = (e: MouseEvent) => {
      if (labelMenuRef.current && !labelMenuRef.current.contains(e.target as Node)) {
        setLabelMenuOpen(false)
        setLabelMenuKey(null)
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (labelMenuKey !== null) setLabelMenuKey(null)
        else setLabelMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', onClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [labelMenuOpen, labelMenuKey])

  // Inline filter typeahead — AWS-style faceted suggestions popped under the
  // free-text input as you type, while still letting you type anything. The
  // dropdown suggests `key:value` labels, `status:…`, label keys (to drill),
  // and matching workspace names; clicking one inserts the chip.
  const [suggestOpen, setSuggestOpen] = useState(false)
  // -1 = nothing highlighted. A suggestion is only applied on an explicit
  // pick (click, or arrow-key to highlight + Enter); a bare Enter keeps the
  // free text the user typed.
  const [suggestIndex, setSuggestIndex] = useState(-1)
  const filterInputRef = useRef<HTMLInputElement>(null)
  const suggestWrapRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!suggestOpen) return
    const onClick = (e: MouseEvent) => {
      if (suggestWrapRef.current && !suggestWrapRef.current.contains(e.target as Node)) {
        setSuggestOpen(false)
      }
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [suggestOpen])

  // Sync the input to the URL whenever the parsed filter changes — debounced.
  //
  // Each `router.replace` triggers a Next.js RSC prefetch for the new URL.
  // Firing per-keystroke (e.g. typing "repo:data-pipelines") cascades
  // ~20 prefetches in a few hundred ms, overwhelms Next's dev server with
  // 503s, and can leave the address bar stuck on an early keystroke when
  // a later prefetch wins the race. We debounce to a single replace once
  // typing settles.
  //
  // Dedup via `lastSyncedQueryRef` — we track the last query string we
  // wrote and skip redundant `router.replace` calls. We deliberately do
  // NOT compare against `searchParams.get('q')`: `useSearchParams()` is
  // updated by Next.js asynchronously after `router.replace`, so its
  // value in the effect closure can be one render behind the URL we
  // just wrote.
  //
  // Initialised to the URL's current `q` so that mounting on a page
  // already loaded with `?q=...` (refresh, shared link) doesn't fire a
  // redundant `router.replace` over the unchanged URL on first render.
  const lastSyncedQueryRef = useRef<string | null>(searchParams.get('q') || '')
  useEffect(() => {
    const serialized = serializeFilter(parsedFilter)
    if (lastSyncedQueryRef.current === serialized) return
    const timer = setTimeout(() => {
      lastSyncedQueryRef.current = serialized
      const url = serialized
        ? `/workspaces?q=${encodeURIComponent(serialized)}`
        : '/workspaces'
      router.replace(url, { scroll: false })
    }, 250)
    return () => clearTimeout(timer)
  }, [parsedFilter, router])

  const filteredWorkspaces = useMemo(() => {
    if (parsedFilter.terms.length === 0) return workspaces
    return workspaces.filter(ws => {
      // Status terms need the resolved status; pass it conditionally so
      // workspaces without a status (— display) don't accidentally match
      // an empty `status:` predicate.
      return matchWorkspace(ws, parsedFilter, resolveStatus(ws).def?.filter ?? undefined)
    })
  }, [workspaces, parsedFilter])

  // Create form
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [newExecMode, setNewExecMode] = useState('local')
  const [newAutoApply, setNewAutoApply] = useState(false)
  const [newBackend, setNewBackend] = useState('tofu')
  const [newVersion, setNewVersion] = useState('1.11')
  const [newCpu, setNewCpu] = useState('1')
  const [newMemory, setNewMemory] = useState('2Gi')
  const [newWorkingDir, setNewWorkingDir] = useState('')
  const [newVcsConnectionId, setNewVcsConnectionId] = useState('')
  const [newVcsRepoUrl, setNewVcsRepoUrl] = useState('')
  const [newVcsBranch, setNewVcsBranch] = useState('')
  const [newAgentPoolId, setNewAgentPoolId] = useState('')
  const [creating, setCreating] = useState(false)

  // Version suggestions
  const [versionSuggestions, setVersionSuggestions] = useState<string[]>([])
  const [versionsBackend, setVersionsBackend] = useState('')

  // VCS connections
  const [vcsConnections, setVcsConnections] = useState<{ id: string; attributes: { name: string; provider: string } }[]>([])
  const [vcsConnectionsLoaded, setVcsConnectionsLoaded] = useState(false)

  // Agent pools
  const [agentPools, setAgentPools] = useState<{ id: string; attributes: { name: string } }[]>([])
  const [agentPoolsLoaded, setAgentPoolsLoaded] = useState(false)

  type WsSortKey = 'name' | 'mode' | 'pool' | 'resources' | 'status' | 'created'

  // resolveStatus + WORKSPACE_STATUSES are imported from workspace-status.ts
  // — single source of truth for both the row pill and the filter dropdown.

  // Per-status counts on the unfiltered workspace list, memoised so the
  // dropdown render is a constant lookup. Recomputes only when the
  // workspace data changes.
  const statusCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const ws of workspaces) {
      const f = resolveStatus(ws).def?.filter
      if (f) counts[f] = (counts[f] || 0) + 1
    }
    return counts
  }, [workspaces])

  // "Health issues" is ONE control rendered two ways (#719): the desktop
  // stat-grid card and the compact chip on the mobile filter row. Compute the
  // count + toggle once so both forms stay in sync.
  const withConditions = useMemo(
    () => workspaces.filter(ws => (ws.attributes['health-conditions'] || []).length > 0).length,
    [workspaces],
  )
  const healthFilterActive = hasStatusTerm(parsedFilter, HEALTH_ISSUE_STATUS)
  const toggleHealth = () =>
    setFilterInput(serializeFilter(toggleStatusTerm(parsedFilter, HEALTH_ISSUE_STATUS)))
  const lockedFilterActive = hasStatusTerm(parsedFilter, LOCKED_STATUS)
  const toggleLocked = () =>
    setFilterInput(serializeFilter(toggleStatusTerm(parsedFilter, LOCKED_STATUS)))

  // Distinct labels across the unfiltered list: key → sorted unique values
  // with workspace counts. Drives the two-level "+ Label" picker. Computed
  // once per workspace-list change so the menu render stays cheap.
  const labelIndex = useMemo(() => {
    const idx = new Map<string, Map<string, number>>()
    for (const ws of workspaces) {
      const labels = ws.attributes.labels || {}
      for (const [k, v] of Object.entries(labels)) {
        let values = idx.get(k)
        if (!values) {
          values = new Map<string, number>()
          idx.set(k, values)
        }
        values.set(v, (values.get(v) || 0) + 1)
      }
    }
    return idx
  }, [workspaces])
  const sortedLabelKeys = useMemo(
    () => Array.from(labelIndex.keys()).sort(),
    [labelIndex],
  )

  // The token being typed = text after the last space (filter tokens are
  // space-separated). Suggestions are computed against just that token, so
  // earlier chips in the box are left untouched.
  const currentToken = useMemo(() => {
    const parts = filterInput.split(' ')
    return parts[parts.length - 1]
  }, [filterInput])

  interface FilterSuggestion {
    kind: 'label' | 'label-key' | 'status' | 'name'
    insert: string
    display: string
    hint: string
    count?: number
    dot?: string
  }

  const suggestions = useMemo<FilterSuggestion[]>(() => {
    const tok = currentToken.trim()
    if (!tok) return []
    const lower = tok.toLowerCase()
    const out: FilterSuggestion[] = []
    // Aggregate health-issues count for the `status:unhealthy` suggestion.
    const unhealthyCount = workspaces.filter(
      ws => (ws.attributes['health-conditions'] || []).length > 0,
    ).length
    const sepIdx = tok.search(/[:=]/)
    if (sepIdx >= 0) {
      // `key:partial` → suggest values for that key.
      const key = tok.slice(0, sepIdx)
      const partial = tok.slice(sepIdx + 1).toLowerCase()
      if (key === 'status') {
        for (const s of WORKSPACE_STATUSES) {
          if (!partial || s.filter.toLowerCase().includes(partial) || s.label.toLowerCase().includes(partial))
            out.push({ kind: 'status', insert: `status:${s.filter}`, display: `status:${s.filter}`, hint: 'status', dot: s.dot, count: statusCounts[s.filter] || 0 })
        }
        if (unhealthyCount > 0 && (!partial || HEALTH_ISSUE_STATUS.includes(partial) || 'health issues'.includes(partial)))
          out.push({ kind: 'status', insert: `status:${HEALTH_ISSUE_STATUS}`, display: `status:${HEALTH_ISSUE_STATUS}`, hint: 'health', dot: 'bg-red-400', count: unhealthyCount })
      } else {
        const values = labelIndex.get(key)
        if (values) {
          for (const [v, c] of values) {
            if (!partial || v.toLowerCase().includes(partial))
              out.push({ kind: 'label', insert: `${key}:${v}`, display: `${key}:${v}`, hint: 'label', count: c })
          }
        }
      }
    } else {
      // Bare token → match label keys (drill), key:value pairs, statuses, names.
      for (const k of sortedLabelKeys) {
        if (k.toLowerCase().includes(lower))
          out.push({ kind: 'label-key', insert: `${k}:`, display: `${k}:`, hint: 'label key' })
      }
      for (const k of sortedLabelKeys) {
        for (const [v, c] of labelIndex.get(k)!) {
          if (v.toLowerCase().includes(lower) || k.toLowerCase().includes(lower))
            out.push({ kind: 'label', insert: `${k}:${v}`, display: `${k}:${v}`, hint: 'label', count: c })
        }
      }
      for (const s of WORKSPACE_STATUSES) {
        if (s.filter.toLowerCase().includes(lower) || s.label.toLowerCase().includes(lower))
          out.push({ kind: 'status', insert: `status:${s.filter}`, display: `status:${s.filter}`, hint: 'status', dot: s.dot, count: statusCounts[s.filter] || 0 })
      }
      if (unhealthyCount > 0 && (HEALTH_ISSUE_STATUS.includes(lower) || 'health'.includes(lower) || 'issues'.includes(lower)))
        out.push({ kind: 'status', insert: `status:${HEALTH_ISSUE_STATUS}`, display: `status:${HEALTH_ISSUE_STATUS}`, hint: 'health', dot: 'bg-red-400', count: unhealthyCount })
      for (const ws of workspaces) {
        const n = ws.attributes.name
        if (n.toLowerCase().includes(lower))
          out.push({ kind: 'name', insert: n, display: n, hint: 'name' })
      }
    }
    // Dedup by inserted text; cap so the menu stays usable.
    const seen = new Set<string>()
    return out.filter(s => (seen.has(s.insert) ? false : (seen.add(s.insert), true))).slice(0, 12)
  }, [currentToken, sortedLabelKeys, labelIndex, statusCounts, workspaces])

  function applyFilterSuggestion(s: FilterSuggestion) {
    const parts = filterInput.split(' ')
    parts[parts.length - 1] = s.insert
    // A label-key suggestion (`env:`) leaves the cursor mid-token so you keep
    // typing/picking the value; everything else completes the chip + a space.
    const drill = s.kind === 'label-key'
    setFilterInput(parts.join(' ') + (drill ? '' : ' '))
    setSuggestIndex(-1)
    setSuggestOpen(drill)
    filterInputRef.current?.focus()
  }

  function onFilterKeyDown(e: ReactKeyboardEvent<HTMLInputElement>) {
    if (e.key === 'ArrowDown' && (!suggestOpen || suggestions.length === 0)) {
      if (currentToken.trim()) { setSuggestOpen(true); setSuggestIndex(0) }
      return
    }
    if (!suggestOpen || suggestions.length === 0) return
    if (e.key === 'ArrowDown') { e.preventDefault(); setSuggestIndex(i => (i + 1) % suggestions.length) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSuggestIndex(i => (i <= 0 ? suggestions.length - 1 : i - 1)) }
    else if (e.key === 'Enter') {
      // Apply ONLY a suggestion the user explicitly highlighted with the
      // arrow keys. A bare Enter (nothing highlighted) keeps the typed text.
      if (suggestIndex >= 0) { e.preventDefault(); applyFilterSuggestion(suggestions[suggestIndex]); }
      else setSuggestOpen(false)
    }
    else if (e.key === 'Escape') { e.preventDefault(); setSuggestOpen(false) }
  }

  const { sortedItems: sortedWorkspaces, sortState, toggleSort } = useSortable<Workspace, WsSortKey>(
    filteredWorkspaces, 'name', 'asc',
    useCallback((item: Workspace, key: WsSortKey) => {
      switch (key) {
        case 'name': return item.attributes.name
        case 'mode': return item.attributes['execution-mode']
        case 'pool': return item.attributes['agent-pool-name'] || ''
        case 'resources': return item.attributes['resource-cpu']
        case 'status': return resolveStatus(item).def?.label ?? '\u2014'
        case 'created': return item.attributes['created-at']
      }
    }, []),
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadWorkspaces()
  }, [router])

  // Real-time workspace list updates via SSE. Always-on (not gated on
  // workspaces.length) so the FIRST workspace created from another tab / the
  // CLI / a VCS push live-appears on an empty org instead of requiring a
  // manual refresh — the empty state is exactly where live-update matters most.
  const { connected: sseConnected } = useWorkspaceListEvents(true, useCallback(() => {
    loadWorkspaces()
  }, []))

  // Load VCS connections and agent pools when form opens
  useEffect(() => {
    if (!showCreate) return
    if (!vcsConnectionsLoaded) {
      apiFetch('/api/terrapod/v1/vcs-connections')
        .then(res => res.ok ? res.json() : { data: [] })
        .then(data => { setVcsConnections(data.data || []); setVcsConnectionsLoaded(true) })
        .catch(() => {})
    }
    if (!agentPoolsLoaded) {
      apiFetch('/api/terrapod/v1/agent-pools')
        .then(res => res.ok ? res.json() : { data: [] })
        .then(data => { setAgentPools(data.data || []); setAgentPoolsLoaded(true) })
        .catch(() => {})
    }
  }, [showCreate, vcsConnectionsLoaded, agentPoolsLoaded])

  // Fetch version suggestions when backend changes and form is open
  useEffect(() => {
    if (!showCreate || newBackend === versionsBackend) return
    apiFetch(`/api/terrapod/v1/binary-cache/versions?tool=${newBackend}`)
      .then(res => res.ok ? res.json() : { data: [] })
      .then(data => {
        setVersionSuggestions(data.data || [])
        setVersionsBackend(newBackend)
      })
      .catch(() => {})
  }, [showCreate, newBackend, versionsBackend])

  async function loadWorkspaces() {
    try {
      const res = await apiFetch('/api/v2/organizations/default/workspaces')
      if (!res.ok) throw new Error(t('loadFailed'))
      const data = await res.json()
      setWorkspaces(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : t('loadFailed'))
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    try {
      const res = await apiFetch('/api/v2/organizations/default/workspaces', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'workspaces',
            attributes: {
              name: newName,
              'execution-mode': newExecMode,
              'execution-backend': newBackend,
              'terraform-version': newVersion,
              'auto-apply': newAutoApply,
              'resource-cpu': newCpu,
              'resource-memory': newMemory,
              'working-directory': newWorkingDir,
              'vcs-repo-url': newVcsRepoUrl,
              'vcs-branch': newVcsBranch,
              ...(newAgentPoolId ? { 'agent-pool-id': newAgentPoolId } : {}),
            },
            ...(newVcsConnectionId ? {
              relationships: {
                'vcs-connection': {
                  data: { id: newVcsConnectionId, type: 'vcs-connections' },
                },
              },
            } : {}),
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `${t('createFailed')} (${res.status})`)
      }
      setNewName('')
      setNewExecMode('local')
      setNewBackend('tofu')
      setNewVersion('1.11')
      setNewAutoApply(false)
      setNewCpu('1')
      setNewMemory('2Gi')
      setNewWorkingDir('')
      setNewVcsConnectionId('')
      setNewVcsRepoUrl('')
      setNewVcsBranch('')
      setNewAgentPoolId('')
      setShowCreate(false)
      await loadWorkspaces()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('createFailed'))
    } finally {
      setCreating(false)
    }
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={t('title')}
          description={t('description')}
          actions={
            <div className="flex items-center gap-3">
              <ConnectionStatus connected={sseConnected} />
              <button
                onClick={() => setShowCreate(!showCreate)}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
              >
                {showCreate ? t('cancel') : t('new')}
              </button>
            </div>
          }
        />

        {error && <ErrorBanner message={error} />}

        {showCreate && (
          <form onSubmit={handleCreate} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              <div>
                <label htmlFor="ws-name" className="block text-sm font-medium text-slate-300 mb-1">{t('form.name')}</label>
                <input
                  id="ws-name"
                  type="text"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  required
                  pattern="[a-zA-Z0-9][a-zA-Z0-9_\-]*"
                  title={t('form.namePattern')}
                  placeholder={t('form.namePlaceholder')}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
              <div>
                <label htmlFor="ws-exec" className="block text-sm font-medium text-slate-300 mb-1">{t('form.executionMode')}</label>
                <select
                  id="ws-exec"
                  value={newExecMode}
                  onChange={(e) => setNewExecMode(e.target.value)}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                >
                  <option value="local">{t('form.local')}</option>
                  <option value="agent">{t('form.agent')}</option>
                </select>
              </div>
              <div>
                <label htmlFor="ws-backend" className="block text-sm font-medium text-slate-300 mb-1">{t('form.executionBackend')}</label>
                <select
                  id="ws-backend"
                  value={newBackend}
                  onChange={(e) => setNewBackend(e.target.value)}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                >
                  <option value="tofu">OpenTofu</option>
                  <option value="terraform">Terraform</option>
                </select>
              </div>
              <div>
                <label htmlFor="ws-version" className="block text-sm font-medium text-slate-300 mb-1">{t('form.version')}</label>
                <input
                  id="ws-version"
                  type="text"
                  list="version-suggestions"
                  value={newVersion}
                  onChange={(e) => setNewVersion(e.target.value)}
                  pattern="[0-9]+\.[0-9]+(\.[0-9]+)?"
                  title={t('form.versionTitle')}
                  placeholder={t('form.versionPlaceholder')}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
                <datalist id="version-suggestions">
                  {versionSuggestions.map(v => (
                    <option key={v} value={v} />
                  ))}
                </datalist>
              </div>
              <div>
                <label htmlFor="ws-cpu" className="block text-sm font-medium text-slate-300 mb-1">{t('form.cpuRequest')}</label>
                <input
                  id="ws-cpu"
                  type="text"
                  value={newCpu}
                  onChange={(e) => setNewCpu(e.target.value)}
                  pattern="[0-9]+m|[0-9]+(\.[0-9]+)?"
                  title={t('form.cpuTitle')}
                  placeholder="1"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
                <p className="mt-1 text-xs text-slate-500">{t('form.cpuHint')}</p>
              </div>
              <div>
                <label htmlFor="ws-mem" className="block text-sm font-medium text-slate-300 mb-1">{t('form.memoryRequest')}</label>
                <input
                  id="ws-mem"
                  type="text"
                  value={newMemory}
                  onChange={(e) => setNewMemory(e.target.value)}
                  pattern="[0-9]+(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E|m)?"
                  title={t('form.memoryTitle')}
                  placeholder="2Gi"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
                <p className="mt-1 text-xs text-slate-500">{t('form.memoryHint')}</p>
              </div>
              {newExecMode === 'agent' && (
              <div>
                <label htmlFor="ws-pool" className="block text-sm font-medium text-slate-300 mb-1">{t('form.agentPool')}</label>
                <select
                  id="ws-pool"
                  value={newAgentPoolId}
                  onChange={(e) => setNewAgentPoolId(e.target.value)}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                >
                  <option value="">{t('form.none')}</option>
                  {agentPools.map((p) => (
                    <option key={p.id} value={p.id}>{p.attributes.name}</option>
                  ))}
                </select>
              </div>
              )}
              <div>
                <span className="block text-sm font-medium text-slate-300 mb-1">{t('form.autoApply')}</span>
                <label className="flex items-center gap-2 h-[42px] px-3 cursor-pointer border border-slate-600 rounded-lg bg-slate-700">
                  <input
                    type="checkbox"
                    checked={newAutoApply}
                    onChange={(e) => setNewAutoApply(e.target.checked)}
                    className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
                  />
                  <span className="text-sm text-slate-300">{t('form.enabled')}</span>
                </label>
              </div>
              <div>
                <label htmlFor="ws-workdir" className="block text-sm font-medium text-slate-300 mb-1">{t('form.workingDirectory')}</label>
                <input
                  id="ws-workdir"
                  type="text"
                  value={newWorkingDir}
                  onChange={(e) => setNewWorkingDir(e.target.value)}
                  placeholder={t('form.workingDirectoryPlaceholder')}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
                <p className="mt-1 text-xs text-slate-500">{t('form.workingDirectoryHint')}</p>
              </div>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              <div>
                <label htmlFor="ws-vcs-conn" className="block text-sm font-medium text-slate-300 mb-1">{t('form.vcsConnection')}</label>
                <select
                  id="ws-vcs-conn"
                  value={newVcsConnectionId}
                  onChange={(e) => setNewVcsConnectionId(e.target.value)}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                >
                  <option value="">{t('form.none')}</option>
                  {vcsConnections.map((c) => (
                    <option key={c.id} value={c.id}>{c.attributes.name} ({c.attributes.provider})</option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="ws-vcs-repo" className="block text-sm font-medium text-slate-300 mb-1">{t('form.vcsRepoUrl')}</label>
                <input
                  id="ws-vcs-repo"
                  type="text"
                  value={newVcsRepoUrl}
                  onChange={(e) => setNewVcsRepoUrl(e.target.value)}
                  pattern="https?://.+"
                  title={t('form.vcsRepoUrlTitle')}
                  placeholder={t('form.vcsRepoUrlPlaceholder')}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
              <div>
                <label htmlFor="ws-vcs-branch" className="block text-sm font-medium text-slate-300 mb-1">{t('form.vcsBranch')}</label>
                <input
                  id="ws-vcs-branch"
                  type="text"
                  value={newVcsBranch}
                  onChange={(e) => setNewVcsBranch(e.target.value)}
                  placeholder={t('form.vcsBranchPlaceholder')}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
            </div>
            <button
              type="submit"
              disabled={creating}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
            >
              {creating ? t('creating') : t('create')}
            </button>
          </form>
        )}

        {!loading && workspaces.length > 0 && (() => {
          const total = workspaces.length
          const locked = workspaces.filter(ws => ws.attributes.locked).length
          // `unhealthy` is driven by the Health chip, not the Status dropdown,
          // so it doesn't count toward the dropdown's active badge (which would
          // otherwise show a count with no matching checked row inside).
          const activeStatusCount = parsedFilter.terms.filter(
            t => t.kind === 'status' && t.value !== HEALTH_ISSUE_STATUS && t.value !== LOCKED_STATUS,
          ).length
          const healthLabel = healthFilterActive
            ? t('stats.clearHealthFilter')
            : t('stats.filterHealth')
          return (
            <div className="mb-4">
              {/* Compact toolbar (#719): stat chips + Status/Label share one
                  row; the filter input drops to its own row (basis-full) via
                  flex `order`. Total/Locked are desktop-only secondary counts;
                  Health is always shown (primary signal) and toggles the
                  `status:unhealthy` filter. No big stat cards on any viewport. */}
              <div className="flex flex-wrap items-center gap-2">
                <StatChip
                  label={t('stats.total')}
                  value={total}
                  onClick={parsedFilter.terms.length > 0 ? () => setFilterInput('') : undefined}
                  ariaLabel={t('stats.clearAllFilters')}
                  className="order-1 max-sm:hidden"
                />
                <StatChip
                  label={t('stats.health')}
                  value={withConditions}
                  valueClassName={withConditions > 0 ? 'text-red-400' : 'text-slate-300'}
                  onClick={withConditions > 0 ? toggleHealth : undefined}
                  active={healthFilterActive}
                  ariaLabel={healthLabel}
                  className="order-2"
                />
                <StatChip
                  label={t('stats.locked')}
                  value={locked}
                  valueClassName={locked > 0 ? 'text-amber-400' : undefined}
                  onClick={locked > 0 ? toggleLocked : undefined}
                  active={lockedFilterActive}
                  activeClassName="bg-amber-500/10 border-amber-500/50"
                  ariaLabel={lockedFilterActive ? t('stats.clearLockedFilter') : t('stats.filterLocked')}
                  className="order-3 max-sm:hidden"
                />
                <div className="order-4 flex-1 min-w-0" />
                {/* Input + Clear share their own full-width row (order-7),
                    Clear pinned to the right of the input — desktop and mobile. */}
                <div className="order-7 basis-full flex items-center gap-2">
                  <div className="relative flex-1 min-w-0" ref={suggestWrapRef}>
                  <input
                    type="text"
                    value={filterInput}
                    onChange={e => { setFilterInput(e.target.value); setSuggestOpen(true); setSuggestIndex(-1) }}
                    onFocus={() => { if (currentToken.trim()) setSuggestOpen(true) }}
                    onKeyDown={onFilterKeyDown}
                    ref={filterInputRef}
                    placeholder={t('filter.placeholder')}
                    aria-label={t('filter.ariaLabel')}
                    role="combobox"
                    aria-expanded={suggestOpen && suggestions.length > 0}
                    aria-autocomplete="list"
                    autoComplete="off"
                    className="w-full px-3 py-2 rounded-lg bg-slate-800/50 border border-slate-700/50 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-brand-500"
                  />
                  {suggestOpen && suggestions.length > 0 && (
                    <div
                      role="listbox"
                      data-testid="filter-suggestions"
                      className="absolute left-0 right-0 z-20 mt-1 rounded-lg bg-slate-800 border border-slate-700 shadow-xl py-1 max-h-72 overflow-y-auto"
                    >
                      {suggestions.map((s, i) => (
                        <button
                          key={`${s.kind}:${s.insert}`}
                          type="button"
                          role="option"
                          aria-selected={i === suggestIndex}
                          // onMouseDown (not onClick) so it fires before the input blur closes the menu.
                          // Clicking applies THIS suggestion directly (independent of the
                          // keyboard-highlight index), so a hover never hijacks a bare Enter.
                          onMouseDown={e => { e.preventDefault(); applyFilterSuggestion(s) }}
                          className={
                            'w-full flex items-center gap-2 px-3 py-1.5 text-sm transition-colors text-left border-l-2 ' +
                            (i === suggestIndex
                              ? 'bg-brand-600/40 text-white border-brand-300 font-semibold ring-1 ring-inset ring-brand-400/60'
                              : 'border-transparent text-slate-300 hover:bg-slate-700/40')
                          }
                        >
                          {s.dot ? (
                            <span className={'w-1.5 h-1.5 rounded-full ' + s.dot} />
                          ) : (
                            <span className="w-1.5" />
                          )}
                          <span className="flex-1 font-mono text-xs">{s.display}</span>
                          {typeof s.count === 'number' && (
                            <span className="text-[10px] text-slate-500">{s.count}</span>
                          )}
                          <span className="text-[10px] uppercase tracking-wide text-slate-500">{hintLabel(s.hint)}</span>
                        </button>
                      ))}
                    </div>
                  )}
                  </div>
                  {parsedFilter.terms.length > 0 && (
                    <button
                      type="button"
                      onClick={() => setFilterInput('')}
                      className="px-3 py-2 rounded-lg text-sm text-slate-400 hover:text-slate-200 transition-colors whitespace-nowrap flex-shrink-0"
                    >
                      {t('filter.clear')}
                    </button>
                  )}
                </div>
                {/* Status dropdown — single entry point for all status presets.
                    Picks any combination via toggle; the existing chips below
                    show what's active and let the user remove individually. */}
                <div className="relative order-5" ref={statusMenuRef}>
                  <button
                    type="button"
                    aria-haspopup="menu"
                    aria-expanded={statusMenuOpen}
                    onClick={() => setStatusMenuOpen(o => !o)}
                    className={
                      'inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium border transition-colors ' +
                      (activeStatusCount > 0
                        ? 'bg-slate-700/60 text-slate-100 border-slate-600'
                        : 'bg-slate-800/50 text-slate-300 border-slate-700/50 hover:bg-slate-700/60')
                    }
                  >
                    <span>{t('filter.status')}</span>
                    {activeStatusCount > 0 && (
                      <span className="inline-flex items-center justify-center min-w-5 px-1.5 rounded-full text-[10px] font-semibold bg-brand-600 text-white">
                        {activeStatusCount}
                      </span>
                    )}
                    <svg className={'w-3 h-3 transition-transform ' + (statusMenuOpen ? 'rotate-180' : '')} viewBox="0 0 12 12" fill="currentColor" aria-hidden>
                      <path d="M3 4.5l3 3 3-3" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </button>
                  {statusMenuOpen && (
                    <div
                      role="menu"
                      className="absolute right-0 z-10 mt-1 w-64 rounded-lg bg-slate-800 border border-slate-700 shadow-xl py-1 max-h-96 overflow-y-auto"
                    >
                      {WORKSPACE_STATUSES.map(opt => {
                        const active = hasStatusTerm(parsedFilter, opt.filter)
                        const count = statusCounts[opt.filter] || 0
                        return (
                          <button
                            key={opt.filter}
                            type="button"
                            role="menuitemcheckbox"
                            aria-checked={active}
                            onClick={() => setFilterInput(serializeFilter(toggleStatusTerm(parsedFilter, opt.filter)))}
                            className={
                              'w-full flex items-center gap-2 px-3 py-1.5 text-sm transition-colors ' +
                              (active ? 'bg-slate-700/60 text-slate-100' : 'text-slate-300 hover:bg-slate-700/40')
                            }
                          >
                            {/* Checkmark rail keeps the option labels aligned whether
                                checked or not. */}
                            <span className="w-3 inline-flex justify-center text-brand-400">
                              {active ? '✓' : ''}
                            </span>
                            <span className={'w-1.5 h-1.5 rounded-full ' + opt.dot} />
                            <span className="flex-1 text-left">{ts(opt.filter)}</span>
                            <span className={'text-xs ' + (count > 0 ? 'text-slate-400' : 'text-slate-600')}>{count}</span>
                          </button>
                        )
                      })}
                    </div>
                  )}
                </div>
                {/* Label dropdown — two-level picker for arbitrary label
                    key/value pairs. First level lists distinct label keys
                    in the visible workspaces; clicking a key drills into a
                    second menu of distinct values for that key. */}
                <div className="relative order-6" ref={labelMenuRef}>
                  {(() => {
                    const activeLabelCount = parsedFilter.terms.filter(t => t.kind === 'label' && t.value !== null).length
                    return (
                      <>
                        <button
                          type="button"
                          aria-haspopup="menu"
                          aria-expanded={labelMenuOpen}
                          onClick={() => {
                            setLabelMenuOpen(o => !o)
                            setLabelMenuKey(null)
                          }}
                          disabled={sortedLabelKeys.length === 0}
                          className={
                            'inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium border transition-colors disabled:opacity-50 disabled:cursor-not-allowed ' +
                            (activeLabelCount > 0
                              ? 'bg-slate-700/60 text-slate-100 border-slate-600'
                              : 'bg-slate-800/50 text-slate-300 border-slate-700/50 hover:bg-slate-700/60')
                          }
                        >
                          <span>{t('filter.label')}</span>
                          {activeLabelCount > 0 && (
                            <span className="inline-flex items-center justify-center min-w-5 px-1.5 rounded-full text-[10px] font-semibold bg-brand-600 text-white">
                              {activeLabelCount}
                            </span>
                          )}
                          <svg className={'w-3 h-3 transition-transform ' + (labelMenuOpen ? 'rotate-180' : '')} viewBox="0 0 12 12" fill="currentColor" aria-hidden>
                            <path d="M3 4.5l3 3 3-3" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                        </button>
                        {labelMenuOpen && labelMenuKey === null && (
                          <div role="menu" className="absolute right-0 z-10 mt-1 w-64 rounded-lg bg-slate-800 border border-slate-700 shadow-xl py-1 max-h-96 overflow-y-auto">
                            {sortedLabelKeys.map(k => (
                              <button
                                key={k}
                                type="button"
                                role="menuitem"
                                onClick={() => setLabelMenuKey(k)}
                                className="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-700/40 transition-colors"
                              >
                                <span className="flex-1 text-left font-mono text-xs">{k}</span>
                                <span className="text-xs text-slate-500">{t('filter.valueCount', { count: labelIndex.get(k)?.size || 0 })}</span>
                                <span aria-hidden className="text-slate-500">›</span>
                              </button>
                            ))}
                          </div>
                        )}
                        {labelMenuOpen && labelMenuKey !== null && (
                          <div role="menu" className="absolute right-0 z-10 mt-1 w-64 rounded-lg bg-slate-800 border border-slate-700 shadow-xl py-1 max-h-96 overflow-y-auto">
                            <button
                              type="button"
                              onClick={() => setLabelMenuKey(null)}
                              className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-slate-400 hover:bg-slate-700/40 transition-colors border-b border-slate-700/50"
                            >
                              <span aria-hidden>‹</span>
                              <span>{t('filter.back')}</span>
                              <span className="ml-auto font-mono text-slate-500">{labelMenuKey}</span>
                            </button>
                            {Array.from(labelIndex.get(labelMenuKey)?.entries() || [])
                              .sort(([a], [b]) => a.localeCompare(b))
                              .map(([v, count]) => {
                                const active = hasLabelTerm(parsedFilter, labelMenuKey, v)
                                return (
                                  <button
                                    key={v}
                                    type="button"
                                    role="menuitemcheckbox"
                                    aria-checked={active}
                                    onClick={() => {
                                      setFilterInput(serializeFilter(toggleLabelTerm(parsedFilter, labelMenuKey, v)))
                                    }}
                                    className={
                                      'w-full flex items-center gap-2 px-3 py-1.5 text-sm transition-colors ' +
                                      (active ? 'bg-slate-700/60 text-slate-100' : 'text-slate-300 hover:bg-slate-700/40')
                                    }
                                  >
                                    <span className="w-3 inline-flex justify-center text-brand-400">{active ? '✓' : ''}</span>
                                    <span className="flex-1 text-left font-mono text-xs">{v}</span>
                                    <span className="text-xs text-slate-500">{count}</span>
                                  </button>
                                )
                              })}
                          </div>
                        )}
                      </>
                    )
                  })()}
                </div>
              </div>
              {parsedFilter.terms.length > 0 && (
                <div className="flex flex-wrap gap-2 mt-2">
                  {parsedFilter.terms.map((term, i) => {
                    const label =
                      term.kind === 'name'
                        ? t('filter.termName', { value: term.value })
                        : term.kind === 'status'
                          ? t('filter.termStatus', { value: term.value })
                          : term.value === null
                            ? t('filter.termLabelAny', { key: term.key })
                            : t('filter.termLabel', { key: term.key, value: term.value })
                    return (
                      <button
                        type="button"
                        key={`${i}-${label}`}
                        onClick={() => setFilterInput(serializeFilter(removeTerm(parsedFilter, i)))}
                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-slate-700/50 text-slate-300 hover:bg-slate-700 transition-colors"
                        title={t('filter.removeTerm')}
                      >
                        <span>{label}</span>
                        <span aria-hidden className="text-slate-500">×</span>
                      </button>
                    )
                  })}
                  <span className="text-xs text-slate-500 self-center">
                    {t('filter.showing', { filtered: filteredWorkspaces.length, total: workspaces.length })}
                  </span>
                </div>
              )}
            </div>
          )
        })()}

        {loading ? (
          <LoadingSpinner />
        ) : workspaces.length === 0 ? (
          <EmptyState message={t('empty.none')} />
        ) : filteredWorkspaces.length === 0 ? (
          <EmptyState message={t('empty.noMatch')} />
        ) : (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <SortableHeader label={t('table.name')} sortKey="name" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label={t('table.mode')} sortKey="mode" sortState={sortState} onSort={toggleSort} className="hidden sm:table-cell" />
                  <SortableHeader label={t('table.pool')} sortKey="pool" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
                  <SortableHeader label={t('table.resources')} sortKey="resources" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                  <SortableHeader label={t('table.status')} sortKey="status" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                  <SortableHeader label={t('table.created')} sortKey="created" sortState={sortState} onSort={toggleSort} className="hidden xl:table-cell" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700/30">
                {sortedWorkspaces.map((ws) => {
                  const { def, runId } = resolveStatus(ws)
                  return (
                  <tr key={ws.id} className="hover:bg-slate-700/20 transition-colors">
                    <td className="px-4 py-3">
                      <div className="flex flex-col gap-1.5">
                        <Link
                          href={`/workspaces/${ws.id}`}
                          className="text-sm font-medium text-brand-400 hover:text-brand-300 break-all"
                        >
                          {ws.attributes.name}
                        </Link>
                        {ws.attributes.labels && Object.keys(ws.attributes.labels).length > 0 && (
                          <div className="flex flex-wrap gap-1">
                            {Object.entries(ws.attributes.labels).map(([k, v]) => {
                              const active = hasLabelTerm(parsedFilter, k, v)
                              return (
                                <button
                                  key={`${k}=${v}`}
                                  type="button"
                                  onClick={() => setFilterInput(serializeFilter(toggleLabelTerm(parsedFilter, k, v)))}
                                  className={
                                    'inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-mono transition-colors ' +
                                    (active
                                      ? 'bg-brand-700/60 text-brand-100 hover:bg-brand-700'
                                      : 'bg-slate-700/40 text-slate-400 hover:bg-slate-700/80 hover:text-slate-200')
                                  }
                                  title={active ? t('row.removeLabelFilter') : t('row.addLabelFilter')}
                                >
                                  <span className="text-slate-500">{k}:</span>
                                  <span className="ml-0.5">{v}</span>
                                </button>
                              )
                            })}
                          </div>
                        )}
                        {/*
                          Mobile-only status (#719). Below `lg` the STATUS
                          column is hidden, so surface the same badges here
                          where the row is name-only — a phone must never lose
                          the running/errored/applied signal. Hidden at `lg`+
                          where the dedicated STATUS column takes over, so
                          desktop is pixel-identical.
                        */}
                        <div className="lg:hidden" data-testid="ws-row-status-mobile">
                          <WorkspaceStatusBadges
                            workspaceId={ws.id}
                            def={def}
                            runId={runId}
                            lifecycleState={ws.attributes['lifecycle-state']}
                          />
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-3 hidden sm:table-cell">
                      <span className="text-xs text-slate-400">{ws.attributes['execution-mode']}</span>
                    </td>
                    <td className="px-4 py-3 hidden md:table-cell">
                      <span className="text-xs text-slate-400">
                        {ws.attributes['agent-pool-name'] || '\u2014'}
                      </span>
                    </td>
                    <td className="px-4 py-3 hidden lg:table-cell">
                      <span className="text-xs text-slate-400">
                        {t('table.resourcesValue', { cpu: ws.attributes['resource-cpu'], memory: ws.attributes['resource-memory'] })}
                      </span>
                    </td>
                    <td className="px-4 py-3 hidden lg:table-cell">
                      <WorkspaceStatusBadges
                        workspaceId={ws.id}
                        def={def}
                        runId={runId}
                        lifecycleState={ws.attributes['lifecycle-state']}
                      />
                    </td>
                    <td className="px-4 py-3 hidden xl:table-cell">
                      <span className="text-xs text-slate-500">
                        {fmt.date(ws.attributes['created-at'])}
                      </span>
                    </td>
                  </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </>
  )
}

export default function WorkspacesPage() {
  return (
    <Suspense fallback={<><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>}>
      <WorkspacesPageInner />
    </Suspense>
  )
}
