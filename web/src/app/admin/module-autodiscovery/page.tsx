'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import { useFormatter, useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { LabelsEditor } from '@/components/labels-editor'
import { HealthConditions } from '@/components/health-conditions'
import { MobileCard, MobileCardList } from '@/components/mobile-card-list'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch, fetchAllPages, parseApiError } from '@/lib/api'
import { useSortable } from '@/lib/use-sortable'
import { useIsTouch } from '@/lib/use-media-query'

// Module autodiscovery rules (#1584, #1620): find the modules in a repository,
// or in every repository of an org, group or name pattern — the root and any
// submodules — and register them. Contract:
// services/terrapod/api/routers/module_autodiscovery_rules.py.

type TargetKind = 'repository' | 'namespace' | 'pattern'

interface ModuleRule {
  id: string
  attributes: {
    name: string
    'vcs-connection-id': string
    'repo-url': string
    'target-kind'?: TargetKind
    branch: string
    pattern: string
    'ignore-patterns': string[]
    enabled: boolean
    'name-template': string
    provider: string
    'vcs-tag-pattern': string
    labels: Record<string, string>
    'owner-email': string
    'last-enumerated-at'?: string | null
    'last-error'?: string
    'created-at': string
  }
}

interface VCSConnection {
  id: string
  attributes: { name: string; provider: string }
}

interface Candidate {
  repository?: string
  'repo-url'?: string
  subdirectory: string
  name: string
  provider: string
  'registered-as': { name: string; provider: string } | null
  collision: boolean
  'missing-provider': boolean
}

interface PreviewRepository {
  repository: string
  'repo-url': string
  ref: string
  status: string
  origin?: string
  error: string
}

interface PageMeta {
  'current-page': number
  'page-size': number
  'total-count': number
  'total-pages': number
}

// Where a preview came from, so another page can be fetched the same way.
type PreviewSource =
  | { saved: true; ruleId: string }
  | { saved: false; attributes: Record<string, unknown> }

interface PreviewState {
  ruleId: string // '' for an unsaved rule
  ruleName: string
  source: PreviewSource
  page: number
  loading?: boolean
  error?: string
  kind?: TargetKind
  ref?: string
  filesWalked?: number
  entries?: Candidate[]
  repositories?: PreviewRepository[]
  complete?: boolean
  meta?: PageMeta | null
  // Candidate keys (see keyOf), kept across pages of an org-wide preview.
  selected: string[]
  registering?: boolean
}

interface RepositoryRow {
  id: string
  attributes: {
    repository: string
    'repo-url': string
    origin: string
    status: string
    candidates: { subdirectory: string; name: string; provider: string }[]
    'previous-paths': { path: string; url: string }[]
    'last-checked-at': string | null
    'last-error': string
  }
}

interface RepositoriesState {
  ruleId: string
  ruleName: string
  kind: TargetKind
  lastEnumeratedAt: string | null
  status: string
  page: number
  loading?: boolean
  error?: string
  items?: RepositoryRow[]
  meta?: PageMeta | null
}

type SortKey = 'name' | 'repo' | 'pattern' | 'enabled'

const INPUT = 'w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-base sm:text-sm'
const DEFAULT_PATTERN = '**/*.tf'
const PILL = 'inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium whitespace-nowrap'
const REPO_STATUSES = ['active', 'archived', 'empty', 'no-branch', 'out-of-scope', 'covered', 'error']
const REPOSITORIES_PAGE_SIZE = 25

const STATUS_COLOURS: Record<string, string> = {
  active: 'bg-green-900/50 text-green-300',
  archived: 'bg-slate-700/50 text-slate-400',
  empty: 'bg-slate-700/50 text-slate-400',
  'no-branch': 'bg-amber-900/50 text-amber-300',
  'out-of-scope': 'bg-slate-700/50 text-slate-400',
  covered: 'bg-blue-900/50 text-blue-300',
  error: 'bg-red-900/50 text-red-300',
}

function registrable(c: Candidate): boolean {
  return !c['registered-as'] && !c.collision && !c['missing-provider']
}

// A candidate's selection key: two repositories can both have `modules/x`.
function keyOf(c: Candidate): string {
  return JSON.stringify([c.repository ?? '', c.subdirectory])
}

function splitKey(key: string): [string, string] {
  return JSON.parse(key) as [string, string]
}

function kindOf(value: unknown): TargetKind {
  return value === 'namespace' || value === 'pattern' ? value : 'repository'
}

