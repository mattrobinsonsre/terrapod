'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter, useParams } from 'next/navigation'
import Link from 'next/link'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

interface VarsetAttrs {
  name: string
  description: string
  global: boolean
  priority: boolean
  'var-count': number
  'workspace-count': number
  'created-at': string
}

interface Varset {
  id: string
  attributes: VarsetAttrs
}

interface Variable {
  id: string
  attributes: {
    key: string
    value: string
    category: string
    hcl: boolean
    sensitive: boolean
    description: string
  }
}

interface WorkspaceRef {
  id: string
  attributes: {
    name: string
  }
}

type Tab = 'settings' | 'variables' | 'workspaces'

export default function VariableSetDetailPage() {
  const router = useRouter()
  const params = useParams()
  const varsetId = params.id as string

  const [varset, setVarset] = useState<Varset | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [activeTab, setActiveTab] = useState<Tab>('settings')

  // Settings editing
  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const [editGlobal, setEditGlobal] = useState(false)
  const [editPriority, setEditPriority] = useState(false)
  const [saving, setSaving] = useState(false)

  // Delete varset
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [deleting, setDeleting] = useState(false)

  // Variables
  const [variables, setVariables] = useState<Variable[]>([])
  const [varsLoading, setVarsLoading] = useState(false)
  const [showAddVar, setShowAddVar] = useState(false)
  const [varKey, setVarKey] = useState('')
  const [varValue, setVarValue] = useState('')
  const [varCategory, setVarCategory] = useState('terraform')
  const [varSensitive, setVarSensitive] = useState(false)
  const [varHcl, setVarHcl] = useState(false)
  const [addingVar, setAddingVar] = useState(false)

  // Variable editing
  const [editingVarId, setEditingVarId] = useState<string | null>(null)
  const [editVarKey, setEditVarKey] = useState('')
  const [editVarValue, setEditVarValue] = useState('')
  const [editVarCategory, setEditVarCategory] = useState('terraform')
  const [editVarSensitive, setEditVarSensitive] = useState(false)
  const [editVarHcl, setEditVarHcl] = useState(false)
  const [savingVar, setSavingVar] = useState(false)

  // Workspaces
  const [workspaces, setWorkspaces] = useState<WorkspaceRef[]>([])
  const [wsLoading, setWsLoading] = useState(false)
  const [allWorkspaces, setAllWorkspaces] = useState<WorkspaceRef[]>([])
  const [showAddWs, setShowAddWs] = useState(false)
  const [selectedWsId, setSelectedWsId] = useState('')
  const [addingWs, setAddingWs] = useState(false)

  const loadVarset = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/v2/varsets/${varsetId}`)
      if (!res.ok) throw new Error('Failed to load variable set')
      const data = await res.json()
      setVarset(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load variable set')
    } finally {
      setLoading(false)
    }
  }, [varsetId])

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadVarset()
  }, [router, loadVarset])

  useEffect(() => {
    if (!varset) return
    if (activeTab === 'variables') loadVariables()
    if (activeTab === 'workspaces') { loadWorkspaces(); loadAllWorkspaces() }
  }, [activeTab, varset])

  async function loadVariables() {
    setVarsLoading(true)
    try {
      const res = await apiFetch(`/api/v2/varsets/${varsetId}/relationships/vars`)
      if (!res.ok) throw new Error('Failed to load variables')
      const data = await res.json()
      setVariables(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load variables')
    } finally {
      setVarsLoading(false)
    }
  }

  async function loadWorkspaces() {
    setWsLoading(true)
    try {
      // Varset show response may include workspace relationships
      // Re-fetch varset to get workspace list
      const res = await apiFetch(`/api/v2/varsets/${varsetId}`)
      if (!res.ok) throw new Error('Failed to load workspaces')
      const data = await res.json()
      setWorkspaces(data.data?.relationships?.workspaces?.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load workspaces')
    } finally {
      setWsLoading(false)
    }
  }

  async function loadAllWorkspaces() {
    try {
      const res = await apiFetch('/api/v2/organizations/default/workspaces')
      if (!res.ok) return
      const data = await res.json()
      setAllWorkspaces(data.data || [])
    } catch {
      // Non-critical
    }
  }

  function startEditing() {
    if (!varset) return
    setEditName(varset.attributes.name)
    setEditDesc(varset.attributes.description || '')
    setEditGlobal(varset.attributes.global)
    setEditPriority(varset.attributes.priority)
    setEditing(true)
  }

  async function handleSave() {
    setSaving(true)
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/v2/varsets/${varsetId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'varsets',
            attributes: {
              name: editName,
              description: editDesc,
              global: editGlobal,
              priority: editPriority,
            },
          },
        }),
      })
      if (!res.ok) throw new Error('Failed to update variable set')
      const data = await res.json()
      setVarset(data.data)
      setEditing(false)
      setSuccess('Variable set updated')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update variable set')
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete() {
    setDeleting(true)
    try {
      const res = await apiFetch(`/api/v2/varsets/${varsetId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete variable set')
      router.push('/admin/variable-sets')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete variable set')
      setDeleting(false)
    }
  }

  async function handleAddVariable(e: React.FormEvent) {
    e.preventDefault()
    setAddingVar(true)
    setError('')
    try {
      const res = await apiFetch(`/api/v2/varsets/${varsetId}/relationships/vars`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'vars',
            attributes: {
              key: varKey,
              value: varValue,
              category: varCategory,
              sensitive: varSensitive,
              hcl: varHcl,
            },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to add variable (${res.status})`)
      }
      setVarKey('')
      setVarValue('')
      setVarCategory('terraform')
      setVarSensitive(false)
      setVarHcl(false)
      setShowAddVar(false)
      await loadVariables()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add variable')
    } finally {
      setAddingVar(false)
    }
  }

  function startEditingVar(v: Variable) {
    setEditingVarId(v.id)
    setEditVarKey(v.attributes.key)
    setEditVarValue(v.attributes.sensitive ? '' : v.attributes.value)
    setEditVarCategory(v.attributes.category)
    setEditVarSensitive(v.attributes.sensitive)
    setEditVarHcl(v.attributes.hcl)
  }

  async function handleSaveVar() {
    if (!editingVarId) return
    setSavingVar(true)
    setError('')
    try {
      const attrs: Record<string, unknown> = {
        key: editVarKey,
        category: editVarCategory,
        sensitive: editVarSensitive,
        hcl: editVarHcl,
      }
      if (editVarValue !== '') attrs.value = editVarValue
      const res = await apiFetch(`/api/v2/varsets/${varsetId}/relationships/vars/${editingVarId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'vars', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Failed to update variable')
      }
      setEditingVarId(null)
      await loadVariables()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update variable')
    } finally {
      setSavingVar(false)
    }
  }

  async function handleDeleteVariable(varId: string) {
    setError('')
    try {
      const res = await apiFetch(`/api/v2/varsets/${varsetId}/relationships/vars/${varId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete variable')
      await loadVariables()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete variable')
    }
  }

  async function handleAddWorkspace(e: React.FormEvent) {
    e.preventDefault()
    if (!selectedWsId) return
    setAddingWs(true)
    setError('')
    try {
      const res = await apiFetch(`/api/v2/varsets/${varsetId}/relationships/workspaces`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: [{ id: selectedWsId, type: 'workspaces' }],
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Failed to add workspace')
      }
      setSelectedWsId('')
      setShowAddWs(false)
      setSuccess('Workspace added')
      await loadWorkspaces()
      await loadVarset()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add workspace')
    } finally {
      setAddingWs(false)
    }
  }

  async function handleRemoveWorkspace(wsId: string) {
    setError('')
    try {
      const res = await apiFetch(`/api/v2/varsets/${varsetId}/relationships/workspaces`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: [{ id: wsId, type: 'workspaces' }],
        }),
      })
      if (!res.ok) throw new Error('Failed to remove workspace')
      setSuccess('Workspace removed')
      await loadWorkspaces()
      await loadVarset()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to remove workspace')
    }
  }

  const tabs: { key: Tab; label: string }[] = [
    { key: 'settings', label: 'Settings' },
    { key: 'variables', label: 'Variables' },
    { key: 'workspaces', label: 'Workspaces' },
  ]

  if (loading) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>
  if (!varset) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><ErrorBanner message="Variable set not found" /></main></>

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <div className="mb-4">
          <Link href="/admin/variable-sets" className="text-sm text-slate-400 hover:text-slate-200">
            &larr; Back to variable sets
          </Link>
        </div>

        <PageHeader
          title={varset.attributes.name}
          description={varset.attributes.description || 'Variable set'}
        />

        {error && <ErrorBanner message={error} />}
        {success && (
          <div className="mb-4 p-3 bg-green-900/30 text-green-400 rounded-lg text-sm border border-green-800/50">{success}</div>
        )}

        {/* Tabs */}
        <div className="border-b border-slate-700/50 mb-6">
          <div className="flex gap-1 -mb-px">
            {tabs.map((tab) => (
              <button
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
                className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                  activeTab === tab.key
                    ? 'border-brand-500 text-brand-400'
                    : 'border-transparent text-slate-400 hover:text-slate-200 hover:border-slate-600'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>

        {/* Settings Tab */}
        {activeTab === 'settings' && (
          <div className="space-y-6">
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-sm font-medium text-slate-300">Settings</h3>
                {!editing ? (
                  <button onClick={startEditing} className="text-xs text-brand-400 hover:text-brand-300">Edit</button>
                ) : (
                  <div className="flex gap-2">
                    <button onClick={() => setEditing(false)} className="text-xs text-slate-400 hover:text-slate-200">Cancel</button>
                    <button onClick={handleSave} disabled={saving} className="text-xs text-brand-400 hover:text-brand-300">
                      {saving ? 'Saving...' : 'Save'}
                    </button>
                  </div>
                )}
              </div>
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <dt className="text-xs text-slate-500">Name</dt>
                  {editing ? (
                    <input type="text" value={editName} onChange={(e) => setEditName(e.target.value)}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{varset.attributes.name}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Description</dt>
                  {editing ? (
                    <input type="text" value={editDesc} onChange={(e) => setEditDesc(e.target.value)}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{varset.attributes.description || '-'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Global</dt>
                  {editing ? (
                    <label className="flex items-center gap-2 mt-1">
                      <input type="checkbox" checked={editGlobal} onChange={(e) => setEditGlobal(e.target.checked)}
                        className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                      <span className="text-sm text-slate-200">{editGlobal ? 'Yes' : 'No'}</span>
                    </label>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{varset.attributes.global ? 'Yes' : 'No'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Priority</dt>
                  {editing ? (
                    <label className="flex items-center gap-2 mt-1">
                      <input type="checkbox" checked={editPriority} onChange={(e) => setEditPriority(e.target.checked)}
                        className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                      <span className="text-sm text-slate-200">{editPriority ? 'Yes' : 'No'}</span>
                    </label>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{varset.attributes.priority ? 'Yes' : 'No'}</dd>
                  )}
                </div>
              </dl>
            </div>

            <div className="bg-slate-800/50 rounded-lg border border-red-900/30 p-6">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-sm font-medium text-red-400">Delete Variable Set</h3>
                  <p className="text-sm text-slate-400 mt-1">Permanently delete this variable set and all its variables.</p>
                </div>
                {!showDeleteConfirm ? (
                  <button onClick={() => setShowDeleteConfirm(true)}
                    className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600/20 hover:bg-red-600/40 text-red-400 transition-colors">
                    Delete
                  </button>
                ) : (
                  <div className="flex gap-2">
                    <button onClick={() => setShowDeleteConfirm(false)} className="px-3 py-1.5 rounded-lg text-sm font-medium text-slate-400 hover:text-slate-200">Cancel</button>
                    <button onClick={handleDelete} disabled={deleting}
                      className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-500 text-white transition-colors">
                      {deleting ? 'Deleting...' : 'Confirm Delete'}
                    </button>
                  </div>
                )}
              </div>
            </div>
          </div>
        )}

        {/* Variables Tab */}
        {activeTab === 'variables' && (
          <div>
            <div className="flex justify-end mb-4">
              <button
                onClick={() => setShowAddVar(!showAddVar)}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors"
              >
                {showAddVar ? 'Cancel' : 'Add Variable'}
              </button>
            </div>

            {showAddVar && (
              <form onSubmit={handleAddVariable} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="var-key" className="block text-sm font-medium text-slate-300 mb-1">Key</label>
                    <input id="var-key" type="text" value={varKey} onChange={(e) => setVarKey(e.target.value)} required placeholder="AWS_REGION"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="var-val" className="block text-sm font-medium text-slate-300 mb-1">Value</label>
                    <input id="var-val" type="text" value={varValue} onChange={(e) => setVarValue(e.target.value)} placeholder="us-east-1"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="var-cat" className="block text-sm font-medium text-slate-300 mb-1">Category</label>
                    <select id="var-cat" value={varCategory} onChange={(e) => setVarCategory(e.target.value)}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      <option value="terraform">Terraform</option>
                      <option value="env">Environment</option>
                    </select>
                  </div>
                  <div className="flex items-end gap-4">
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input type="checkbox" checked={varSensitive} onChange={(e) => setVarSensitive(e.target.checked)}
                        className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                      <span className="text-sm text-slate-300">Sensitive</span>
                    </label>
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input type="checkbox" checked={varHcl} onChange={(e) => setVarHcl(e.target.checked)}
                        className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                      <span className="text-sm text-slate-300">HCL</span>
                    </label>
                  </div>
                </div>
                <button type="submit" disabled={addingVar}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {addingVar ? 'Adding...' : 'Add Variable'}
                </button>
              </form>
            )}

            {varsLoading ? (
              <LoadingSpinner />
            ) : variables.length === 0 ? (
              <EmptyState message="No variables in this set." />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">Key</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">Value</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden sm:table-cell">Category</th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {variables.map((v) =>
                      editingVarId === v.id ? (
                        <tr key={v.id} className="bg-slate-700/20">
                          <td className="px-4 py-3">
                            <input type="text" value={editVarKey} onChange={(e) => setEditVarKey(e.target.value)}
                              className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 font-mono focus:outline-none focus:ring-1 focus:ring-brand-500" />
                          </td>
                          <td className="px-4 py-3">
                            <input type="text" value={editVarValue} onChange={(e) => setEditVarValue(e.target.value)}
                              placeholder={editVarSensitive ? 'Enter new value' : ''}
                              className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 font-mono focus:outline-none focus:ring-1 focus:ring-brand-500" />
                          </td>
                          <td className="px-4 py-3 hidden sm:table-cell">
                            <div className="flex items-center gap-3">
                              <select value={editVarCategory} onChange={(e) => setEditVarCategory(e.target.value)}
                                className="px-2 py-1 text-xs border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500">
                                <option value="terraform">terraform</option>
                                <option value="env">env</option>
                              </select>
                              <label className="flex items-center gap-1 cursor-pointer">
                                <input type="checkbox" checked={editVarSensitive} onChange={(e) => setEditVarSensitive(e.target.checked)}
                                  className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                                <span className="text-xs text-slate-400">Sens.</span>
                              </label>
                              <label className="flex items-center gap-1 cursor-pointer">
                                <input type="checkbox" checked={editVarHcl} onChange={(e) => setEditVarHcl(e.target.checked)}
                                  className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                                <span className="text-xs text-slate-400">HCL</span>
                              </label>
                            </div>
                          </td>
                          <td className="px-4 py-3 text-right">
                            <div className="flex justify-end gap-2">
                              <button onClick={() => setEditingVarId(null)} className="text-xs text-slate-400 hover:text-slate-200">Cancel</button>
                              <button onClick={handleSaveVar} disabled={savingVar} className="text-xs text-brand-400 hover:text-brand-300">
                                {savingVar ? 'Saving...' : 'Save'}
                              </button>
                            </div>
                          </td>
                        </tr>
                      ) : (
                        <tr key={v.id} className="hover:bg-slate-700/20 transition-colors">
                          <td className="px-4 py-3 text-sm text-slate-200 font-mono">{v.attributes.key}</td>
                          <td className="px-4 py-3 text-sm text-slate-400 font-mono">
                            {v.attributes.sensitive ? '***' : (v.attributes.value || <span className="text-slate-600 italic">empty</span>)}
                          </td>
                          <td className="px-4 py-3 text-xs text-slate-400 hidden sm:table-cell">
                            <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                              v.attributes.category === 'terraform' ? 'bg-purple-900/50 text-purple-300' : 'bg-cyan-900/50 text-cyan-300'
                            }`}>
                              {v.attributes.category}
                            </span>
                          </td>
                          <td className="px-4 py-3 text-right">
                            <div className="flex justify-end gap-2">
                              <button onClick={() => startEditingVar(v)} className="text-xs text-brand-400 hover:text-brand-300">Edit</button>
                              <button onClick={() => handleDeleteVariable(v.id)} className="text-xs text-red-400 hover:text-red-300">Delete</button>
                            </div>
                          </td>
                        </tr>
                      )
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {/* Workspaces Tab */}
        {activeTab === 'workspaces' && (
          <div>
            {!varset.attributes.global && (
              <div className="flex justify-end mb-4">
                <button
                  onClick={() => setShowAddWs(!showAddWs)}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors"
                >
                  {showAddWs ? 'Cancel' : 'Add Workspace'}
                </button>
              </div>
            )}

            {varset.attributes.global && (
              <div className="mb-4 p-3 bg-blue-900/20 text-blue-300 rounded-lg text-sm border border-blue-800/50">
                This variable set is global and applies to all workspaces automatically.
              </div>
            )}

            {showAddWs && (
              <form onSubmit={handleAddWorkspace} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 flex items-end gap-3">
                <div className="flex-1">
                  <label htmlFor="ws-select" className="block text-sm font-medium text-slate-300 mb-1">Workspace</label>
                  <select id="ws-select" value={selectedWsId} onChange={(e) => setSelectedWsId(e.target.value)}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                    <option value="">Select a workspace...</option>
                    {allWorkspaces
                      .filter((ws) => !workspaces.some((assigned) => assigned.id === ws.id))
                      .map((ws) => (
                        <option key={ws.id} value={ws.id}>{ws.attributes.name}</option>
                      ))}
                  </select>
                </div>
                <button type="submit" disabled={addingWs || !selectedWsId}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {addingWs ? 'Adding...' : 'Add'}
                </button>
              </form>
            )}

            {wsLoading ? (
              <LoadingSpinner />
            ) : workspaces.length === 0 && !varset.attributes.global ? (
              <EmptyState message="No workspaces assigned to this variable set." />
            ) : workspaces.length > 0 ? (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">Workspace</th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {workspaces.map((ws) => (
                      <tr key={ws.id} className="hover:bg-slate-700/20 transition-colors">
                        <td className="px-4 py-3">
                          <Link href={`/workspaces/${ws.id}`} className="text-sm font-medium text-brand-400 hover:text-brand-300">
                            {ws.attributes?.name || ws.id}
                          </Link>
                        </td>
                        <td className="px-4 py-3 text-right">
                          <button onClick={() => handleRemoveWorkspace(ws.id)} className="text-xs text-red-400 hover:text-red-300">Remove</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </div>
        )}
      </main>
    </>
  )
}
