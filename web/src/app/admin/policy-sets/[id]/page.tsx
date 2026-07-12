'use client'

import { useEffect, useState, useCallback, use } from 'react'
import { useTranslations } from 'next-intl'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

interface Policy {
  id: string
  attributes: { name: string; description: string; rego: string }
}

interface PolicySet {
  id: string
  attributes: {
    name: string
    description: string
    'enforcement-level': string
    enabled: boolean
    'global-scope': boolean
    'allow-labels': Record<string, string | string[]>
    'allow-names': string[]
    'deny-labels': Record<string, string | string[]>
    'deny-names': string[]
    source: 'inline' | 'vcs'
    'vcs-repo-url': string | null
    'vcs-branch': string | null
    'policy-path': string | null
    'vcs-last-commit-sha': string | null
    'vcs-last-synced-at': string | null
    'vcs-last-error': string | null
    'created-by': string
  }
  relationships?: { policies?: { data: Policy[] } }
}

function labelsToText(obj: Record<string, string | string[]>): string {
  return Object.entries(obj || {})
    .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : v}`)
    .join('\n')
}

function parseLabels(text: string): Record<string, string[]> {
  const out: Record<string, string[]> = {}
  for (const line of text.split('\n')) {
    const t = line.trim()
    if (!t) continue
    const idx = t.indexOf(':')
    if (idx < 0) continue
    const key = t.slice(0, idx).trim()
    const vals = t.slice(idx + 1).split(',').map((v) => v.trim()).filter(Boolean)
    if (key && vals.length) out[key] = vals
  }
  return out
}

function linesToList(text: string): string[] {
  return text.split('\n').map((l) => l.trim()).filter(Boolean)
}

export default function PolicySetDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params)
  const t = useTranslations('policySetDetail')
  const router = useRouter()
  const [ps, setPs] = useState<PolicySet | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  // Set edit fields
  const [description, setDescription] = useState('')
  const [enforcement, setEnforcement] = useState('advisory')
  const [enabled, setEnabled] = useState(true)
  const [globalScope, setGlobalScope] = useState(false)
  const [allowNames, setAllowNames] = useState('')
  const [denyNames, setDenyNames] = useState('')
  const [allowLabels, setAllowLabels] = useState('')
  const [denyLabels, setDenyLabels] = useState('')
  // VCS config edit fields
  const [vcsRepoUrl, setVcsRepoUrl] = useState('')
  const [vcsBranch, setVcsBranch] = useState('')
  const [policyPath, setPolicyPath] = useState('')

  // New policy form
  const [showAddPolicy, setShowAddPolicy] = useState(false)
  const [npName, setNpName] = useState('')
  const [npDesc, setNpDesc] = useState('')
  const [npRego, setNpRego] = useState(
    'package terrapod\n\ndeny contains msg if {\n\tfalse\n\tmsg := "example"\n}\n',
  )
  const [npSaving, setNpSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/terrapod/v1/policy-sets/${id}`)
      if (!res.ok) throw new Error(t('errors.load'))
      const data = await res.json()
      const set: PolicySet = data.data
      setPs(set)
      const a = set.attributes
      setDescription(a.description || '')
      setEnforcement(a['enforcement-level'])
      setEnabled(a.enabled)
      setGlobalScope(a['global-scope'])
      setAllowNames((a['allow-names'] || []).join('\n'))
      setDenyNames((a['deny-names'] || []).join('\n'))
      setAllowLabels(labelsToText(a['allow-labels'] || {}))
      setDenyLabels(labelsToText(a['deny-labels'] || {}))
      setVcsRepoUrl(a['vcs-repo-url'] || '')
      setVcsBranch(a['vcs-branch'] || '')
      setPolicyPath(a['policy-path'] || '')
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.load'))
    } finally {
      setLoading(false)
    }
  }, [id, t])

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    load()
  }, [router, load])

  async function saveSet(e: React.FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/policy-sets/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'policy-sets',
            attributes: {
              description,
              'enforcement-level': enforcement,
              enabled,
              'global-scope': globalScope,
              'allow-names': linesToList(allowNames),
              'deny-names': linesToList(denyNames),
              'allow-labels': parseLabels(allowLabels),
              'deny-labels': parseLabels(denyLabels),
              ...(ps?.attributes.source === 'vcs' ? {
                'vcs-repo-url': vcsRepoUrl,
                'vcs-branch': vcsBranch,
                'policy-path': policyPath,
              } : {}),
            },
          },
        }),
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        throw new Error(d.detail || t('errors.saveStatus', { status: res.status }))
      }
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.save'))
    } finally {
      setSaving(false)
    }
  }

  async function deleteSet() {
    if (!confirm(t('confirm.deleteSet'))) return
    try {
      const res = await apiFetch(`/api/terrapod/v1/policy-sets/${id}`, { method: 'DELETE' })
      if (!res.ok && res.status !== 204) throw new Error(t('errors.deleteStatus', { status: res.status }))
      router.push('/admin/policy-sets')
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.delete'))
    }
  }

  async function addPolicy(e: React.FormEvent) {
    e.preventDefault()
    setNpSaving(true)
    setError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/policy-sets/${id}/policies`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'policies', attributes: { name: npName, description: npDesc, rego: npRego } },
        }),
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        throw new Error(d.detail || t('errors.addPolicyStatus', { status: res.status }))
      }
      setNpName(''); setNpDesc(''); setShowAddPolicy(false)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.addPolicy'))
    } finally {
      setNpSaving(false)
    }
  }

  if (loading) return (<><NavBar /><main className="px-6 py-8 max-w-5xl mx-auto"><LoadingSpinner /></main></>)
  if (!ps) return (<><NavBar /><main className="px-6 py-8 max-w-5xl mx-auto"><ErrorBanner message={error || t('notFound')} /></main></>)

  const policies = ps.relationships?.policies?.data || []

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-5xl mx-auto">
        <Link href="/admin/policy-sets" className="text-sm text-brand-400 hover:text-brand-300">&larr; {t('backLink')}</Link>
        <PageHeader
          title={ps.attributes.name}
          description={t('subtitle', { createdBy: ps.attributes['created-by'] || t('unknown') })}
          actions={
            <button onClick={deleteSet}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-red-900/60 hover:bg-red-800 text-red-200 transition-colors">
              {t('actions.deleteSet')}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}

        <form onSubmit={saveSet} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-4">
          <h2 className="text-sm font-semibold text-slate-200">{t('settings.heading')}</h2>
          <div>
            <label className="block text-sm font-medium text-slate-300 mb-1">{t('fields.description')}</label>
            <input type="text" value={description} onChange={(e) => setDescription(e.target.value)}
              className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
          </div>
          <div className="flex flex-wrap gap-6 items-center">
            <div>
              <label className="block text-sm font-medium text-slate-300 mb-1">{t('fields.enforcement')}</label>
              <select value={enforcement} onChange={(e) => setEnforcement(e.target.value)}
                className="px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500">
                <option value="advisory">{t('enforcement.advisory')}</option>
                <option value="mandatory">{t('enforcement.mandatory')}</option>
              </select>
            </div>
            <label className="flex items-center gap-2 cursor-pointer mt-5">
              <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)}
                className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
              <span className="text-sm text-slate-300">{t('fields.enabled')}</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer mt-5">
              <input type="checkbox" checked={globalScope} onChange={(e) => setGlobalScope(e.target.checked)}
                className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
              <span className="text-sm text-slate-300">{t('fields.globalScope')}</span>
            </label>
          </div>

          {!globalScope && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 pt-1">
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">{t('scope.allowLabels')}</label>
                <textarea value={allowLabels} onChange={(e) => setAllowLabels(e.target.value)} rows={3}
                  placeholder={t('scope.allowLabelsPlaceholder')}
                  className="w-full px-3 py-2 font-mono text-xs border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">{t('scope.allowNames')}</label>
                <textarea value={allowNames} onChange={(e) => setAllowNames(e.target.value)} rows={3}
                  className="w-full px-3 py-2 font-mono text-xs border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">{t('scope.denyLabels')}</label>
                <textarea value={denyLabels} onChange={(e) => setDenyLabels(e.target.value)} rows={3}
                  className="w-full px-3 py-2 font-mono text-xs border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">{t('scope.denyNames')}</label>
                <textarea value={denyNames} onChange={(e) => setDenyNames(e.target.value)} rows={3}
                  className="w-full px-3 py-2 font-mono text-xs border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
              </div>
            </div>
          )}
          {ps.attributes.source === 'vcs' && (
            <div className="border-t border-slate-700/50 pt-4">
              <h3 className="text-xs font-semibold text-slate-400 uppercase mb-3">{t('vcs.heading')}</h3>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">{t('vcs.repoUrl')}</label>
                  <input type="text" value={vcsRepoUrl} onChange={(e) => setVcsRepoUrl(e.target.value)}
                    placeholder="https://github.com/org/policies"
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">{t('vcs.branch')}</label>
                  <input type="text" value={vcsBranch} onChange={(e) => setVcsBranch(e.target.value)}
                    placeholder={t('vcs.branchPlaceholder')}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">{t('vcs.policyPath')}</label>
                  <input type="text" value={policyPath} onChange={(e) => setPolicyPath(e.target.value)}
                    placeholder="policies/"
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
                </div>
              </div>
            </div>
          )}
          <button type="submit" disabled={saving}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 text-white transition-colors">
            {saving ? t('actions.saving') : t('actions.saveSettings')}
          </button>
        </form>

        {ps.attributes.source === 'vcs' && (
          <div className="rounded-md bg-blue-900/30 border border-blue-700/50 p-4 mb-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-blue-200">
                  {t('vcsBanner.syncedFrom')} <span className="font-mono text-blue-300">{ps.attributes['vcs-repo-url']}</span>
                  {ps.attributes['vcs-branch'] && <> ({ps.attributes['vcs-branch']})</>}
                  {ps.attributes['policy-path'] && <> {t('vcsBanner.at')} <span className="font-mono">{ps.attributes['policy-path']}/</span></>}
                </p>
                {ps.attributes['vcs-last-synced-at'] && (
                  <p className="text-xs text-blue-400 mt-1">
                    {t('vcsBanner.lastSynced', { time: new Date(ps.attributes['vcs-last-synced-at']).toLocaleString() })}
                    {ps.attributes['vcs-last-commit-sha'] && <> {t('vcsBanner.commit', { sha: ps.attributes['vcs-last-commit-sha'].slice(0, 8) })}</>}
                  </p>
                )}
                {ps.attributes['vcs-last-error'] && (
                  <p className="text-xs text-red-400 mt-1">{t('vcsBanner.syncError', { error: ps.attributes['vcs-last-error'] })}</p>
                )}
              </div>
              <button
                onClick={async () => {
                  try {
                    const res = await apiFetch(`/api/terrapod/v1/policy-sets/${ps.id}/actions/sync`, { method: 'POST' })
                    if (res.ok) load()
                  } catch {}
                }}
                className="px-3 py-1.5 rounded-lg text-sm font-medium bg-blue-700 hover:bg-blue-600 text-white transition-colors"
              >
                {t('actions.syncNow')}
              </button>
            </div>
          </div>
        )}

        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-slate-200">{t('policies.heading', { count: policies.length })}</h2>
          {ps.attributes.source !== 'vcs' && (
            <button onClick={() => setShowAddPolicy(!showAddPolicy)}
              className="px-3 py-1.5 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors">
              {showAddPolicy ? t('actions.cancel') : t('actions.addPolicy')}
            </button>
          )}
          {ps.attributes.source === 'vcs' && (
            <span className="text-xs text-slate-500">{t('policies.vcsManaged')}</span>
          )}
        </div>

        {showAddPolicy && (
          <form onSubmit={addPolicy} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-4 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <input type="text" value={npName} onChange={(e) => setNpName(e.target.value)} required placeholder={t('addPolicy.namePlaceholder')}
                className="px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
              <input type="text" value={npDesc} onChange={(e) => setNpDesc(e.target.value)} placeholder={t('addPolicy.descPlaceholder')}
                className="px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
            </div>
            <textarea value={npRego} onChange={(e) => setNpRego(e.target.value)} rows={10} spellCheck={false}
              className="w-full px-3 py-2 font-mono text-xs border border-slate-600 rounded-lg bg-slate-900 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
            <p className="text-xs text-slate-500">
              {t.rich('addPolicy.regoHint', { code: (chunks) => <code className="text-slate-400">{chunks}</code> })}
            </p>
            <button type="submit" disabled={npSaving}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 text-white transition-colors">
              {npSaving ? t('actions.validatingRego') : t('actions.addPolicy')}
            </button>
          </form>
        )}

        {policies.length === 0 ? (
          <p className="text-sm text-slate-500">{t('policies.empty')}</p>
        ) : (
          <div className="space-y-3">
            {policies.map((p) => (
              <PolicyCard key={p.id} policy={p} onChanged={load} onError={setError} />
            ))}
          </div>
        )}
      </main>
    </>
  )
}

function PolicyCard({ policy, onChanged, onError }: {
  policy: Policy
  onChanged: () => void
  onError: (m: string) => void
}) {
  const t = useTranslations('policySetDetail')
  const [rego, setRego] = useState(policy.attributes.rego)
  const [desc, setDesc] = useState(policy.attributes.description || '')
  const [saving, setSaving] = useState(false)
  const dirty = rego !== policy.attributes.rego || desc !== (policy.attributes.description || '')

  async function save() {
    setSaving(true)
    onError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/policies/${policy.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'policies', attributes: { rego, description: desc } } }),
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        throw new Error(d.detail || t('errors.savePolicyStatus', { status: res.status }))
      }
      onChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : t('errors.savePolicy'))
    } finally {
      setSaving(false)
    }
  }

  async function remove() {
    if (!confirm(t('confirm.deletePolicy', { name: policy.attributes.name }))) return
    onError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/policies/${policy.id}`, { method: 'DELETE' })
      if (!res.ok && res.status !== 204) throw new Error(t('errors.deletePolicyStatus', { status: res.status }))
      onChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : t('errors.deletePolicy'))
    }
  }

  return (
    <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-medium text-slate-200">{policy.attributes.name}</span>
        <button onClick={remove} className="text-xs text-red-400 hover:text-red-300">{t('actions.delete')}</button>
      </div>
      <input type="text" value={desc} onChange={(e) => setDesc(e.target.value)} placeholder={t('fields.description')}
        className="w-full mb-2 px-3 py-1.5 text-sm border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
      <textarea value={rego} onChange={(e) => setRego(e.target.value)} rows={10} spellCheck={false}
        className="w-full px-3 py-2 font-mono text-xs border border-slate-600 rounded-lg bg-slate-900 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
      <button onClick={save} disabled={!dirty || saving}
        className="mt-2 px-3 py-1.5 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-slate-700 disabled:text-slate-500 text-white transition-colors">
        {saving ? t('actions.validatingRego') : t('actions.savePolicy')}
      </button>
    </div>
  )
}
