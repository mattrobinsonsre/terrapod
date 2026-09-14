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
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch, fetchAllPages, parseApiError } from '@/lib/api'
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

// A one-off scan of one repository (#1584), through the unsaved-rule preview of
// module autodiscovery: proposals, nothing registered.
interface DiscoveryCandidate {
  subdirectory: string
  name: string
  provider: string
  'registered-as': { name: string; provider: string } | null
  collision: boolean
  'missing-provider': boolean
}

interface Discovery {
  repoUrl: string
  candidates: DiscoveryCandidate[]
}

// Every Terraform file in the repository; the preview narrows to module files.
const DISCOVERY_PATTERN = '**/*.tf*'

interface DiscoveryPick {
  selected: boolean
  name: string
  provider: string
}

const DISCOVERY_INPUT = 'w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent'

export default function ModulesPage() {
  const router = useRouter()
  const t = useTranslations('registry')
  const tRule = useTranslations('adminModuleAutodiscovery')
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

  // Module discovery (#1584): scan a repository, then pick what to register.
  const [showDiscover, setShowDiscover] = useState(false)
  const [discConnectionId, setDiscConnectionId] = useState('')
  const [discRepoUrl, setDiscRepoUrl] = useState('')
  const [discBranch, setDiscBranch] = useState('')
  const [discScanning, setDiscScanning] = useState(false)
  const [discResult, setDiscResult] = useState<Discovery | null>(null)
  const [discPicks, setDiscPicks] = useState<Record<string, DiscoveryPick>>({})
  const [discRegistering, setDiscRegistering] = useState(false)



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

  async function runDiscoveryScan() {
    setDiscScanning(true)
    setError('')
    try {
      const repoUrl = discRepoUrl.trim()
      const attributes = {
        name: 'discover',
        'vcs-connection-id': discConnectionId,
        'repo-url': repoUrl,
        branch: discBranch.trim(),
        pattern: DISCOVERY_PATTERN,
      }
      const res = await apiFetch('/api/terrapod/v1/module-autodiscovery-rules/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'module-autodiscovery-rules', attributes } }),
      })
      if (!res.ok) throw new Error(await parseApiError(res, t('modules.discover.failed')))
      const data = await res.json()
      const result: Discovery = { repoUrl, candidates: data.data.attributes.entries ?? [] }
      const picks: Record<string, DiscoveryPick> = {}
      for (const c of result.candidates) {
        picks[c.subdirectory] = { selected: false, name: c.name, provider: c.provider }
      }
      setDiscResult(result)
      setDiscPicks(picks)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('modules.discover.failed'))
    } finally {
      setDiscScanning(false)
    }
  }

  // Register the ticked candidates through the ordinary create path, each with
  // its subdirectory, then scan again so what was registered shows as such.
  async function registerDiscoveryPicks() {
    if (!discResult) return
    setDiscRegistering(true)
    setError('')
    const failures: string[] = []
    for (const c of discResult.candidates) {
      const pick = discPicks[c.subdirectory]
      if (!pick?.selected || c['registered-as']) continue
      const attributes: Record<string, unknown> = {
        name: pick.name.trim(),
        provider: pick.provider.trim(),
        'vcs-connection-id': discConnectionId,
        'vcs-repo-url': discResult.repoUrl,
        'vcs-branch': discBranch.trim(),
        'vcs-tag-pattern': 'v*',
      }
      if (c.subdirectory) attributes.subdirectory = c.subdirectory
      const res = await apiFetch('/api/terrapod/v1/registry-modules', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'registry-modules', attributes } }),
      })
      if (!res.ok) {
        const where = c.subdirectory || t('modules.discover.root')
        failures.push(`${where}: ${await parseApiError(res, t('modules.createFailed'))}`)
      }
    }
    setDiscRegistering(false)
    await loadModules()
    await runDiscoveryScan()
    if (failures.length) setError(failures.join(' · '))
  }

  const discSelected = discResult
    ? discResult.candidates.filter((c) => discPicks[c.subdirectory]?.selected && !c['registered-as'])
    : []
  const discReady =
    discSelected.length > 0 &&
    discSelected.every((c) => discPicks[c.subdirectory].name.trim() && discPicks[c.subdirectory].provider.trim())
  const canDiscover = isAdmin()

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
            <div className="flex flex-wrap gap-2">
              {canDiscover && (
                <button
                  type="button"
                  onClick={() => setShowDiscover(!showDiscover)}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors"
                >
                  {showDiscover ? t('modules.cancel') : t('modules.discover.open')}
                </button>
              )}
              <button
                onClick={() => setShowCreate(!showCreate)}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
              >
                {showCreate ? t('modules.cancel') : t('modules.createModule')}
              </button>
            </div>
          }
        />

        {error && <ErrorBanner message={error} />}

        {showDiscover && canDiscover && (
          <section
            aria-labelledby="discover-title"
            className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-4"
          >
            <div>
              <h2 id="discover-title" className="text-sm font-semibold text-slate-200">{t('modules.discover.title')}</h2>
              <p className="mt-1 text-xs text-slate-400">{t('modules.discover.intro')}</p>
              <Link
                href="/admin/module-autodiscovery"
                className="mt-2 inline-flex items-center min-h-11 text-sm text-brand-400 hover:text-brand-300 underline underline-offset-2"
              >
                {t('modules.discover.ruleLink')}
              </Link>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div>
                <label htmlFor="disc-conn" className="block text-sm font-medium text-slate-300 mb-1">{t('modules.discover.connection')}</label>
                <select
                  id="disc-conn"
                  value={discConnectionId}
                  onChange={(e) => setDiscConnectionId(e.target.value)}
                  className={DISCOVERY_INPUT}
                >
                  <option value="">{t('moduleDetail.vcs.selectConnection')}</option>
                  {vcsConnections.map((conn) => (
                    <option key={conn.id} value={conn.id}>
                      {conn.attributes.name} ({conn.attributes.provider})
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="disc-repo" className="block text-sm font-medium text-slate-300 mb-1">{t('modules.form.repositoryUrl')}</label>
                <input
                  id="disc-repo"
                  type="text"
                  value={discRepoUrl}
                  onChange={(e) => setDiscRepoUrl(e.target.value)}
                  placeholder="https://github.com/org/terraform-aws-vpc" // i18n-ignore — an example URL, not copy
                  className={DISCOVERY_INPUT}
                />
              </div>
              <div>
                <label htmlFor="disc-branch" className="block text-sm font-medium text-slate-300 mb-1">{t('modules.form.branchOptional')}</label>
                <input
                  id="disc-branch"
                  type="text"
                  value={discBranch}
                  onChange={(e) => setDiscBranch(e.target.value)}
                  placeholder={t('modules.form.branchPlaceholder')}
                  className={DISCOVERY_INPUT}
                />
              </div>
            </div>
            <button
              type="button"
              onClick={runDiscoveryScan}
              disabled={discScanning || !discConnectionId || !discRepoUrl.trim()}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
            >
              {discScanning ? t('modules.discover.scanning') : t('modules.discover.scan')}
            </button>

            {discResult && discResult.candidates.length === 0 && (
              <p className="text-sm text-slate-400">{t('modules.discover.none')}</p>
            )}
            {discResult && discResult.candidates.length > 0 && (
              <>
                <ul className="space-y-2">
                  {discResult.candidates.map((c) => {
                    const pick = discPicks[c.subdirectory]
                    const registered = c['registered-as']
                    const where = c.subdirectory || t('modules.discover.root')
                    const setPick = (patch: Partial<DiscoveryPick>) =>
                      setDiscPicks({ ...discPicks, [c.subdirectory]: { ...pick, ...patch } })
                    return (
                      <li key={c.subdirectory} className="rounded-lg border border-slate-700/50 p-3 space-y-2">
                        <label className="flex items-center gap-3 min-h-11 cursor-pointer">
                          <input
                            type="checkbox"
                            className="h-5 w-5 shrink-0"
                            checked={!!pick?.selected && !registered}
                            disabled={!!registered}
                            aria-label={t('modules.discover.select', { path: where })}
                            onChange={(e) => setPick({ selected: e.target.checked })}
                          />
                          <span className="font-mono text-xs text-slate-300 break-all">{where}</span>
                        </label>
                        {registered ? (
                          <p className="text-xs text-green-300 ms-8">
                            {t('modules.discover.registeredAs', { name: `${registered.name}/${registered.provider}` })}
                          </p>
                        ) : (
                          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 ms-8">
                            {c.collision && pick?.name === c.name && (
                              <p className="sm:col-span-2 text-xs text-amber-300">{tRule('nameTaken')}</p>
                            )}
                            {c['missing-provider'] && !pick?.provider.trim() && (
                              <p className="sm:col-span-2 text-xs text-amber-300">{tRule('needsProvider')}</p>
                            )}
                            <div>
                              <label htmlFor={`disc-name-${c.subdirectory}`} className="block text-xs text-slate-400 mb-1">{t('modules.form.name')}</label>
                              <input
                                id={`disc-name-${c.subdirectory}`}
                                type="text"
                                value={pick?.name ?? ''}
                                onChange={(e) => setPick({ name: e.target.value })}
                                pattern="[a-z][a-z0-9-]*"
                                className={DISCOVERY_INPUT}
                              />
                            </div>
                            <div>
                              <label htmlFor={`disc-provider-${c.subdirectory}`} className="block text-xs text-slate-400 mb-1">{t('modules.form.provider')}</label>
                              <input
                                id={`disc-provider-${c.subdirectory}`}
                                type="text"
                                value={pick?.provider ?? ''}
                                onChange={(e) => setPick({ provider: e.target.value })}
                                pattern="[a-z][a-z0-9-]*"
                                className={DISCOVERY_INPUT}
                              />
                            </div>
                          </div>
                        )}
                      </li>
                    )
                  })}
                </ul>
                <button
                  type="button"
                  onClick={registerDiscoveryPicks}
                  disabled={discRegistering || !discReady}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
                >
                  {discRegistering
                    ? t('modules.discover.registering')
                    : t('modules.discover.register', { count: discSelected.length })}
                </button>
              </>
            )}
          </section>
        )}

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
