'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import Link from 'next/link'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { LabelsEditor } from '@/components/labels-editor'
import { getAuthState } from '@/lib/auth'
import { apiFetch, fetchAllPages } from '@/lib/api'
import { usePollingInterval } from '@/lib/use-polling-interval'

interface VCSConnection {
  id: string
  attributes: { name: string; provider: string }
}

interface Module {
  id: string
  attributes: {
    name: string
    namespace: string
    provider: string
    status: string
    source: string
    'vcs-repo-url'?: string
    subdirectory?: string
    'version-statuses': { version: string; status: string }[]
    'created-at': string | null
  }
}

export default function ModulesPage() {
  const router = useRouter()
  const t = useTranslations('registry')
  const [modules, setModules] = useState<Module[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Create form
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [newProvider, setNewProvider] = useState('')
  const [newLabels, setNewLabels] = useState<Record<string, string>>({})
  const [newVcsConnectionId, setNewVcsConnectionId] = useState('')
  const [newVcsRepoUrl, setNewVcsRepoUrl] = useState('')
  const [newVcsBranch, setNewVcsBranch] = useState('')
  const [newVcsTagPattern, setNewVcsTagPattern] = useState('v*')
  const [newSubdirectory, setNewSubdirectory] = useState('')
  const [vcsConnections, setVcsConnections] = useState<VCSConnection[]>([])
  const [creating, setCreating] = useState(false)



  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadModules()
    loadVcsConnections()
  // eslint-disable-next-line react-hooks/exhaustive-deps -- initial mount load; the loader is a hoisted function declaration recreated each render, so depending on it would re-fetch on every render
  }, [router])

  usePollingInterval(!loading, 60_000, loadModules)

  async function loadVcsConnections() {
    try {
      setVcsConnections(await fetchAllPages<VCSConnection>('/api/terrapod/v1/vcs-connections'))
    } catch {
      // VCS connections are optional
    }
  }

  async function loadModules() {
    try {
      setModules(await fetchAllPages<Module>('/api/terrapod/v1/registry-modules'))
    } catch (err) {
      setError(err instanceof Error ? err.message : t('modules.loadFailed'))
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    try {
      const attributes: Record<string, unknown> = {
        name: newName,
        provider: newProvider,
      }
      if (Object.keys(newLabels).length > 0) attributes.labels = newLabels
      if (newVcsConnectionId) {
        attributes['vcs-connection-id'] = newVcsConnectionId
        attributes['vcs-repo-url'] = newVcsRepoUrl
        attributes['vcs-branch'] = newVcsBranch
        attributes['vcs-tag-pattern'] = newVcsTagPattern
        if (newSubdirectory.trim()) attributes.subdirectory = newSubdirectory.trim()
      }

      const res = await apiFetch('/api/terrapod/v1/registry-modules', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'registry-modules', attributes },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('modules.createFailedStatus', { status: res.status }))
      }
      setNewName('')
      setNewProvider('')
      setNewLabels({})
      setNewVcsConnectionId('')
      setNewVcsRepoUrl('')
      setNewVcsBranch('')
      setNewVcsTagPattern('v*')
      setNewSubdirectory('')
      setShowCreate(false)
      await loadModules()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('modules.createFailed'))
    } finally {
      setCreating(false)
    }
  }

  // Modules that share a repository — a root module and its submodules
  // (#1583) — are listed together under it. Everything else is listed as it
  // always was.
  const perRepo = new Map<string, Module[]>()
  for (const mod of modules) {
    const repo = mod.attributes['vcs-repo-url']
    if (repo) perRepo.set(repo, [...(perRepo.get(repo) ?? []), mod])
  }
  const repoGroups = [...perRepo.entries()]
    .filter(([, mods]) => mods.length > 1)
    .map(([repo, mods]) => ({
      repo,
      // The root module first, then its submodules by path.
      modules: [...mods].sort((a, b) =>
        (a.attributes.subdirectory ?? '').localeCompare(b.attributes.subdirectory ?? ''),
      ),
    }))
  const grouped = new Set(repoGroups.flatMap((g) => g.modules.map((m) => m.id)))
  const singles = modules.filter((m) => !grouped.has(m.id))

  function renderCard(mod: Module) {
    return (
      <Link
        key={mod.id}
        href={`/registry/modules/${mod.attributes.name}/${mod.attributes.provider}`}
        className="bg-slate-800/50 rounded-lg border border-slate-700/50 hover:border-brand-600/30 p-4 transition-colors"
      >
        <h3 className="font-semibold text-slate-200">{mod.attributes.name}</h3>
        <p className="text-sm text-slate-500 mt-1">{t('modules.providerLabel', { provider: mod.attributes.provider })}</p>
        {mod.attributes.subdirectory && (
          <p className="text-xs text-slate-400 font-mono mt-1 break-all">{t('modules.submoduleAt', { path: mod.attributes.subdirectory })}</p>
        )}
        <div className="flex items-center gap-2 mt-2">
          <span className="text-xs text-slate-400">
            {t('modules.versionCount', { count: mod.attributes['version-statuses']?.length || 0 })}
          </span>
          <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
            mod.attributes.status === 'setup_complete'
              ? 'bg-green-900/50 text-green-300'
              : 'bg-slate-700 text-slate-400'
          }`}>
            {mod.attributes.status}
          </span>
          {mod.attributes.source === 'vcs' && (
            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-900/50 text-blue-300">
              VCS
            </span>
          )}
        </div>
      </Link>
    )
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={t('modules.title')}
          description={t('modules.description')}
          actions={
            <button
              onClick={() => setShowCreate(!showCreate)}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showCreate ? t('modules.cancel') : t('modules.createModule')}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}

        {showCreate && (
          <form onSubmit={handleCreate} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-4">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label htmlFor="mod-name" className="block text-sm font-medium text-slate-300 mb-1">{t('modules.form.name')}</label>
                <input
                  id="mod-name"
                  type="text"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  required
                  pattern="[a-z][a-z0-9-]*"
                  title={t('modules.form.slugTitle')}
                  placeholder="vpc"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
              <div>
                <label htmlFor="mod-provider" className="block text-sm font-medium text-slate-300 mb-1">{t('modules.form.provider')}</label>
                <input
                  id="mod-provider"
                  type="text"
                  value={newProvider}
                  onChange={(e) => setNewProvider(e.target.value)}
                  required
                  pattern="[a-z][a-z0-9-]*"
                  title={t('modules.form.slugTitle')}
                  placeholder="aws"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
            </div>
            <p className="mt-1 text-xs text-slate-500">{t('modules.form.addressNote')}</p>

            {/* VCS Configuration */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label htmlFor="create-vcs-conn" className="block text-sm font-medium text-slate-300 mb-1">{t('modules.form.vcsConnectionOptional')}</label>
                <select
                  id="create-vcs-conn"
                  value={newVcsConnectionId}
                  onChange={(e) => setNewVcsConnectionId(e.target.value)}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                >
                  <option value="">{t('modules.form.noneUploadManually')}</option>
                  {vcsConnections.map((conn) => (
                    <option key={conn.id} value={conn.id}>
                      {conn.attributes.name} ({conn.attributes.provider})
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="create-vcs-repo" className="block text-sm font-medium text-slate-300 mb-1">{t('modules.form.repositoryUrl')}</label>
                <input
                  id="create-vcs-repo"
                  type="text"
                  value={newVcsRepoUrl}
                  onChange={(e) => setNewVcsRepoUrl(e.target.value)}
                  placeholder="https://github.com/org/terraform-module-vpc"
                  disabled={!newVcsConnectionId}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent disabled:opacity-50"
                />
              </div>
              <div>
                <label htmlFor="create-vcs-branch" className="block text-sm font-medium text-slate-300 mb-1">{t('modules.form.branchOptional')}</label>
                <input
                  id="create-vcs-branch"
                  type="text"
                  value={newVcsBranch}
                  onChange={(e) => setNewVcsBranch(e.target.value)}
                  placeholder={t('modules.form.branchPlaceholder')}
                  disabled={!newVcsConnectionId}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent disabled:opacity-50"
                />
              </div>
              <div>
                <label htmlFor="create-vcs-tag" className="block text-sm font-medium text-slate-300 mb-1">{t('modules.form.tagPattern')}</label>
                <input
                  id="create-vcs-tag"
                  type="text"
                  value={newVcsTagPattern}
                  onChange={(e) => setNewVcsTagPattern(e.target.value)}
                  placeholder="v*"
                  disabled={!newVcsConnectionId}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent disabled:opacity-50"
                />
                <p className="mt-1 text-xs text-slate-500">{t('modules.form.tagPatternHint')}</p>
              </div>
              <div className="sm:col-span-2">
                <label htmlFor="create-vcs-subdir" className="block text-sm font-medium text-slate-300 mb-1">{t('modules.form.subdirectoryOptional')}</label>
                <input
                  id="create-vcs-subdir"
                  type="text"
                  value={newSubdirectory}
                  onChange={(e) => setNewSubdirectory(e.target.value)}
                  placeholder="modules/create" // i18n-ignore — an example path, not copy
                  disabled={!newVcsConnectionId}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent disabled:opacity-50"
                />
                <p className="mt-1 text-xs text-slate-500">{t('modules.form.subdirectoryHint')}</p>
              </div>
            </div>

            {/* Labels */}
            <div className="pt-2">
              <label className="block text-sm font-medium text-slate-300 mb-1">{t('modules.form.labelsOptional')}</label>
              <LabelsEditor labels={newLabels} onChange={setNewLabels} />
            </div>

            <button
              type="submit"
              disabled={creating}
              className="mt-2 px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
            >
              {creating ? t('modules.form.creating') : t('modules.form.create')}
            </button>
          </form>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : modules.length === 0 ? (
          <EmptyState message={t('modules.empty')} />
        ) : (
          <div className="space-y-6">
            {singles.length > 0 && (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
                {singles.map(renderCard)}
              </div>
            )}
            {repoGroups.map((g) => (
              <section key={g.repo}>
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 mb-2">
                  <h2 className="text-sm font-semibold text-slate-300 font-mono break-all">{g.repo}</h2>
                  <span className="text-xs text-slate-500">{t('modules.repoModuleCount', { count: g.modules.length })}</span>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
                  {g.modules.map(renderCard)}
                </div>
              </section>
            ))}
          </div>
        )}
      </main>
    </>
  )
}
