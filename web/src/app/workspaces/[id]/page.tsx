'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter, useParams } from 'next/navigation'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { getAuthState } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

interface WorkspaceAttrs {
  name: string
  'execution-mode': string
  'auto-apply': boolean
  'terraform-version': string
  'working-directory': string
  locked: boolean
  'resource-cpu': string
  'resource-memory': string
  'created-at': string
  'updated-at': string
}

interface Workspace {
  id: string
  attributes: WorkspaceAttrs
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

interface RunItem {
  id: string
  attributes: {
    status: string
    source: string
    message: string
    'created-at': string
    'plan-started-at': string | null
    'apply-finished-at': string | null
  }
}

interface StateVersionItem {
  id: string
  attributes: {
    serial: number
    lineage: string
    md5: string
    size: number
    'created-at': string
  }
}

type Tab = 'overview' | 'variables' | 'runs' | 'state'

export default function WorkspaceDetailPage() {
  const router = useRouter()
  const params = useParams()
  const workspaceId = params.id as string

  const [workspace, setWorkspace] = useState<Workspace | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [activeTab, setActiveTab] = useState<Tab>('overview')

  // Overview editing
  const [editing, setEditing] = useState(false)
  const [editCpu, setEditCpu] = useState('')
  const [editMemory, setEditMemory] = useState('')
  const [editAutoApply, setEditAutoApply] = useState(false)
  const [editExecMode, setEditExecMode] = useState('')
  const [saving, setSaving] = useState(false)

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

  // Runs
  const [runs, setRuns] = useState<RunItem[]>([])
  const [runsLoading, setRunsLoading] = useState(false)

  // State versions
  const [stateVersions, setStateVersions] = useState<StateVersionItem[]>([])
  const [stateLoading, setStateLoading] = useState(false)

  // Delete confirmation
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const loadWorkspace = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}`)
      if (!res.ok) throw new Error('Failed to load workspace')
      const data = await res.json()
      setWorkspace(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load workspace')
    } finally {
      setLoading(false)
    }
  }, [workspaceId])

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadWorkspace()
  }, [router, loadWorkspace])

  // Load tab data when tab changes
  useEffect(() => {
    if (!workspace) return
    if (activeTab === 'variables') loadVariables()
    if (activeTab === 'runs') loadRuns()
    if (activeTab === 'state') loadStateVersions()
  }, [activeTab, workspace])

  async function loadVariables() {
    setVarsLoading(true)
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/vars`)
      if (!res.ok) throw new Error('Failed to load variables')
      const data = await res.json()
      setVariables(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load variables')
    } finally {
      setVarsLoading(false)
    }
  }

  async function loadRuns() {
    setRunsLoading(true)
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/runs`)
      if (!res.ok) throw new Error('Failed to load runs')
      const data = await res.json()
      setRuns(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load runs')
    } finally {
      setRunsLoading(false)
    }
  }

  async function loadStateVersions() {
    setStateLoading(true)
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/state-versions`)
      if (!res.ok) throw new Error('Failed to load state versions')
      const data = await res.json()
      setStateVersions(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load state versions')
    } finally {
      setStateLoading(false)
    }
  }

  function startEditing() {
    if (!workspace) return
    setEditCpu(workspace.attributes['resource-cpu'])
    setEditMemory(workspace.attributes['resource-memory'])
    setEditAutoApply(workspace.attributes['auto-apply'])
    setEditExecMode(workspace.attributes['execution-mode'])
    setEditing(true)
  }

  async function handleSave() {
    setSaving(true)
    setError('')
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'workspaces',
            attributes: {
              'resource-cpu': editCpu,
              'resource-memory': editMemory,
              'auto-apply': editAutoApply,
              'execution-mode': editExecMode,
            },
          },
        }),
      })
      if (!res.ok) throw new Error('Failed to update workspace')
      const data = await res.json()
      setWorkspace(data.data)
      setEditing(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update workspace')
    } finally {
      setSaving(false)
    }
  }

  async function handleLockToggle() {
    if (!workspace) return
    const action = workspace.attributes.locked ? 'unlock' : 'lock'
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/actions/${action}`, {
        method: 'POST',
      })
      if (!res.ok) throw new Error(`Failed to ${action} workspace`)
      await loadWorkspace()
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${action} workspace`)
    }
  }

  async function handleDelete() {
    setDeleting(true)
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete workspace')
      router.push('/workspaces')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete workspace')
      setDeleting(false)
    }
  }

  async function handleAddVariable(e: React.FormEvent) {
    e.preventDefault()
    setAddingVar(true)
    setError('')
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/vars`, {
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

  async function handleDeleteVariable(varId: string) {
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/vars/${varId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete variable')
      await loadVariables()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete variable')
    }
  }

  const tabs: { key: Tab; label: string }[] = [
    { key: 'overview', label: 'Overview' },
    { key: 'variables', label: 'Variables' },
    { key: 'runs', label: 'Runs' },
    { key: 'state', label: 'State' },
  ]

  function statusColor(status: string): string {
    switch (status) {
      case 'applied': return 'bg-green-900/50 text-green-300'
      case 'planned': return 'bg-blue-900/50 text-blue-300'
      case 'planning': case 'applying': return 'bg-yellow-900/50 text-yellow-300'
      case 'errored': return 'bg-red-900/50 text-red-300'
      case 'canceled': case 'discarded': return 'bg-slate-700 text-slate-400'
      default: return 'bg-slate-700 text-slate-400'
    }
  }

  if (loading) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>
  if (!workspace) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><ErrorBanner message="Workspace not found" /></main></>

  const attrs = workspace.attributes

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={attrs.name}
          description={`${attrs['execution-mode']} execution mode`}
        />

        {error && <ErrorBanner message={error} />}

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

        {/* Overview Tab */}
        {activeTab === 'overview' && (
          <div className="space-y-6">
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-sm font-medium text-slate-300">Settings</h3>
                {!editing ? (
                  <button onClick={startEditing} className="text-xs text-brand-400 hover:text-brand-300">
                    Edit
                  </button>
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
                  <dt className="text-xs text-slate-500">Execution Mode</dt>
                  {editing ? (
                    <select value={editExecMode} onChange={(e) => setEditExecMode(e.target.value)} className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500">
                      <option value="local">Local</option>
                      <option value="remote">Remote</option>
                    </select>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['execution-mode']}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Auto Apply</dt>
                  {editing ? (
                    <label className="flex items-center gap-2 mt-1">
                      <input type="checkbox" checked={editAutoApply} onChange={(e) => setEditAutoApply(e.target.checked)} className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                      <span className="text-sm text-slate-200">{editAutoApply ? 'Enabled' : 'Disabled'}</span>
                    </label>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['auto-apply'] ? 'Enabled' : 'Disabled'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">CPU Request</dt>
                  {editing ? (
                    <input type="text" value={editCpu} onChange={(e) => setEditCpu(e.target.value)} className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['resource-cpu']}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Memory Request</dt>
                  {editing ? (
                    <input type="text" value={editMemory} onChange={(e) => setEditMemory(e.target.value)} className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['resource-memory']}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Terraform Version</dt>
                  <dd className="mt-1 text-sm text-slate-200">{attrs['terraform-version'] || 'Default'}</dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Working Directory</dt>
                  <dd className="mt-1 text-sm text-slate-200">{attrs['working-directory'] || '/'}</dd>
                </div>
              </dl>
            </div>

            {/* Lock / Unlock */}
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-sm font-medium text-slate-300">Lock Status</h3>
                  <p className="text-sm text-slate-400 mt-1">
                    {attrs.locked ? 'This workspace is locked. No plans or applies can run.' : 'This workspace is unlocked and ready for runs.'}
                  </p>
                </div>
                <button
                  onClick={handleLockToggle}
                  className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                    attrs.locked
                      ? 'bg-amber-600 hover:bg-amber-500 text-white'
                      : 'bg-slate-600 hover:bg-slate-500 text-slate-200'
                  }`}
                >
                  {attrs.locked ? 'Unlock' : 'Lock'}
                </button>
              </div>
            </div>

            {/* Delete */}
            <div className="bg-slate-800/50 rounded-lg border border-red-900/30 p-6">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-sm font-medium text-red-400">Delete Workspace</h3>
                  <p className="text-sm text-slate-400 mt-1">Permanently delete this workspace and all associated state, variables, and runs.</p>
                </div>
                {!showDeleteConfirm ? (
                  <button
                    onClick={() => setShowDeleteConfirm(true)}
                    className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600/20 hover:bg-red-600/40 text-red-400 transition-colors"
                  >
                    Delete
                  </button>
                ) : (
                  <div className="flex gap-2">
                    <button onClick={() => setShowDeleteConfirm(false)} className="px-3 py-1.5 rounded-lg text-sm font-medium text-slate-400 hover:text-slate-200">
                      Cancel
                    </button>
                    <button
                      onClick={handleDelete}
                      disabled={deleting}
                      className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-500 text-white transition-colors"
                    >
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
                    <input id="var-key" type="text" value={varKey} onChange={(e) => setVarKey(e.target.value)} required placeholder="AWS_REGION" className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="var-val" className="block text-sm font-medium text-slate-300 mb-1">Value</label>
                    <input id="var-val" type="text" value={varValue} onChange={(e) => setVarValue(e.target.value)} placeholder="us-east-1" className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="var-cat" className="block text-sm font-medium text-slate-300 mb-1">Category</label>
                    <select id="var-cat" value={varCategory} onChange={(e) => setVarCategory(e.target.value)} className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      <option value="terraform">Terraform</option>
                      <option value="env">Environment</option>
                    </select>
                  </div>
                  <div className="flex items-end gap-4">
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input type="checkbox" checked={varSensitive} onChange={(e) => setVarSensitive(e.target.checked)} className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                      <span className="text-sm text-slate-300">Sensitive</span>
                    </label>
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input type="checkbox" checked={varHcl} onChange={(e) => setVarHcl(e.target.checked)} className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                      <span className="text-sm text-slate-300">HCL</span>
                    </label>
                  </div>
                </div>
                <button type="submit" disabled={addingVar} className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {addingVar ? 'Adding...' : 'Add Variable'}
                </button>
              </form>
            )}

            {varsLoading ? (
              <LoadingSpinner />
            ) : variables.length === 0 ? (
              <EmptyState message="No variables configured for this workspace." />
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
                    {variables.map((v) => (
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
                          <button onClick={() => handleDeleteVariable(v.id)} className="text-xs text-red-400 hover:text-red-300">
                            Delete
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {/* Runs Tab */}
        {activeTab === 'runs' && (
          <div>
            {runsLoading ? (
              <LoadingSpinner />
            ) : runs.length === 0 ? (
              <EmptyState message="No runs yet for this workspace." />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">Run ID</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">Status</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden sm:table-cell">Source</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden md:table-cell">Created</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {runs.map((run) => (
                      <tr key={run.id} className="hover:bg-slate-700/20 transition-colors">
                        <td className="px-4 py-3 text-sm text-slate-200 font-mono">{run.id}</td>
                        <td className="px-4 py-3">
                          <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${statusColor(run.attributes.status)}`}>
                            {run.attributes.status}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-400 hidden sm:table-cell">{run.attributes.source}</td>
                        <td className="px-4 py-3 text-xs text-slate-500 hidden md:table-cell">
                          {run.attributes['created-at'] ? new Date(run.attributes['created-at']).toLocaleString() : ''}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {/* State Tab */}
        {activeTab === 'state' && (
          <div>
            {stateLoading ? (
              <LoadingSpinner />
            ) : stateVersions.length === 0 ? (
              <EmptyState message="No state versions yet for this workspace." />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">Serial</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden sm:table-cell">Lineage</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden md:table-cell">Size</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden lg:table-cell">Created</th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {stateVersions.map((sv) => (
                      <tr key={sv.id} className="hover:bg-slate-700/20 transition-colors">
                        <td className="px-4 py-3 text-sm text-slate-200 font-mono">#{sv.attributes.serial}</td>
                        <td className="px-4 py-3 text-xs text-slate-400 font-mono hidden sm:table-cell">{sv.attributes.lineage || '-'}</td>
                        <td className="px-4 py-3 text-xs text-slate-400 hidden md:table-cell">
                          {sv.attributes.size > 0 ? `${(sv.attributes.size / 1024).toFixed(1)} KB` : '-'}
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-500 hidden lg:table-cell">
                          {sv.attributes['created-at'] ? new Date(sv.attributes['created-at']).toLocaleString() : ''}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <a
                            href={`/api/v2/state-versions/${sv.id}/download`}
                            className="text-xs text-brand-400 hover:text-brand-300"
                          >
                            Download
                          </a>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </main>
    </>
  )
}
