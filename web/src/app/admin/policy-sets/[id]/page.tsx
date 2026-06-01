'use client'

import { useEffect, useState, useCallback, use } from 'react'
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
      if (!res.ok) throw new Error('Failed to load policy set')
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
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load policy set')
    } finally {
      setLoading(false)
    }
  }, [id])

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
            },
          },
        }),
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        throw new Error(d.detail || `Failed to save (${res.status})`)
      }
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save policy set')
    } finally {
      setSaving(false)
    }
  }

  async function deleteSet() {
    if (!confirm('Delete this policy set and all its policies? Recorded run evaluations are kept.')) return
    try {
      const res = await apiFetch(`/api/terrapod/v1/policy-sets/${id}`, { method: 'DELETE' })
      if (!res.ok && res.status !== 204) throw new Error(`Failed to delete (${res.status})`)
      router.push('/admin/policy-sets')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete policy set')
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
        throw new Error(d.detail || `Failed to add policy (${res.status})`)
      }
      setNpName(''); setNpDesc(''); setShowAddPolicy(false)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add policy')
    } finally {
      setNpSaving(false)
    }
  }

  if (loading) return (<><NavBar /><main className="px-6 py-8 max-w-5xl mx-auto"><LoadingSpinner /></main></>)
  if (!ps) return (<><NavBar /><main className="px-6 py-8 max-w-5xl mx-auto"><ErrorBanner message={error || 'Not found'} /></main></>)

  const policies = ps.relationships?.policies?.data || []

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-5xl mx-auto">
        <Link href="/admin/policy-sets" className="text-sm text-brand-400 hover:text-brand-300">&larr; Policy Sets</Link>
        <PageHeader
          title={ps.attributes.name}
          description={`OPA policy set · created by ${ps.attributes['created-by'] || 'unknown'}`}
          actions={
            <button onClick={deleteSet}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-red-900/60 hover:bg-red-800 text-red-200 transition-colors">
              Delete Set
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}

        <form onSubmit={saveSet} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-4">
          <h2 className="text-sm font-semibold text-slate-200">Settings</h2>
          <div>
            <label className="block text-sm font-medium text-slate-300 mb-1">Description</label>
            <input type="text" value={description} onChange={(e) => setDescription(e.target.value)}
              className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
          </div>
          <div className="flex flex-wrap gap-6 items-center">
            <div>
              <label className="block text-sm font-medium text-slate-300 mb-1">Enforcement</label>
              <select value={enforcement} onChange={(e) => setEnforcement(e.target.value)}
                className="px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500">
                <option value="advisory">Advisory (warn)</option>
                <option value="mandatory">Mandatory (block apply)</option>
              </select>
            </div>
            <label className="flex items-center gap-2 cursor-pointer mt-5">
              <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)}
                className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
              <span className="text-sm text-slate-300">Enabled</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer mt-5">
              <input type="checkbox" checked={globalScope} onChange={(e) => setGlobalScope(e.target.checked)}
                className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
              <span className="text-sm text-slate-300">Global (every workspace)</span>
            </label>
          </div>

          {!globalScope && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 pt-1">
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">Allow labels (key: v1, v2)</label>
                <textarea value={allowLabels} onChange={(e) => setAllowLabels(e.target.value)} rows={3}
                  placeholder="env: prod, staging"
                  className="w-full px-3 py-2 font-mono text-xs border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">Allow names (one per line)</label>
                <textarea value={allowNames} onChange={(e) => setAllowNames(e.target.value)} rows={3}
                  className="w-full px-3 py-2 font-mono text-xs border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">Deny labels (key: v1, v2)</label>
                <textarea value={denyLabels} onChange={(e) => setDenyLabels(e.target.value)} rows={3}
                  className="w-full px-3 py-2 font-mono text-xs border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">Deny names (one per line)</label>
                <textarea value={denyNames} onChange={(e) => setDenyNames(e.target.value)} rows={3}
                  className="w-full px-3 py-2 font-mono text-xs border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
              </div>
            </div>
          )}
          <button type="submit" disabled={saving}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 text-white transition-colors">
            {saving ? 'Saving...' : 'Save Settings'}
          </button>
        </form>

        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-slate-200">Policies ({policies.length})</h2>
          <button onClick={() => setShowAddPolicy(!showAddPolicy)}
            className="px-3 py-1.5 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors">
            {showAddPolicy ? 'Cancel' : 'Add Policy'}
          </button>
        </div>

        {showAddPolicy && (
          <form onSubmit={addPolicy} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-4 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <input type="text" value={npName} onChange={(e) => setNpName(e.target.value)} required placeholder="Policy name"
                className="px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
              <input type="text" value={npDesc} onChange={(e) => setNpDesc(e.target.value)} placeholder="Description (optional)"
                className="px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
            </div>
            <textarea value={npRego} onChange={(e) => setNpRego(e.target.value)} rows={10} spellCheck={false}
              className="w-full px-3 py-2 font-mono text-xs border border-slate-600 rounded-lg bg-slate-900 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
            <p className="text-xs text-slate-500">
              Rego v1. Must declare <code className="text-slate-400">package terrapod</code> and express violations
              via a <code className="text-slate-400">deny</code> set. The plan is <code className="text-slate-400">input</code>;
              Terrapod context is <code className="text-slate-400">data.terrapod_context</code>.
            </p>
            <button type="submit" disabled={npSaving}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 text-white transition-colors">
              {npSaving ? 'Validating Rego...' : 'Add Policy'}
            </button>
          </form>
        )}

        {policies.length === 0 ? (
          <p className="text-sm text-slate-500">No policies in this set yet.</p>
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
        throw new Error(d.detail || `Failed to save policy (${res.status})`)
      }
      onChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : 'Failed to save policy')
    } finally {
      setSaving(false)
    }
  }

  async function remove() {
    if (!confirm(`Delete policy "${policy.attributes.name}"?`)) return
    onError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/policies/${policy.id}`, { method: 'DELETE' })
      if (!res.ok && res.status !== 204) throw new Error(`Failed to delete (${res.status})`)
      onChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : 'Failed to delete policy')
    }
  }

  return (
    <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-medium text-slate-200">{policy.attributes.name}</span>
        <button onClick={remove} className="text-xs text-red-400 hover:text-red-300">Delete</button>
      </div>
      <input type="text" value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="Description"
        className="w-full mb-2 px-3 py-1.5 text-sm border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
      <textarea value={rego} onChange={(e) => setRego(e.target.value)} rows={10} spellCheck={false}
        className="w-full px-3 py-2 font-mono text-xs border border-slate-600 rounded-lg bg-slate-900 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500" />
      <button onClick={save} disabled={!dirty || saving}
        className="mt-2 px-3 py-1.5 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-slate-700 disabled:text-slate-500 text-white transition-colors">
        {saving ? 'Validating Rego...' : 'Save Policy'}
      </button>
    </div>
  )
}
