'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { LabelsEditor } from '@/components/labels-editor'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch, fetchAllPages, parseApiError } from '@/lib/api'
import { useSortable } from '@/lib/use-sortable'

// Module autodiscovery rules (#1584): find the modules in a repository — the
// root and any submodules — and register them. Contract:
// services/terrapod/api/routers/module_autodiscovery_rules.py.

interface ModuleRule {
  id: string
  attributes: {
    name: string
    'vcs-connection-id': string
    'repo-url': string
    branch: string
    pattern: string
    'ignore-patterns': string[]
    enabled: boolean
    'name-template': string
    provider: string
    'vcs-tag-pattern': string
    labels: Record<string, string>
    'owner-email': string
    'created-at': string
  }
}

interface VCSConnection {
  id: string
  attributes: { name: string; provider: string }
}

interface Candidate {
  subdirectory: string
  name: string
  provider: string
  'registered-as': { name: string; provider: string } | null
  collision: boolean
  'missing-provider': boolean
}

interface PreviewState {
  ruleId: string // '' for an unsaved rule
  ruleName: string
  loading?: boolean
  error?: string
  ref?: string
  filesWalked?: number
  entries?: Candidate[]
  selected: string[]
  registering?: boolean
}

type SortKey = 'name' | 'repo' | 'pattern' | 'enabled'

const INPUT = 'w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-base sm:text-sm'
const DEFAULT_PATTERN = '**/*.tf'

function registrable(c: Candidate): boolean {
  return !c['registered-as'] && !c.collision && !c['missing-provider']
}