export default function ModuleAutodiscoveryPage() {
  const router = useRouter()
  const t = useTranslations('adminModuleAutodiscovery')
  const tw = useTranslations('adminAutodiscovery')
  const td = useTranslations('registry.modules.discover')
  const format = useFormatter()
  const isTouch = useIsTouch()

  const [rules, setRules] = useState<ModuleRule[]>([])
  const [connections, setConnections] = useState<VCSConnection[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [vcsConnectionId, setVcsConnectionId] = useState('')
  const [repoUrl, setRepoUrl] = useState('')
  const [branch, setBranch] = useState('')
  const [pattern, setPattern] = useState(DEFAULT_PATTERN)
  const [ignoreText, setIgnoreText] = useState('')
  const [nameTemplate, setNameTemplate] = useState('')
  const [provider, setProvider] = useState('')
  const [tagPattern, setTagPattern] = useState('v*')
  const [labels, setLabels] = useState<Record<string, string>>({})
  const [ownerEmail, setOwnerEmail] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [submitting, setSubmitting] = useState(false)

  const [preview, setPreview] = useState<PreviewState | null>(null)
  const [repos, setRepos] = useState<RepositoriesState | null>(null)

  const accessor = useCallback((r: ModuleRule, key: SortKey) => {
    switch (key) {
      case 'name': return r.attributes.name
      case 'repo': return r.attributes['repo-url']
      case 'pattern': return r.attributes.pattern
      case 'enabled': return r.attributes.enabled ? '1' : '0'
    }
  }, [])
  const { sortedItems, sortState, toggleSort } = useSortable<ModuleRule, SortKey>(
    rules, 'name', 'asc', accessor,
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadAll()
  // eslint-disable-next-line react-hooks/exhaustive-deps -- initial mount load; the loader is a hoisted function declaration recreated each render
  }, [router])

  async function loadAll() {
    try {
      const [rulesList, connsList] = await Promise.all([
        fetchAllPages<ModuleRule>('/api/terrapod/v1/module-autodiscovery-rules'),
        fetchAllPages<VCSConnection>('/api/terrapod/v1/vcs-connections').catch(() => [] as VCSConnection[]),
      ])
      setRules(rulesList)
      setConnections(connsList)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('loadFailed'))
    } finally {
      setLoading(false)
    }
  }

  function kindLabel(kind: TargetKind): string {
    switch (kind) {
      case 'namespace': return t('targetKind.namespace')
      case 'pattern': return t('targetKind.pattern')
      default: return t('targetKind.repository')
    }
  }

  function statusLabel(status: string): string {
    switch (status) {
      case 'active': return t('repoStatus.active')
      case 'archived': return t('repoStatus.archived')
      case 'empty': return t('repoStatus.empty')
      case 'no-branch': return t('repoStatus.noBranch')
      case 'out-of-scope': return t('repoStatus.outOfScope')
      case 'covered': return t('repoStatus.covered')
      case 'error': return t('repoStatus.error')
      default: return status
    }
  }

  function originLabel(origin: string): string {
    switch (origin) {
      case 'new': return t('origin.new')
      case 'baseline': return t('origin.baseline')
      default: return origin
    }
  }

  function when(value: string | null | undefined): string {
    if (!value) return t('repositories.never')
    return format.dateTime(new Date(value), { dateStyle: 'medium', timeStyle: 'short' })
  }

  function statusPill(status: string) {
    return <span className={`${PILL} ${STATUS_COLOURS[status] ?? STATUS_COLOURS.archived}`}>{statusLabel(status)}</span>
  }

  function originPill(origin: string) {
    const colour = origin === 'new' ? 'bg-blue-900/50 text-blue-300' : 'bg-slate-700/50 text-slate-400'
    return <span className={`${PILL} ${colour}`}>{originLabel(origin)}</span>
  }

  function kindPill(kind: TargetKind) {
    const colour = kind === 'repository' ? 'bg-slate-700/50 text-slate-300' : 'bg-brand-900/50 text-brand-300'
    return <span className={`${PILL} ${colour}`}>{kindLabel(kind)}</span>
  }

  function resetForm() {
    setEditingId(null)
    setName('')
    setVcsConnectionId('')
    setRepoUrl('')
    setBranch('')
    setPattern(DEFAULT_PATTERN)
    setIgnoreText('')
    setNameTemplate('')
    setProvider('')
    setTagPattern('v*')
    setLabels({})
    setOwnerEmail('')
    setEnabled(true)
  }

  function openEditForm(r: ModuleRule) {
    const a = r.attributes
    setEditingId(r.id)
    setName(a.name)
    setVcsConnectionId(a['vcs-connection-id'])
    setRepoUrl(a['repo-url'])
    setBranch(a.branch)
    setPattern(a.pattern)
    setIgnoreText((a['ignore-patterns'] || []).join('\n'))
    setNameTemplate(a['name-template'])
    setProvider(a.provider)
    setTagPattern(a['vcs-tag-pattern'] || 'v*')
    setLabels(a.labels || {})
    setOwnerEmail(a['owner-email'] || '')
    setEnabled(a.enabled)
    setShowForm(true)
  }

  function formAttributes(): Record<string, unknown> {
    return {
      name,
      'vcs-connection-id': vcsConnectionId,
      'repo-url': repoUrl.trim(),
      branch: branch.trim(),
      pattern: pattern.trim(),
      'ignore-patterns': ignoreText.split('\n').map((s) => s.trim()).filter(Boolean),
      'name-template': nameTemplate,
      provider: provider.trim(),
      'vcs-tag-pattern': tagPattern.trim() || 'v*',
      labels,
      'owner-email': ownerEmail,
      enabled,
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    setError('')
    setSuccess('')
    try {
      const path = editingId
        ? `/api/terrapod/v1/module-autodiscovery-rules/${editingId}`
        : '/api/terrapod/v1/module-autodiscovery-rules'
      const res = await apiFetch(path, {
        method: editingId ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'module-autodiscovery-rules', attributes: formAttributes() } }),
      })
      if (!res.ok) throw new Error(await parseApiError(res, tw('errors.save')))
      const saved: ModuleRule | undefined = (await res.json().catch(() => null))?.data
      setSuccess(editingId ? tw('success.updated', { name }) : tw('success.created', { name }))
      setShowForm(false)
      resetForm()
      loadAll()
      // Choosing which modules to register is the next step after creating a rule,
      // and only a saved rule can register them — so show the saved rule's preview
      // rather than leave an unsaved one on screen that can no longer register
      // anything. After an edit, refresh a preview that is already open.
      if (saved?.id && (!editingId || preview)) previewSaved(saved)
      else if (preview && !preview.ruleId) setPreview(null)
      if (saved?.id && repos?.ruleId === saved.id) setRepos(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : tw('errors.save'))
    } finally {
      setSubmitting(false)
    }
  }

  // Irreversible: a native confirm in every pointer mode (AGENTS.md).
  async function handleDelete(r: ModuleRule) {
    if (!window.confirm(t('confirmDelete', { name: r.attributes.name }))) return
    setError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/module-autodiscovery-rules/${r.id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(await parseApiError(res, tw('errors.delete')))
      setSuccess(tw('success.deleted'))
      if (preview?.ruleId === r.id) setPreview(null)
      if (repos?.ruleId === r.id) setRepos(null)
      loadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : tw('errors.delete'))
    }
  }

  async function loadPreview(ruleId: string, ruleName: string, source: PreviewSource, page = 1, selected: string[] = []) {
    setPreview({ ruleId, ruleName, source, page, loading: true, selected })
    try {
      const query = page > 1 ? `?page[number]=${page}` : ''
      const res = source.saved
        ? await apiFetch(`/api/terrapod/v1/module-autodiscovery-rules/${source.ruleId}/preview${query}`)
        : await apiFetch(`/api/terrapod/v1/module-autodiscovery-rules/preview${query}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/vnd.api+json' },
          body: JSON.stringify({ data: { type: 'module-autodiscovery-rules', attributes: source.attributes } }),
        })
      if (!res.ok) throw new Error(await parseApiError(res, tw('errors.preview')))
      const body = await res.json()
      const attrs = body?.data?.attributes ?? {}
      setPreview({
        ruleId,
        ruleName,
        source,
        page,
        kind: kindOf(attrs['target-kind']),
        ref: attrs.ref,
        filesWalked: attrs['files-walked'],
        entries: attrs.entries ?? [],
        repositories: attrs.repositories ?? [],
        complete: attrs['listing-complete'] !== false,
        meta: body?.meta?.pagination ?? null,
        selected,
      })
    } catch (err) {
      setPreview({ ruleId, ruleName, source, page, selected, error: err instanceof Error ? err.message : tw('errors.preview') })
    }
  }

  function previewSaved(r: ModuleRule) {
    loadPreview(r.id, r.attributes.name, { saved: true, ruleId: r.id })
  }

  function previewForm() {
    if (!vcsConnectionId || !repoUrl.trim() || !pattern.trim()) {
      setError(tw('errors.previewPrereq'))
      return
    }
    setError('')
    loadPreview('', name || tw('unsavedRule'), {
      saved: false,
      attributes: { ...formAttributes(), name: name || 'preview' },
    })
  }

  async function loadRepositories(r: ModuleRule, status: string, page: number) {
    const base: RepositoriesState = {
      ruleId: r.id,
      ruleName: r.attributes.name,
      kind: kindOf(r.attributes['target-kind']),
      lastEnumeratedAt: r.attributes['last-enumerated-at'] ?? null,
      status,
      page,
    }
    setRepos({ ...base, loading: true })
    try {
      let path = `/api/terrapod/v1/module-autodiscovery-rules/${r.id}/repositories?page[size]=${REPOSITORIES_PAGE_SIZE}&page[number]=${page}`
      if (status) path += `&filter[status]=${encodeURIComponent(status)}`
      const res = await apiFetch(path)
      if (!res.ok) throw new Error(await parseApiError(res, t('repositories.loadFailed')))
      const body = await res.json()
      setRepos({ ...base, items: body?.data ?? [], meta: body?.meta?.pagination ?? null })
    } catch (err) {
      setRepos({ ...base, error: err instanceof Error ? err.message : t('repositories.loadFailed') })
    }
  }

  async function register(all: boolean) {
    if (!preview?.ruleId) return
    const kind = preview.kind ?? 'repository'
    // Registering everything is one tap. For an org-wide rule it can be many
    // repositories' worth, so it is always confirmed; for one repository only
    // on touch, where a mis-tap is easy (AGENTS.md).
    if (all && (kind !== 'repository' || isTouch) && !window.confirm(t('grouped.confirmRegisterAll'))) return
    let attributes: Record<string, unknown> = {}
    if (!all) {
      if (kind === 'repository') {
        attributes = { subdirectories: preview.selected.map((k) => splitKey(k)[1]) }
      } else {
        const byRepository = new Map<string, string[]>()
        for (const k of preview.selected) {
          const [repository, subdirectory] = splitKey(k)
          byRepository.set(repository, [...(byRepository.get(repository) ?? []), subdirectory])
        }
        attributes = {
          selections: [...byRepository.entries()].map(([repository, subdirectories]) => ({ repository, subdirectories })),
        }
      }
    }
    setPreview({ ...preview, registering: true, error: undefined })
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/module-autodiscovery-rules/${preview.ruleId}/scan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'module-autodiscovery-rule-scans', attributes } }),
      })
      if (!res.ok) throw new Error(await parseApiError(res, tw('errors.scan')))
      const attrs = (await res.json())?.data?.attributes ?? {}
      const skipped: { repository?: string; subdirectory: string; reason: string }[] = attrs.skipped ?? []
      const reason = (r: string) =>
        r === 'already-registered' ? t('reasonAlreadyRegistered')
          : r === 'name-taken' ? t('reasonNameTaken')
            : r === 'missing-provider' ? t('reasonMissingProvider') : r
      const where = (s: { repository?: string; subdirectory: string }) => {
        const dir = s.subdirectory || td('root')
        return s.repository && kind !== 'repository' ? `${s.repository} · ${dir}` : dir
      }
      setSuccess([
        t('scanResult', { count: attrs['modules-registered'] ?? 0 }),
        ...(kind !== 'repository' ? [t('grouped.scanRepositories', { count: attrs['repositories-scanned'] ?? 0 })] : []),
        ...skipped.map((s) => t('scanSkipped', { path: where(s), reason: reason(s.reason) })),
      ].join(' '))
      setPreview(null)
    } catch (err) {
      setPreview({ ...preview, registering: false, error: err instanceof Error ? err.message : tw('errors.scan') })
    }
  }

  function setPicked(keys: string[], on: boolean) {
    if (!preview) return
    const selected = new Set(preview.selected)
    for (const k of keys) {
      if (on) selected.add(k)
      else selected.delete(k)
    }
    setPreview({ ...preview, selected: [...selected] })
  }

  function pager(meta: PageMeta | null | undefined, onPage: (page: number) => void, disabled?: boolean) {
    if (!meta || meta['total-pages'] <= 1) return null
    const page = meta['current-page']
    const pages = meta['total-pages']
    const button = 'px-3 py-2 min-h-11 rounded-lg bg-slate-700 hover:bg-slate-600 text-slate-100 text-sm disabled:opacity-50'
    return (
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className={button} disabled={disabled || page <= 1} onClick={() => onPage(page - 1)}>
          {t('paging.previous')}
        </button>
        <span className="text-xs text-slate-400">{t('paging.page', { page, pages })}</span>
        <button type="button" className={button} disabled={disabled || page >= pages} onClick={() => onPage(page + 1)}>
          {t('paging.next')}
        </button>
      </div>
    )
  }

  function renderCandidate(c: Candidate, grouped: boolean) {
    if (!preview) return null
    const dir = c.subdirectory || td('root')
    const where = grouped && c.repository
      ? (c.subdirectory ? `${c.repository}/${c.subdirectory}` : `${c.repository} ${td('root')}`)
      : dir
    const key = keyOf(c)
    const canPick = registrable(c) && !!preview.ruleId
    const registered = c['registered-as']
    return (
      <li key={key} className="rounded-lg border border-slate-700/50 p-3">
        <label className={`flex items-start gap-3 min-h-11 ${canPick ? 'cursor-pointer' : ''}`}>
          <input
            type="checkbox"
            className="h-5 w-5 mt-0.5 shrink-0"
            checked={preview.selected.includes(key)}
            disabled={!canPick || preview.registering}
            aria-label={td('select', { path: where })}
            onChange={(e) => setPicked([key], e.target.checked)}
          />
          <span className="min-w-0 space-y-1">
            <span className="block font-mono text-xs text-slate-300 break-all">{dir}</span>
            <span className="block text-sm text-slate-200 break-all">
              {c.name}{c.provider ? `/${c.provider}` : ''}
            </span>
            {registered && (
              <span className="block text-xs text-green-300">
                {td('registeredAs', { name: `${registered.name}/${registered.provider}` })}
              </span>
            )}
            {!registered && c.collision && (
              <span className="block text-xs text-amber-300">{t('nameTaken')}</span>
            )}
            {!registered && c['missing-provider'] && (
              <span className="block text-xs text-amber-300">{t('needsProvider')}</span>
            )}
          </span>
        </label>
      </li>
    )
  }

  function renderGroups() {
    if (!preview?.entries) return null
    const repositories = preview.repositories ?? []
    const known = new Set(repositories.map((r) => r.repository))
    const groups: { repository: PreviewRepository; entries: Candidate[] }[] = repositories.map((r) => ({
      repository: r,
      entries: preview.entries!.filter((c) => (c.repository ?? '') === r.repository),
    }))
    // Entries for a repository the response did not describe still show.
    for (const c of preview.entries) {
      const path = c.repository ?? ''
      if (known.has(path)) continue
      known.add(path)
      groups.push({
        repository: { repository: path, 'repo-url': c['repo-url'] ?? '', ref: '', status: 'active', error: '' },
        entries: preview.entries.filter((e) => (e.repository ?? '') === path),
      })
    }
    if (groups.length === 0) return <p className="text-sm text-slate-400">{td('none')}</p>
    return (
      <ul className="space-y-3">
        {groups.map(({ repository: r, entries }) => {
          const pickable = entries.filter(registrable).map(keyOf)
          const allPicked = pickable.length > 0 && pickable.every((k) => preview.selected.includes(k))
          return (
            <li key={r.repository} className="rounded-lg border border-slate-700 bg-slate-900/40 p-3 space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-sm text-slate-100 break-all">{r.repository}</span>
                {statusPill(r.status)}
                {r.origin && originPill(r.origin)}
              </div>
              {r.error && (
                <p className="text-xs text-red-300 break-words">{t('grouped.repositoryError', { error: r.error })}</p>
              )}
              {entries.length === 0 && !r.error && (
                <p className="text-xs text-slate-500">{t('grouped.noCandidates')}</p>
              )}
              {preview.ruleId && pickable.length > 1 && (
                <label className="flex items-center gap-3 min-h-11 text-sm text-slate-300 cursor-pointer">
                  <input
                    type="checkbox"
                    className="h-5 w-5 shrink-0"
                    checked={allPicked}
                    disabled={preview.registering}
                    onChange={(e) => setPicked(pickable, e.target.checked)}
                  />
                  <span className="break-all">{t('grouped.selectRepository', { repository: r.repository })}</span>
                </label>
              )}
              {entries.length > 0 && <ul className="space-y-2">{entries.map((c) => renderCandidate(c, true))}</ul>}
            </li>
          )
        })}
      </ul>
    )
  }

  if (loading) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>

  const rowButton = 'px-3 py-1.5 rounded-lg text-xs font-medium transition-colors min-h-9'
  const attention = sortedItems.filter((r) => r.attributes['last-error'])
  const previewKind = preview?.kind ?? 'repository'
  const orgPreview = !!preview && !preview.loading && !preview.error && previewKind !== 'repository'

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={t('title')}
          description={t('description')}
          actions={
            // While the form is open its own buttons carry Create and Cancel; a
            // second Cancel here, styled as the page's primary action, only competes.
            !showForm && (
              <button
                type="button"
                onClick={() => { resetForm(); setShowForm(true) }}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
              >
                {tw('actions.newRule')}
              </button>
            )
          }
        />

        {error && <ErrorBanner message={error} />}
        {success && (
          <div role="status" className="mb-4 px-4 py-3 rounded-lg bg-green-900/30 border border-green-800 text-green-300 text-sm">
            {success}
          </div>
        )}

        {/* A rule's last poll could not do all its work: its target was deleted,
            could not be listed, or the API quota ran low. Shown at every width. */}
        {attention.length > 0 && (
          <div className="mb-6 space-y-2">
            <HealthConditions
              conditions={attention.map((r) => ({
                code: r.id,
                severity: 'warning' as const,
                title: t('attention.title', { name: r.attributes.name }),
                detail: r.attributes['last-error'] ?? '',
              }))}
            />
            <p className="text-xs text-slate-400">{t('attention.hint')}</p>
          </div>
        )}

        {showForm && (
          <form onSubmit={handleSubmit} className="mb-6 p-4 sm:p-6 rounded-lg bg-slate-900/60 border border-slate-800 space-y-4">
            <h2 className="text-lg font-semibold text-slate-100">
              {editingId ? t('editTitle') : t('createTitle')}
            </h2>
            <p className="text-sm text-slate-400">{t('howItWorks')}</p>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label htmlFor="mar-name" className="block text-sm text-slate-300 mb-1">{t('ruleName')}</label>
                <input id="mar-name" required value={name} onChange={(e) => setName(e.target.value)} className={INPUT} />
              </div>
              <div>
                <label htmlFor="mar-conn" className="block text-sm text-slate-300 mb-1">{tw('form.vcsConnection')}</label>
                <select id="mar-conn" required value={vcsConnectionId} onChange={(e) => setVcsConnectionId(e.target.value)} className={INPUT}>
                  <option value="">{tw('form.selectConnection')}</option>
                  {connections.map((c) => (
                    <option key={c.id} value={c.id}>{c.attributes.name} ({c.attributes.provider})</option>
                  ))}
                </select>
              </div>
              <div className="sm:col-span-2">
                <label htmlFor="mar-repo" className="block text-sm text-slate-300 mb-1">{tw('form.repoUrl')}</label>
                <input
                  id="mar-repo"
                  required
                  value={repoUrl}
                  onChange={(e) => setRepoUrl(e.target.value)}
                  placeholder="https://github.com/org/terraform-aws-network" // i18n-ignore — an example URL, not copy
                  aria-describedby="mar-repo-hint"
                  className={INPUT}
                />
                <p id="mar-repo-hint" className="text-xs text-slate-500 mt-1">
                  {t('repoUrlHint', { example: 'https://github.com/org/terraform-*' })}
                </p>
              </div>
              <div>
                <label htmlFor="mar-branch" className="block text-sm text-slate-300 mb-1">{tw('form.branch')}</label>
                <input id="mar-branch" value={branch} onChange={(e) => setBranch(e.target.value)} className={INPUT} />
              </div>
            </div>

            <div>
              <label htmlFor="mar-pattern" className="block text-sm text-slate-300 mb-1">{tw('form.matchPattern')}</label>
              <input
                id="mar-pattern"
                required
                value={pattern}
                onChange={(e) => setPattern(e.target.value)}
                placeholder="**/*.tf" // i18n-ignore — a glob, not copy
                className={`${INPUT} font-mono`}
              />
              <p className="text-xs text-slate-500 mt-1">{t('patternHint')}</p>
            </div>

            <div>
              <label htmlFor="mar-ignore" className="block text-sm text-slate-300 mb-1">{tw('form.ignorePatterns')}</label>
              <textarea
                id="mar-ignore"
                rows={3}
                value={ignoreText}
                onChange={(e) => setIgnoreText(e.target.value)}
                placeholder="modules/legacy/**" // i18n-ignore — a glob, not copy
                className={`${INPUT} font-mono`}
              />
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label htmlFor="mar-template" className="block text-sm text-slate-300 mb-1">{t('nameTemplate')}</label>
                <input
                  id="mar-template"
                  value={nameTemplate}
                  onChange={(e) => setNameTemplate(e.target.value)}
                  placeholder="{repo}-{leaf}" // i18n-ignore — a template, not copy
                  className={`${INPUT} font-mono`}
                />
                <p className="text-xs text-slate-500 mt-1">
                  {t.rich('nameTemplateHint', {
                    repo: () => <code>{'{repo}'}</code>,
                    path: () => <code>{'{path}'}</code>,
                    leaf: () => <code>{'{leaf}'}</code>,
                    root: () => <code>{'{root}'}</code>,
                  })}
                </p>
                <p className="text-xs text-slate-500 mt-1">
                  {t.rich('ownerPlaceholderHint', { owner: () => <code>{'{owner}'}</code> })}
                </p>
              </div>
              <div>
                <label htmlFor="mar-provider" className="block text-sm text-slate-300 mb-1">{t('provider')}</label>
                <input
                  id="mar-provider"
                  value={provider}
                  onChange={(e) => setProvider(e.target.value)}
                  placeholder="aws" // i18n-ignore — a provider name, not copy
                  className={INPUT}
                />
                <p className="text-xs text-slate-500 mt-1">{t('providerHint')}</p>
              </div>
              <div>
                <label htmlFor="mar-tags" className="block text-sm text-slate-300 mb-1">{t('tagPattern')}</label>
                <input id="mar-tags" value={tagPattern} onChange={(e) => setTagPattern(e.target.value)} className={`${INPUT} font-mono`} />
              </div>
              <div>
                <label htmlFor="mar-owner" className="block text-sm text-slate-300 mb-1">{tw('form.ownerEmail')}</label>
                <input id="mar-owner" type="email" value={ownerEmail} onChange={(e) => setOwnerEmail(e.target.value)} className={INPUT} />
                <p className="text-xs text-slate-500 mt-1">{t('ownerEmailHint')}</p>
              </div>
            </div>

            <div>
              <span className="block text-sm text-slate-300 mb-1">{t('labels')}</span>
              <LabelsEditor labels={labels} onChange={setLabels} />
            </div>

            <label className="flex items-center gap-3 min-h-11 text-sm text-slate-300 cursor-pointer">
              <input type="checkbox" className="h-5 w-5" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
              {tw('form.enabled')}
            </label>

            <div className="flex flex-wrap gap-2 pt-2">
              <button
                type="submit"
                disabled={submitting}
                className="px-4 py-2 rounded-lg bg-brand-600 hover:bg-brand-500 text-white text-sm font-medium disabled:opacity-50"
              >
                {submitting ? tw('form.saving') : (editingId ? tw('actions.update') : tw('actions.create'))}
              </button>
              <button
                type="button"
                onClick={previewForm}
                disabled={submitting}
                className="px-4 py-2 rounded-lg bg-slate-700 hover:bg-slate-600 text-slate-100 text-sm disabled:opacity-50"
              >
                {tw('actions.preview')}
              </button>
              <button
                type="button"
                onClick={() => { setShowForm(false); resetForm() }}
                className="px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm"
              >
                {tw('actions.cancel')}
              </button>
            </div>
          </form>
        )}

        {preview && (
          <section
            aria-labelledby="mar-preview-title"
            className="mb-6 p-4 sm:p-6 rounded-lg bg-slate-900/60 border border-slate-800 space-y-4"
          >
            <div>
              <h2 id="mar-preview-title" className="text-lg font-semibold text-slate-100">
                {tw('preview.title', { ruleName: preview.ruleName })}
              </h2>
              {!orgPreview && (
                <p className="text-xs text-slate-500 mt-1">
                  {preview.ref
                    ? tw.rich('preview.walkedFiles', {
                      count: preview.filesWalked ?? 0,
                      ref: preview.ref,
                      files: (c) => <span className="text-slate-300">{c}</span>,
                      refspan: (c) => <span className="text-slate-300 font-mono">{c}</span>,
                    })
                    : tw('preview.walking')}
                </p>
              )}
              {orgPreview && (
                <p className="text-xs text-slate-400 mt-1">
                  {preview.source.saved ? t('grouped.storedNote') : t('grouped.unsavedNote')}
                </p>
              )}
            </div>

            {preview.loading && <LoadingSpinner />}
            {preview.error && (
              <div className="text-sm text-red-300 bg-red-900/20 border border-red-800/50 rounded p-3">{preview.error}</div>
            )}
            {orgPreview && preview.complete === false && (
              <p role="status" className="text-sm text-amber-300 bg-amber-900/20 border border-amber-800/50 rounded p-3">
                {t('grouped.incomplete')}
              </p>
            )}

            {!orgPreview && preview.entries && preview.entries.length === 0 && (
              <p className="text-sm text-slate-400">{td('none')}</p>
            )}
            {!orgPreview && preview.entries && preview.entries.length > 0 && (
              <ul className="space-y-2">{preview.entries.map((c) => renderCandidate(c, false))}</ul>
            )}
            {orgPreview && renderGroups()}
            {orgPreview && pager(
              preview.meta,
              (page) => loadPreview(preview.ruleId, preview.ruleName, preview.source, page, preview.selected),
              preview.registering,
            )}

            <div className="flex flex-wrap gap-2">
              {preview.ruleId && preview.entries && (preview.entries.some(registrable) || (orgPreview && preview.selected.length > 0)) && (
                <>
                  <button
                    type="button"
                    onClick={() => register(false)}
                    disabled={preview.registering || preview.selected.length === 0}
                    className="px-4 py-2 rounded-lg bg-brand-600 hover:bg-brand-500 text-white text-sm font-medium disabled:opacity-50"
                  >
                    {preview.registering ? td('registering') : td('register', { count: preview.selected.length })}
                  </button>
                  <button
                    type="button"
                    onClick={() => register(true)}
                    disabled={preview.registering}
                    className="px-4 py-2 rounded-lg bg-slate-700 hover:bg-slate-600 text-slate-100 text-sm disabled:opacity-50"
                  >
                    {t('registerAll')}
                  </button>
                </>
              )}
              {!preview.ruleId && preview.entries && preview.entries.length > 0 && (
                <p className="self-center text-xs text-slate-500">{t('saveToRegister')}</p>
              )}
              <button
                type="button"
                onClick={() => setPreview(null)}
                disabled={preview.registering}
                className="px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm disabled:opacity-50"
              >
                {tw('actions.close')}
              </button>
            </div>
          </section>
        )}

        {repos && (
          <section
            aria-labelledby="mar-repos-title"
            className="mb-6 p-4 sm:p-6 rounded-lg bg-slate-900/60 border border-slate-800 space-y-4"
          >
            <div>
              <h2 id="mar-repos-title" className="text-lg font-semibold text-slate-100 break-all">
                {t('repositories.title', { ruleName: repos.ruleName })}
              </h2>
              {repos.kind !== 'repository' && (
                <p className="text-xs text-slate-500 mt-1">
                  {repos.lastEnumeratedAt
                    ? t('repositories.lastListed', { time: when(repos.lastEnumeratedAt) })
                    : t('repositories.notListed')}
                </p>
              )}
              <p className="text-xs text-slate-400 mt-1">{t('repositories.legend')}</p>
            </div>

            <div className="max-w-xs">
              <label htmlFor="mar-repos-status" className="block text-sm text-slate-300 mb-1">{t('repositories.filterLabel')}</label>
              <select
                id="mar-repos-status"
                value={repos.status}
                onChange={(e) => {
                  const rule = rules.find((r) => r.id === repos.ruleId)
                  if (rule) loadRepositories(rule, e.target.value, 1)
                }}
                className={INPUT}
              >
                <option value="">{t('repositories.filterAll')}</option>
                {REPO_STATUSES.map((s) => <option key={s} value={s}>{statusLabel(s)}</option>)}
              </select>
            </div>

            {repos.loading && <LoadingSpinner />}
            {repos.error && (
              <div className="text-sm text-red-300 bg-red-900/20 border border-red-800/50 rounded p-3">{repos.error}</div>
            )}
            {repos.items && repos.items.length === 0 && (
              <p className="text-sm text-slate-400">
                {repos.status ? t('repositories.noneWithStatus') : t('repositories.none')}
              </p>
            )}

            {repos.items && repos.items.length > 0 && (
              <>
                {/* Desktop: a table. */}
                <div className="hidden md:block overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead className="text-start text-slate-400 border-b border-slate-800">
                      <tr>
                        <th className="py-2 text-start font-medium">{t('repositories.repository')}</th>
                        <th className="py-2 text-start font-medium">{t('repositories.status')}</th>
                        <th className="py-2 text-start font-medium">{t('repositories.origin')}</th>
                        <th className="py-2 text-start font-medium">{t('repositories.candidates')}</th>
                        <th className="py-2 text-start font-medium">{t('repositories.lastChecked')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {repos.items.map((r) => {
                        const a = r.attributes
                        const previous = (a['previous-paths'] ?? []).map((p) => p.path)
                        return (
                          <tr key={r.id} className="border-b border-slate-900 align-top">
                            <td className="py-3 pe-4">
                              <span className="font-mono text-xs text-slate-200 break-all">{a.repository}</span>
                              {previous.length > 0 && (
                                <p className="text-xs text-slate-500 mt-1 break-all">{t('repositories.renamedFrom', { paths: previous.join(', ') })}</p>
                              )}
                              {a['last-error'] && (
                                <p className="text-xs text-red-300 mt-1 break-words">{a['last-error']}</p>
                              )}
                            </td>
                            <td className="py-3 pe-4">{statusPill(a.status)}</td>
                            <td className="py-3 pe-4">{originPill(a.origin)}</td>
                            <td className="py-3 pe-4 text-slate-300">{t('repositories.candidateCount', { count: (a.candidates ?? []).length })}</td>
                            <td className="py-3 text-slate-400 text-xs">{when(a['last-checked-at'])}</td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>

                {/* Phone: cards from the same data; status and any error are never dropped. */}
                <MobileCardList>
                  {repos.items.map((r) => {
                    const a = r.attributes
                    const previous = (a['previous-paths'] ?? []).map((p) => p.path)
                    return (
                      <MobileCard
                        key={r.id}
                        title={<span className="font-mono text-xs text-slate-100 break-all">{a.repository}</span>}
                        badge={statusPill(a.status)}
                        fields={[
                          { label: t('repositories.origin'), value: originPill(a.origin) },
                          { label: t('repositories.candidates'), value: t('repositories.candidateCount', { count: (a.candidates ?? []).length }) },
                          { label: t('repositories.lastChecked'), value: when(a['last-checked-at']) },
                          ...(previous.length > 0
                            ? [{ label: t('repositories.renamed'), value: previous.join(', ') }]
                            : []),
                          ...(a['last-error']
                            ? [{ label: t('repositories.error'), value: a['last-error'], valueClassName: 'text-red-300' }]
                            : []),
                        ]}
                      />
                    )
                  })}
                </MobileCardList>
              </>
            )}

            {pager(
              repos.meta,
              (page) => {
                const rule = rules.find((r) => r.id === repos.ruleId)
                if (rule) loadRepositories(rule, repos.status, page)
              },
              repos.loading,
            )}

            <button
              type="button"
              onClick={() => setRepos(null)}
              className="px-4 py-2 min-h-11 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm"
            >
              {tw('actions.close')}
            </button>
          </section>
        )}

        {sortedItems.length === 0 ? (
          <EmptyState message={t('empty')} />
        ) : (
          <>
            {/* Desktop: the sortable table. */}
            <div className="hidden md:block overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-start text-slate-400 border-b border-slate-800">
                  <tr>
                    <SortableHeader label={tw('table.name')} sortKey="name" sortState={sortState} onSort={toggleSort} />
                    <SortableHeader label={tw('table.repo')} sortKey="repo" sortState={sortState} onSort={toggleSort} />
                    <SortableHeader label={tw('table.pattern')} sortKey="pattern" sortState={sortState} onSort={toggleSort} />
                    <SortableHeader label={tw('table.enabled')} sortKey="enabled" sortState={sortState} onSort={toggleSort} />
                    <th className="py-2"></th>
                  </tr>
                </thead>
                <tbody>
                  {sortedItems.map((r) => (
                    <tr key={r.id} className="border-b border-slate-900">
                      <td className="py-3 text-slate-200">{r.attributes.name}</td>
                      <td className="py-3 pe-4">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-slate-400 font-mono text-xs break-all">{r.attributes['repo-url'].replace(/^https?:\/\//, '')}</span>
                          {kindPill(kindOf(r.attributes['target-kind']))}
                        </div>
                      </td>
                      <td className="py-3 text-slate-400 font-mono text-xs">{r.attributes.pattern}</td>
                      <td className="py-3">
                        <div className="flex flex-wrap items-center gap-2">
                          {r.attributes.enabled
                            ? <span className="text-green-400">{tw('status.enabled')}</span>
                            : <span className="text-slate-500">{tw('status.disabled')}</span>}
                          {r.attributes['last-error'] && <span className={`${PILL} bg-amber-900/50 text-amber-300`}>{t('attention.badge')}</span>}
                        </div>
                      </td>
                      <td className="py-3">
                        <div className="flex justify-end gap-2">
                          <button type="button" onClick={() => previewSaved(r)} className={`${rowButton} bg-slate-700 hover:bg-slate-600 text-brand-300`}>{tw('actions.preview')}</button>
                          <button type="button" onClick={() => loadRepositories(r, '', 1)} className={`${rowButton} bg-slate-700 hover:bg-slate-600 text-slate-200`}>{t('repositories.open')}</button>
                          <button type="button" onClick={() => openEditForm(r)} className={`${rowButton} bg-slate-700 hover:bg-slate-600 text-slate-200`}>{tw('actions.edit')}</button>
                          <button type="button" onClick={() => handleDelete(r)} className={`${rowButton} bg-red-900/40 hover:bg-red-900/60 text-red-300`}>{tw('actions.delete')}</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Phone: cards from the same data — status, target and repository never dropped. */}
            <ul className="md:hidden space-y-3">
              {sortedItems.map((r) => (
                <li key={r.id} className="rounded-lg border border-slate-800 bg-slate-900/40 p-4 space-y-2">
                  <div className="flex items-start justify-between gap-3">
                    <span className="text-slate-100 font-medium break-all">{r.attributes.name}</span>
                    <span className="shrink-0 flex flex-wrap justify-end items-center gap-2">
                      {r.attributes['last-error'] && <span className={`${PILL} bg-amber-900/50 text-amber-300`}>{t('attention.badge')}</span>}
                      {r.attributes.enabled
                        ? <span className="text-xs text-green-400">{tw('status.enabled')}</span>
                        : <span className="text-xs text-slate-500">{tw('status.disabled')}</span>}
                    </span>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs text-slate-400 break-all">{r.attributes['repo-url'].replace(/^https?:\/\//, '')}</span>
                    {kindPill(kindOf(r.attributes['target-kind']))}
                  </div>
                  <p className="font-mono text-xs text-slate-500 break-all">{r.attributes.pattern}</p>
                  <div className="flex flex-wrap gap-2 pt-1">
                    <button type="button" onClick={() => previewSaved(r)} className={`${rowButton} min-h-11 bg-slate-700 hover:bg-slate-600 text-brand-300`}>{tw('actions.preview')}</button>
                    <button type="button" onClick={() => loadRepositories(r, '', 1)} className={`${rowButton} min-h-11 bg-slate-700 hover:bg-slate-600 text-slate-200`}>{t('repositories.open')}</button>
                    <button type="button" onClick={() => openEditForm(r)} className={`${rowButton} min-h-11 bg-slate-700 hover:bg-slate-600 text-slate-200`}>{tw('actions.edit')}</button>
                    <button type="button" onClick={() => handleDelete(r)} className={`${rowButton} min-h-11 bg-red-900/40 hover:bg-red-900/60 text-red-300`}>{tw('actions.delete')}</button>
                  </div>
                </li>
              ))}
            </ul>
          </>
        )}
      </main>
    </>
  )
}
