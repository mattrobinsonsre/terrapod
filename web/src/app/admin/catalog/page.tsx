'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { LabelsEditor } from '@/components/labels-editor'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

interface CatalogItem {
  id: string
  attributes: {
    name: string
    'display-name': string
    description: string
    enabled: boolean
    'module-id': string
    'module-name': string
    'module-provider': string
    'default-version-pin': string | null
    'provider-template-ids': string[]
    'allowed-agent-pool-ids': string[] | null
    'variable-options': unknown[]
    labels: Record<string, string>
    'owner-email': string
  }
}

interface ModuleOption {
  id: string
  attributes: { name: string; provider: string }
}

interface ProviderTemplate {
  id: string
  attributes: { name: string; 'provider-type': string }
}

interface AgentPool {
  id: string
  attributes: { name: string }
}

const EMPTY_FORM = {
  name: '',
  moduleId: '',
  displayName: '',
  description: '',
  enabled: true,
  defaultVersionPin: '',
  providerTemplateIds: [] as string[],
  // null sentinel: "any pool". An empty array means "explicitly none allowed".
  allowedPoolIds: null as string[] | null,
  labels: {} as Record<string, string>,
  variableOptionsJson: '[]',
}

export default function AdminCatalogPage() {
  const router = useRouter()
  const [items, setItems] = useState<CatalogItem[]>([])
  const [modules, setModules] = useState<ModuleOption[]>([])
  const [templates, setTemplates] = useState<ProviderTemplate[]>([])
  const [pools, setPools] = useState<AgentPool[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [disabled, setDisabled] = useState(false)

  // Create / edit form
  const [showForm, setShowForm] = useState(false)
  const [editId, setEditId] = useState<string | null>(null)
  const [f, setF] = useState({ ...EMPTY_FORM })
  const [restrictPools, setRestrictPools] = useState(false)
  const [saving, setSaving] = useState(false)

  // Delete confirmation
  const [deleteId, setDeleteId] = useState<string | null>(null)

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadAll()
  }, [router])

  async function loadAll() {
    try {
      const res = await apiFetch('/api/terrapod/v1/catalog-items')
      if (res.status === 404) {
        setDisabled(true)
        setItems([])
        return
      }
      if (!res.ok) throw new Error('Failed to load catalog items')
      const data = await res.json()
      setDisabled(false)
      setItems(data.data || [])

      // Best-effort load of pickers; don't fail the page if any are empty.
      const [modRes, tmplRes, poolRes] = await Promise.all([
        apiFetch('/api/terrapod/v1/registry-modules'),
        apiFetch('/api/terrapod/v1/provider-templates'),
        apiFetch('/api/terrapod/v1/agent-pools'),
      ])
      if (modRes.ok) setModules((await modRes.json()).data || [])
      if (tmplRes.ok) setTemplates((await tmplRes.json()).data || [])
      if (poolRes.ok) setPools((await poolRes.json()).data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load catalog items')
    } finally {
      setLoading(false)
    }
  }

  function resetForm() {
    setF({ ...EMPTY_FORM })
    setRestrictPools(false)
    setEditId(null)
  }

  function startCreate() {
    resetForm()
    setShowForm(true)
    setError(''); setSuccess('')
  }

  function startEdit(item: CatalogItem) {
    const a = item.attributes
    setEditId(item.id)
    setF({
      name: a.name,
      moduleId: a['module-id'],
      displayName: a['display-name'] || '',
      description: a.description || '',
      enabled: a.enabled,
      defaultVersionPin: a['default-version-pin'] || '',
      providerTemplateIds: a['provider-template-ids'] || [],
      allowedPoolIds: a['allowed-agent-pool-ids'],
      labels: a.labels || {},
      variableOptionsJson: JSON.stringify(a['variable-options'] || [], null, 2),
    })
    setRestrictPools(a['allowed-agent-pool-ids'] !== null)
    setShowForm(true)
    setError(''); setSuccess('')
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError(''); setSuccess('')
    try {
      const editing = editId !== null

      // Parse the advanced variable-options JSON.
      let variableOptions: unknown
      try {
        variableOptions = JSON.parse(f.variableOptionsJson || '[]')
      } catch {
        throw new Error('Variable options must be valid JSON (an array).')
      }
      if (!Array.isArray(variableOptions)) {
        throw new Error('Variable options must be a JSON array.')
      }

      const attrs: Record<string, unknown> = {
        'display-name': f.displayName,
        description: f.description,
        enabled: f.enabled,
        'default-version-pin': f.defaultVersionPin || null,
        'provider-template-ids': f.providerTemplateIds,
        'allowed-agent-pool-ids': restrictPools ? (f.allowedPoolIds ?? []) : null,
        'variable-options': variableOptions,
        labels: f.labels,
      }
      if (!editing) {
        attrs.name = f.name
        attrs['module-id'] = f.moduleId
      }

      const url = editing
        ? `/api/terrapod/v1/catalog-items/${editId}`
        : '/api/terrapod/v1/catalog-items'
      const res = await apiFetch(url, {
        method: editing ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'catalog-items', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to ${editing ? 'update' : 'create'} catalog item (${res.status})`)
      }
      setSuccess(`Catalog item "${editing ? f.name : attrs.name}" ${editing ? 'updated' : 'created'}`)
      resetForm()
      setShowForm(false)
      await loadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save catalog item')
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete(id: string) {
    setError(''); setSuccess('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/catalog-items/${id}`, { method: 'DELETE' })
      if (res.status === 409) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Cannot delete: this catalog item has provisioned instances.')
      }
      if (!res.ok) throw new Error('Failed to delete catalog item')
      setDeleteId(null)
      setSuccess('Catalog item deleted')
      await loadAll()
    } catch (err) {
      setDeleteId(null)
      setError(err instanceof Error ? err.message : 'Failed to delete catalog item')
    }
  }

  function toggleTemplate(id: string) {
    setF((prev) => ({
      ...prev,
      providerTemplateIds: prev.providerTemplateIds.includes(id)
        ? prev.providerTemplateIds.filter((x) => x !== id)
        : [...prev.providerTemplateIds, id],
    }))
  }

  function togglePool(id: string) {
    setF((prev) => {
      const current = prev.allowedPoolIds ?? []
      return {
        ...prev,
        allowedPoolIds: current.includes(id) ? current.filter((x) => x !== id) : [...current, id],
      }
    })
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="Catalog Admin"
          description="Manage service catalog items"
          actions={!disabled ? (
            <button
              onClick={() => { if (showForm) { setShowForm(false); resetForm() } else startCreate() }}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showForm ? 'Cancel' : 'New Catalog Item'}
            </button>
          ) : undefined}
        />

        {error && <ErrorBanner message={error} />}
        {success && (
          <div className="mb-4 p-3 bg-green-900/30 text-green-400 rounded-lg text-sm border border-green-800/50">{success}</div>
        )}

        {disabled ? (
          <div className="p-4 bg-slate-800/50 text-slate-400 rounded-lg text-sm border border-slate-700/50">
            Service catalog is not enabled.
          </div>
        ) : (
          <>
            {showForm && (
              <form onSubmit={handleSubmit} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-4">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="cat-name" className="block text-sm font-medium text-slate-300 mb-1">Name</label>
                    <input id="cat-name" type="text" value={f.name} disabled={editId !== null}
                      onChange={(e) => setF({ ...f, name: e.target.value })} required
                      pattern="[a-zA-Z0-9][a-zA-Z0-9_\-]*"
                      title="Letters, numbers, hyphens, and underscores only. Must start with a letter or number."
                      placeholder="standard-bucket"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 disabled:opacity-60 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="cat-module" className="block text-sm font-medium text-slate-300 mb-1">Module</label>
                    <select id="cat-module" value={f.moduleId} disabled={editId !== null}
                      onChange={(e) => setF({ ...f, moduleId: e.target.value })} required
                      title={editId !== null ? 'Module is immutable after creation' : undefined}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 disabled:opacity-60 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      <option value="">Select a module…</option>
                      {modules.map((m) => (
                        <option key={m.id} value={m.id}>{m.attributes.name}/{m.attributes.provider}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label htmlFor="cat-display" className="block text-sm font-medium text-slate-300 mb-1">Display name</label>
                    <input id="cat-display" type="text" value={f.displayName}
                      onChange={(e) => setF({ ...f, displayName: e.target.value })}
                      placeholder="Standard S3 Bucket"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="cat-pin" className="block text-sm font-medium text-slate-300 mb-1">Default version pin</label>
                    <input id="cat-pin" type="text" value={f.defaultVersionPin}
                      onChange={(e) => setF({ ...f, defaultVersionPin: e.target.value })}
                      placeholder="e.g. 1.2.0 (empty = latest)"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                </div>

                <div>
                  <label htmlFor="cat-desc" className="block text-sm font-medium text-slate-300 mb-1">Description</label>
                  <textarea id="cat-desc" value={f.description} rows={2}
                    onChange={(e) => setF({ ...f, description: e.target.value })}
                    placeholder="What this catalog item provisions…"
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                </div>

                <label className="flex items-center gap-2 cursor-pointer">
                  <input type="checkbox" checked={f.enabled}
                    onChange={(e) => setF({ ...f, enabled: e.target.checked })}
                    className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                  <span className="text-sm text-slate-300">Enabled (visible in the browse catalog)</span>
                </label>

                {/* Provider templates multi-select */}
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">Provider templates</label>
                  {templates.length === 0 ? (
                    <p className="text-xs text-slate-500">No provider templates defined.</p>
                  ) : (
                    <div className="flex flex-wrap gap-2">
                      {templates.map((t) => {
                        const on = f.providerTemplateIds.includes(t.id)
                        return (
                          <button key={t.id} type="button" onClick={() => toggleTemplate(t.id)}
                            className={'px-2.5 py-1 rounded-full text-xs font-medium border transition-colors ' +
                              (on ? 'bg-brand-700/60 text-brand-100 border-brand-600' : 'bg-slate-700/40 text-slate-400 border-slate-600 hover:text-slate-200')}>
                            {t.attributes.name} <span className="text-slate-500">({t.attributes['provider-type']})</span>
                          </button>
                        )
                      })}
                    </div>
                  )}
                </div>

                {/* Allowed agent pools */}
                <div>
                  <label className="flex items-center gap-2 cursor-pointer mb-2">
                    <input type="checkbox" checked={restrictPools}
                      onChange={(e) => setRestrictPools(e.target.checked)}
                      className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                    <span className="text-sm text-slate-300">Restrict to specific agent pools</span>
                  </label>
                  {restrictPools ? (
                    pools.length === 0 ? (
                      <p className="text-xs text-slate-500">No agent pools available.</p>
                    ) : (
                      <div className="flex flex-wrap gap-2">
                        {pools.map((p) => {
                          const on = (f.allowedPoolIds ?? []).includes(p.id)
                          return (
                            <button key={p.id} type="button" onClick={() => togglePool(p.id)}
                              className={'px-2.5 py-1 rounded-full text-xs font-medium border transition-colors ' +
                                (on ? 'bg-brand-700/60 text-brand-100 border-brand-600' : 'bg-slate-700/40 text-slate-400 border-slate-600 hover:text-slate-200')}>
                              {p.attributes.name}
                            </button>
                          )
                        })}
                      </div>
                    )
                  ) : (
                    <p className="text-xs text-slate-500">Any agent pool may be used.</p>
                  )}
                </div>

                {/* Labels */}
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">Labels</label>
                  <LabelsEditor labels={f.labels} onChange={(labels) => setF({ ...f, labels })} />
                </div>

                {/* Advanced: variable-options JSON */}
                <div>
                  <label htmlFor="cat-varopts" className="block text-sm font-medium text-slate-300 mb-1">Variable options (advanced, JSON)</label>
                  <textarea id="cat-varopts" value={f.variableOptionsJson} rows={5}
                    onChange={(e) => setF({ ...f, variableOptionsJson: e.target.value })}
                    placeholder='[{"name": "region", "options": ["us-east-1", "eu-west-1"]}]'
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 font-mono text-xs focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  <p className="mt-1 text-xs text-slate-500">Per-input overrides (options, hidden, default). Leave as <code>[]</code> if unused.</p>
                </div>

                <button type="submit" disabled={saving}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {saving ? (editId !== null ? 'Saving…' : 'Creating…') : (editId !== null ? 'Save Changes' : 'Create Catalog Item')}
                </button>
              </form>
            )}

            {loading ? (
              <LoadingSpinner />
            ) : items.length === 0 ? (
              <EmptyState message="No catalog items yet." />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">Name</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden sm:table-cell">Module</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden md:table-cell">Enabled</th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {items.map((item) => (
                      <tr key={item.id} className="hover:bg-slate-700/20 transition-colors">
                        <td className="px-4 py-3">
                          <Link href={`/catalog/${item.id}`} className="text-sm font-medium text-brand-400 hover:text-brand-300">
                            {item.attributes['display-name'] || item.attributes.name}
                          </Link>
                          <span className="block text-xs text-slate-500">{item.attributes.name}</span>
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-400 hidden sm:table-cell">
                          {item.attributes['module-name']}{item.attributes['module-provider'] ? `/${item.attributes['module-provider']}` : ''}
                        </td>
                        <td className="px-4 py-3 hidden md:table-cell">
                          <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${item.attributes.enabled ? 'bg-green-900/50 text-green-300' : 'bg-slate-700 text-slate-400'}`}>
                            {item.attributes.enabled ? 'enabled' : 'disabled'}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-right">
                          {deleteId === item.id ? (
                            <div className="flex justify-end gap-2">
                              <button onClick={() => setDeleteId(null)} className="text-xs text-slate-400 hover:text-slate-200">Cancel</button>
                              <button onClick={() => handleDelete(item.id)} className="text-xs text-red-400 hover:text-red-300">Confirm</button>
                            </div>
                          ) : (
                            <div className="flex justify-end gap-3">
                              <button onClick={() => startEdit(item)} className="text-xs text-brand-400 hover:text-brand-300">Edit</button>
                              <button onClick={() => setDeleteId(item.id)} className="text-xs text-red-400 hover:text-red-300">Delete</button>
                            </div>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </main>
    </>
  )
}