export default function ModuleAutodiscoveryPage() {
  const router = useRouter()
  const t = useTranslations('adminModuleAutodiscovery')
  const tw = useTranslations('adminAutodiscovery')
  const td = useTranslations('registry.modules.discover')

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
      setSuccess(editingId ? tw('success.updated', { name }) : tw('success.created', { name }))
      setShowForm(false)
      resetForm()
      loadAll()
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
      loadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : tw('errors.delete'))
    }
  }

  async function loadPreview(ruleId: string, ruleName: string, res: Promise<Response>) {
    setPreview({ ruleId, ruleName, loading: true, selected: [] })
    try {
      const r = await res
      if (!r.ok) throw new Error(await parseApiError(r, tw('errors.preview')))
      const attrs = (await r.json())?.data?.attributes ?? {}
      setPreview({
        ruleId,
        ruleName,
        ref: attrs.ref,
        filesWalked: attrs['files-walked'],
        entries: attrs.entries ?? [],
        selected: [],
      })
    } catch (err) {
      setPreview({ ruleId, ruleName, selected: [], error: err instanceof Error ? err.message : tw('errors.preview') })
    }
  }

  function previewSaved(r: ModuleRule) {
    loadPreview(r.id, r.attributes.name, apiFetch(`/api/terrapod/v1/module-autodiscovery-rules/${r.id}/preview`))
  }

  function previewForm() {
    if (!vcsConnectionId || !repoUrl.trim() || !pattern.trim()) {
      setError(tw('errors.previewPrereq'))
      return
    }
    setError('')
    loadPreview('', name || tw('unsavedRule'), apiFetch('/api/terrapod/v1/module-autodiscovery-rules/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/vnd.api+json' },
      body: JSON.stringify({
        data: { type: 'module-autodiscovery-rules', attributes: { ...formAttributes(), name: name || 'preview' } },
      }),
    }))
  }

  async function register(subdirectories: string[] | null) {
    if (!preview?.ruleId) return
    setPreview({ ...preview, registering: true, error: undefined })
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/module-autodiscovery-rules/${preview.ruleId}/scan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify(
          subdirectories === null
            ? { data: { type: 'module-autodiscovery-rule-scans', attributes: {} } }
            : { data: { type: 'module-autodiscovery-rule-scans', attributes: { subdirectories } } },
        ),
      })
      if (!res.ok) throw new Error(await parseApiError(res, tw('errors.scan')))
      const attrs = (await res.json())?.data?.attributes ?? {}
      const skipped: { subdirectory: string; reason: string }[] = attrs.skipped ?? []
      const reason = (r: string) =>
        r === 'already-registered' ? t('reasonAlreadyRegistered')
          : r === 'name-taken' ? t('reasonNameTaken')
            : r === 'missing-provider' ? t('reasonMissingProvider') : r
      setSuccess([
        t('scanResult', { count: attrs['modules-registered'] ?? 0 }),
        ...skipped.map((s) => t('scanSkipped', { path: s.subdirectory || td('root'), reason: reason(s.reason) })),
      ].join(' '))
      setPreview(null)
    } catch (err) {
      setPreview({ ...preview, registering: false, error: err instanceof Error ? err.message : tw('errors.scan') })
    }
  }

  function toggle(subdirectory: string, on: boolean) {
    if (!preview) return
    const selected = on
      ? [...preview.selected, subdirectory]
      : preview.selected.filter((d) => d !== subdirectory)
    setPreview({ ...preview, selected })
  }

  if (loading) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>

  const rowButton = 'px-3 py-1.5 rounded-lg text-xs font-medium transition-colors min-h-9'

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={t('title')}
          description={t('description')}
          actions={
            <button
              type="button"
              onClick={() => { if (showForm) setShowForm(false); else { resetForm(); setShowForm(true) } }}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showForm ? tw('actions.cancel') : tw('actions.newRule')}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}
        {success && (
          <div role="status" className="mb-4 px-4 py-3 rounded-lg bg-green-900/30 border border-green-800 text-green-300 text-sm">
            {success}
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
                <label htmlFor="mar-name" className="block text-sm text-slate-300 mb-1">{tw('form.name')}</label>
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
              <div>
                <label htmlFor="mar-repo" className="block text-sm text-slate-300 mb-1">{tw('form.repoUrl')}</label>
                <input
                  id="mar-repo"
                  required
                  value={repoUrl}
                  onChange={(e) => setRepoUrl(e.target.value)}
                  placeholder="https://github.com/org/terraform-aws-network" // i18n-ignore — an example URL, not copy
                  className={INPUT}
                />
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
            </div>

            {preview.loading && <LoadingSpinner />}
            {preview.error && (
              <div className="text-sm text-red-300 bg-red-900/20 border border-red-800/50 rounded p-3">{preview.error}</div>
            )}
            {preview.entries && preview.entries.length === 0 && (
              <p className="text-sm text-slate-400">{td('none')}</p>
            )}

            {preview.entries && preview.entries.length > 0 && (
              <ul className="space-y-2">
                {preview.entries.map((c) => {
                  const where = c.subdirectory || td('root')
                  const canPick = registrable(c) && !!preview.ruleId
                  const registered = c['registered-as']
                  return (
                    <li key={c.subdirectory} className="rounded-lg border border-slate-700/50 p-3">
                      <label className={`flex items-start gap-3 min-h-11 ${canPick ? 'cursor-pointer' : ''}`}>
                        <input
                          type="checkbox"
                          className="h-5 w-5 mt-0.5 shrink-0"
                          checked={preview.selected.includes(c.subdirectory)}
                          disabled={!canPick || preview.registering}
                          aria-label={td('select', { path: where })}
                          onChange={(e) => toggle(c.subdirectory, e.target.checked)}
                        />
                        <span className="min-w-0 space-y-1">
                          <span className="block font-mono text-xs text-slate-300 break-all">{where}</span>
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
                })}
              </ul>
            )}

            <div className="flex flex-wrap gap-2">
              {preview.ruleId && preview.entries && preview.entries.some(registrable) && (
                <>
                  <button
                    type="button"
                    onClick={() => register(preview.selected)}
                    disabled={preview.registering || preview.selected.length === 0}
                    className="px-4 py-2 rounded-lg bg-brand-600 hover:bg-brand-500 text-white text-sm font-medium disabled:opacity-50"
                  >
                    {preview.registering ? td('registering') : td('register', { count: preview.selected.length })}
                  </button>
                  <button
                    type="button"
                    onClick={() => register(null)}
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
                      <td className="py-3 text-slate-400 font-mono text-xs break-all">{r.attributes['repo-url'].replace(/^https?:\/\//, '')}</td>
                      <td className="py-3 text-slate-400 font-mono text-xs">{r.attributes.pattern}</td>
                      <td className="py-3">
                        {r.attributes.enabled
                          ? <span className="text-green-400">{tw('status.enabled')}</span>
                          : <span className="text-slate-500">{tw('status.disabled')}</span>}
                      </td>
                      <td className="py-3">
                        <div className="flex justify-end gap-2">
                          <button type="button" onClick={() => previewSaved(r)} className={`${rowButton} bg-slate-700 hover:bg-slate-600 text-brand-300`}>{tw('actions.preview')}</button>
                          <button type="button" onClick={() => openEditForm(r)} className={`${rowButton} bg-slate-700 hover:bg-slate-600 text-slate-200`}>{tw('actions.edit')}</button>
                          <button type="button" onClick={() => handleDelete(r)} className={`${rowButton} bg-red-900/40 hover:bg-red-900/60 text-red-300`}>{tw('actions.delete')}</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Phone: cards from the same data — status, repository and pattern never dropped. */}
            <ul className="md:hidden space-y-3">
              {sortedItems.map((r) => (
                <li key={r.id} className="rounded-lg border border-slate-800 bg-slate-900/40 p-4 space-y-2">
                  <div className="flex items-start justify-between gap-3">
                    <span className="text-slate-100 font-medium break-all">{r.attributes.name}</span>
                    {r.attributes.enabled
                      ? <span className="shrink-0 text-xs text-green-400">{tw('status.enabled')}</span>
                      : <span className="shrink-0 text-xs text-slate-500">{tw('status.disabled')}</span>}
                  </div>
                  <p className="font-mono text-xs text-slate-400 break-all">{r.attributes['repo-url'].replace(/^https?:\/\//, '')}</p>
                  <p className="font-mono text-xs text-slate-500 break-all">{r.attributes.pattern}</p>
                  <div className="flex flex-wrap gap-2 pt-1">
                    <button type="button" onClick={() => previewSaved(r)} className={`${rowButton} min-h-11 bg-slate-700 hover:bg-slate-600 text-brand-300`}>{tw('actions.preview')}</button>
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
