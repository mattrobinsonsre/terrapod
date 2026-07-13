'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import Link from 'next/link'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { useSortable } from '@/lib/use-sortable'
import { usePollingInterval } from '@/lib/use-polling-interval'

interface PolicySet {
  id: string
  attributes: {
    name: string
    description: string
    'enforcement-level': string
    enabled: boolean
    'global-scope': boolean
    'policy-count': number
    'created-at': string
  }
}

type PsSortKey = 'name' | 'description' | 'enforcement' | 'scope' | 'policies' | 'created'

export default function PolicySetsPage() {
  const router = useRouter()
  const t = useTranslations('adminPolicySets')
  const [sets, setSets] = useState<PolicySet[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [showCreate, setShowCreate] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [enforcement, setEnforcement] = useState('advisory')
  const [globalScope, setGlobalScope] = useState(false)
  const [source, setSource] = useState<'inline' | 'vcs'>('inline')
  const [vcsConnectionId, setVcsConnectionId] = useState('')
  const [vcsRepoUrl, setVcsRepoUrl] = useState('')
  const [vcsBranch, setVcsBranch] = useState('')
  const [policyPath, setPolicyPath] = useState('')
  const [vcsConnections, setVcsConnections] = useState<{id: string; attributes: {name: string}}[]>([])
  const [creating, setCreating] = useState(false)

  const accessor = useCallback((item: PolicySet, key: PsSortKey): string | number | null | undefined => {
    switch (key) {
      case 'name': return item.attributes.name
      case 'description': return item.attributes.description
      case 'enforcement': return item.attributes['enforcement-level']
      case 'scope': return item.attributes['global-scope'] ? 'a-global' : 'b-scoped'
      case 'policies': return item.attributes['policy-count']
      case 'created': return item.attributes['created-at']
    }
  }, [])

  const { sortedItems, sortState, toggleSort } = useSortable<PolicySet, PsSortKey>(
    sets, 'name', 'asc', accessor,
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadSets()
  }, [router])

  usePollingInterval(!loading, 60_000, loadSets)

  async function loadSets() {
    try {
      const res = await apiFetch('/api/terrapod/v1/policy-sets')
      if (!res.ok) throw new Error(t('errors.load'))
      const data = await res.json()
      setSets(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.load'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (showCreate && source === 'vcs' && vcsConnections.length === 0) {
      apiFetch('/api/terrapod/v1/vcs-connections').then(r => r.ok ? r.json() : { data: [] }).then(d => {
        setVcsConnections(d.data || [])
      }).catch(() => {})
    }
  }, [showCreate, source, vcsConnections.length])

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    try {
      const attrs: Record<string, unknown> = {
        name,
        description,
        'enforcement-level': enforcement,
        'global-scope': globalScope,
        source,
      }
      if (source === 'vcs') {
        attrs['vcs-connection-id'] = vcsConnectionId
        attrs['vcs-repo-url'] = vcsRepoUrl
        attrs['vcs-branch'] = vcsBranch
        attrs['policy-path'] = policyPath
      }
      const res = await apiFetch('/api/terrapod/v1/policy-sets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'policy-sets',
            attributes: attrs,
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.createStatus', { status: res.status }))
      }
      const created = await res.json()
      setName(''); setDescription(''); setEnforcement('advisory'); setGlobalScope(false)
      setShowCreate(false)
      router.push(`/admin/policy-sets/${created.data.id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.create'))
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
            <button
              onClick={() => setShowCreate(!showCreate)}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showCreate ? t('actions.cancel') : t('actions.new')}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}

        {showCreate && (
          <form onSubmit={handleCreate} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label htmlFor="ps-name" className="block text-sm font-medium text-slate-300 mb-1">{t('form.name')}</label>
                <input id="ps-name" type="text" value={name} onChange={(e) => setName(e.target.value)} required
                  pattern="[a-zA-Z0-9][a-zA-Z0-9_\-]*"
                  title={t('form.namePatternTitle')}
                  placeholder="production-guardrails"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
              <div>
                <label htmlFor="ps-desc" className="block text-sm font-medium text-slate-300 mb-1">{t('form.description')}</label>
                <input id="ps-desc" type="text" value={description} onChange={(e) => setDescription(e.target.value)}
                  placeholder={t('form.descriptionPlaceholder')}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
            </div>
            <div className="flex flex-wrap gap-6 items-center">
              <div>
                <label htmlFor="ps-enf" className="block text-sm font-medium text-slate-300 mb-1">{t('form.enforcement')}</label>
                <select id="ps-enf" value={enforcement} onChange={(e) => setEnforcement(e.target.value)}
                  className="px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500">
                  <option value="advisory">{t('form.enforcementAdvisory')}</option>
                  <option value="mandatory">{t('form.enforcementMandatory')}</option>
                </select>
              </div>
              <label className="flex items-center gap-2 cursor-pointer mt-5">
                <input type="checkbox" checked={globalScope} onChange={(e) => setGlobalScope(e.target.checked)}
                  className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                <span className="text-sm text-slate-300">{t('form.globalScope')}</span>
              </label>
            </div>
            <div className="flex gap-4 items-center">
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="radio" name="source" value="inline" checked={source === 'inline'} onChange={() => setSource('inline')}
                  className="text-brand-600 focus:ring-brand-500" />
                <span className="text-sm text-slate-300">{t('form.sourceInline')}</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="radio" name="source" value="vcs" checked={source === 'vcs'} onChange={() => setSource('vcs')}
                  className="text-brand-600 focus:ring-brand-500" />
                <span className="text-sm text-slate-300">{t('form.sourceVcs')}</span>
              </label>
            </div>
            {source === 'vcs' && (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 p-3 bg-slate-900/50 rounded-lg border border-slate-700/50">
                <div>
                  <label htmlFor="ps-vcs-conn" className="block text-sm font-medium text-slate-300 mb-1">{t('form.vcsConnection')}</label>
                  <select id="ps-vcs-conn" value={vcsConnectionId} onChange={(e) => setVcsConnectionId(e.target.value)} required
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500">
                    <option value="">{t('form.selectConnection')}</option>
                    {vcsConnections.map(c => (
                      <option key={c.id} value={c.id}>{c.attributes.name}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label htmlFor="ps-vcs-repo" className="block text-sm font-medium text-slate-300 mb-1">{t('form.repoUrl')}</label>
                  <input id="ps-vcs-repo" type="text" value={vcsRepoUrl} onChange={(e) => setVcsRepoUrl(e.target.value)} required
                    placeholder="https://github.com/org/infra-policies"
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                </div>
                <div>
                  <label htmlFor="ps-vcs-branch" className="block text-sm font-medium text-slate-300 mb-1">{t('form.branch')}</label>
                  <input id="ps-vcs-branch" type="text" value={vcsBranch} onChange={(e) => setVcsBranch(e.target.value)}
                    placeholder={t('form.branchPlaceholder')}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                </div>
                <div>
                  <label htmlFor="ps-vcs-path" className="block text-sm font-medium text-slate-300 mb-1">{t('form.policyPath')}</label>
                  <input id="ps-vcs-path" type="text" value={policyPath} onChange={(e) => setPolicyPath(e.target.value)}
                    placeholder={t('form.policyPathPlaceholder')}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                </div>
              </div>
            )}
            <p className="text-xs text-slate-500">
              {source === 'vcs' ? t('form.helpVcs') : t('form.helpInline')}
            </p>
            <button type="submit" disabled={creating}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
              {creating ? t('actions.creating') : t('actions.create')}
            </button>
          </form>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : sets.length === 0 ? (
          <EmptyState message={t('empty')} />
        ) : (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <SortableHeader label={t('columns.name')} sortKey="name" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label={t('columns.description')} sortKey="description" sortState={sortState} onSort={toggleSort} className="hidden sm:table-cell" />
                  <SortableHeader label={t('columns.enforcement')} sortKey="enforcement" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
                  <SortableHeader label={t('columns.scope')} sortKey="scope" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
                  <SortableHeader label={t('columns.policies')} sortKey="policies" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700/30">
                {sortedItems.map((ps) => (
                  <tr key={ps.id} className="hover:bg-slate-700/20 transition-colors cursor-pointer"
                    onClick={() => router.push(`/admin/policy-sets/${ps.id}`)}>
                    <td className="px-4 py-3">
                      <Link href={`/admin/policy-sets/${ps.id}`} className="text-sm font-medium text-brand-400 hover:text-brand-300"
                        onClick={(e) => e.stopPropagation()}>
                        {ps.attributes.name}
                      </Link>
                      {!ps.attributes.enabled && (
                        <span className="ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-700 text-slate-400">{t('badges.disabled')}</span>
                      )}
                      {/* Below md the ENFORCEMENT column is hidden — reflow it inline so a phone
                          still sees whether a policy blocks applies or only advises (#719). */}
                      <span className={`md:hidden ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                        ps.attributes['enforcement-level'] === 'mandatory' ? 'bg-red-900/50 text-red-300' : 'bg-amber-900/50 text-amber-300'
                      }`}>
                        {ps.attributes['enforcement-level'] === 'mandatory' ? t('badges.mandatory') : t('badges.advisory')}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-sm text-slate-400 hidden sm:table-cell">
                      {ps.attributes.description || '-'}
                    </td>
                    <td className="px-4 py-3 hidden md:table-cell">
                      {ps.attributes['enforcement-level'] === 'mandatory' ? (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-900/50 text-red-300">{t('badges.mandatory')}</span>
                      ) : (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900/50 text-amber-300">{t('badges.advisory')}</span>
                      )}
                    </td>
                    <td className="px-4 py-3 hidden md:table-cell">
                      {ps.attributes['global-scope'] ? (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-900/50 text-blue-300">{t('badges.global')}</span>
                      ) : (
                        <span className="text-xs text-slate-500">{t('badges.labelScoped')}</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-400 hidden lg:table-cell">
                      {ps.attributes['policy-count'] ?? 0}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </>
  )
}
