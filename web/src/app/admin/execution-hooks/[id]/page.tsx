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
import { usePollingInterval } from '@/lib/use-polling-interval'

const HOOK_POINTS = ['pre_init', 'pre_plan', 'post_plan', 'pre_apply', 'post_apply'] as const

interface HookAttrs {
  name: string
  description: string
  'hook-point': string
  script: string
  enabled: boolean
  priority: number
  'workspace-count': number
  'created-at': string
}

interface Hook {
  id: string
  attributes: HookAttrs
  relationships?: { workspaces?: { data?: WorkspaceRef[] } }
}

interface WorkspaceRef {
  id: string
  attributes?: { name?: string }
}

type Tab = 'settings' | 'workspaces'

export default function ExecutionHookDetailPage() {
  const router = useRouter()
  const params = useParams()
  const hookId = params.id as string

  const [hook, setHook] = useState<Hook | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [activeTab, setActiveTab] = useState<Tab>('settings')

  // Settings editing
  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const [editPoint, setEditPoint] = useState<string>('pre_init')
  const [editScript, setEditScript] = useState('')
  const [editEnabled, setEditEnabled] = useState(true)
  const [editPriority, setEditPriority] = useState(0)
  const [saving, setSaving] = useState(false)

  // Delete
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [deleting, setDeleting] = useState(false)

  // Workspaces
  const [wsLoading, setWsLoading] = useState(false)
  const [allWorkspaces, setAllWorkspaces] = useState<WorkspaceRef[]>([])
  const [showAddWs, setShowAddWs] = useState(false)
  const [selectedWsId, setSelectedWsId] = useState('')
  const [addingWs, setAddingWs] = useState(false)

  const loadHook = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/terrapod/v1/execution-hooks/${hookId}`)
      if (!res.ok) throw new Error('Failed to load execution hook')
      const data = await res.json()
      setHook(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load execution hook')
    } finally {
      setLoading(false)
    }
  }, [hookId])

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadHook()
  }, [router, loadHook])

  usePollingInterval(!loading, 60_000, loadHook)

  useEffect(() => {
    if (!hook) return
    if (activeTab === 'workspaces') loadAllWorkspaces()
  }, [activeTab, hook])

  async function loadAllWorkspaces() {
    setWsLoading(true)
    try {
      const res = await apiFetch('/api/v2/organizations/default/workspaces')
      if (res.ok) {
        const data = await res.json()
        setAllWorkspaces(data.data || [])
      }
    } catch {
      // Non-critical
    } finally {
      setWsLoading(false)
    }
  }

  function startEditing() {
    if (!hook) return
    setEditName(hook.attributes.name)
    setEditDesc(hook.attributes.description || '')
    setEditPoint(hook.attributes['hook-point'])
    setEditScript(hook.attributes.script || '')
    setEditEnabled(hook.attributes.enabled)
    setEditPriority(hook.attributes.priority ?? 0)
    setEditing(true)
  }

  async function handleSave() {
    setSaving(true)
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/execution-hooks/${hookId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'execution-hooks',
            attributes: {
              name: editName,
              description: editDesc,
              'hook-point': editPoint,
              script: editScript,
              enabled: editEnabled,
              priority: Number(editPriority) || 0,
            },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Failed to update execution hook')
      }
      const data = await res.json()
      setHook(data.data)
      setEditing(false)
      setSuccess('Execution hook updated')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update execution hook')
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete() {
    setDeleting(true)
    try {
      const res = await apiFetch(`/api/terrapod/v1/execution-hooks/${hookId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete execution hook')
      router.push('/admin/execution-hooks')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete execution hook')
      setDeleting(false)
    }
  }

  async function handleAddWorkspace(e: React.FormEvent) {
    e.preventDefault()
    if (!selectedWsId) return
    setAddingWs(true)
    setError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/execution-hooks/${hookId}/relationships/workspaces`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: [{ id: selectedWsId, type: 'workspaces' }] }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Failed to add workspace')
      }
      setSelectedWsId('')
      setShowAddWs(false)
      setSuccess('Workspace associated')
      await loadHook()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add workspace')
    } finally {
      setAddingWs(false)
    }
  }

  async function handleRemoveWorkspace(wsId: string) {
    setError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/execution-hooks/${hookId}/relationships/workspaces`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: [{ id: wsId, type: 'workspaces' }] }),
      })
      if (!res.ok) throw new Error('Failed to remove workspace')
      setSuccess('Workspace removed')
      await loadHook()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to remove workspace')
    }
  }

  const tabs: { key: Tab; label: string }[] = [
    { key: 'settings', label: 'Settings' },
    { key: 'workspaces', label: 'Workspaces' },
  ]

  if (loading) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>
  if (!hook) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><ErrorBanner message="Execution hook not found" /></main></>

  const assigned = hook.relationships?.workspaces?.data || []
  const wsName = (id: string) => allWorkspaces.find((w) => w.id === id)?.attributes?.name || id

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <div className="mb-4">
          <Link href="/admin/execution-hooks" className="text-sm text-slate-400 hover:text-slate-200">
            &larr; Back to execution hooks
          </Link>
        </div>

        <PageHeader
          title={hook.attributes.name}
          description={hook.attributes.description || 'Execution hook'}
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
                    <dd className="mt-1 text-sm text-slate-200">{hook.attributes.name}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Description</dt>
                  {editing ? (
                    <input type="text" value={editDesc} onChange={(e) => setEditDesc(e.target.value)}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{hook.attributes.description || '-'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Hook Point</dt>
                  {editing ? (
                    <select value={editPoint} onChange={(e) => setEditPoint(e.target.value)}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500">
                      {HOOK_POINTS.map((p) => <option key={p} value={p}>{p}</option>)}
                    </select>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200 font-mono">{hook.attributes['hook-point']}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Priority</dt>
                  {editing ? (
                    <input type="number" value={editPriority} onChange={(e) => setEditPriority(Number(e.target.value))}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{hook.attributes.priority ?? 0}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Enabled</dt>
                  {editing ? (
                    <label className="flex items-center gap-2 mt-1">
                      <input type="checkbox" checked={editEnabled} onChange={(e) => setEditEnabled(e.target.checked)}
                        className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                      <span className="text-sm text-slate-200">{editEnabled ? 'Yes' : 'No'}</span>
                    </label>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{hook.attributes.enabled ? 'Yes' : 'No'}</dd>
                  )}
                </div>
              </dl>
              <div className="mt-4">
                <dt className="text-xs text-slate-500 mb-1">Script (<code>/bin/sh -c</code>)</dt>
                {editing ? (
                  <textarea value={editScript} onChange={(e) => setEditScript(e.target.value)} rows={5}
                    className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 font-mono focus:outline-none focus:ring-1 focus:ring-brand-500 resize-y" />
                ) : (
                  <pre className="mt-1 p-3 rounded bg-slate-900/60 border border-slate-700/50 text-xs text-slate-300 font-mono overflow-x-auto whitespace-pre-wrap">{hook.attributes.script || '(empty)'}</pre>
                )}
              </div>
            </div>

            <div className="bg-slate-800/50 rounded-lg border border-red-900/30 p-6">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-sm font-medium text-red-400">Delete Execution Hook</h3>
                  <p className="text-sm text-slate-400 mt-1">Permanently delete this hook and remove it from all workspaces.</p>
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

        {/* Workspaces Tab */}
        {activeTab === 'workspaces' && (
          <div>
            <div className="mb-4 p-3 bg-blue-900/20 text-blue-300 rounded-lg text-sm border border-blue-800/50">
              This hook runs only on the workspaces associated below. There is no global scope.
            </div>
            <div className="flex justify-end mb-4">
              <button
                onClick={() => setShowAddWs(!showAddWs)}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors"
              >
                {showAddWs ? 'Cancel' : 'Associate Workspace'}
              </button>
            </div>

            {showAddWs && (
              <form onSubmit={handleAddWorkspace} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 flex items-end gap-3">
                <div className="flex-1">
                  <label htmlFor="hook-ws-select" className="block text-sm font-medium text-slate-300 mb-1">Workspace</label>
                  <select id="hook-ws-select" value={selectedWsId} onChange={(e) => setSelectedWsId(e.target.value)}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                    <option value="">Select a workspace...</option>
                    {allWorkspaces
                      .filter((ws) => !assigned.some((a) => a.id === ws.id))
                      .map((ws) => (
                        <option key={ws.id} value={ws.id}>{ws.attributes?.name || ws.id}</option>
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
            ) : assigned.length === 0 ? (
              <EmptyState message="No workspaces associated with this hook." />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">Workspace</th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {assigned.map((ws) => (
                      <tr key={ws.id} className="hover:bg-slate-700/20 transition-colors">
                        <td className="px-4 py-3">
                          <Link href={`/workspaces/${ws.id}`} className="text-sm font-medium text-brand-400 hover:text-brand-300">
                            {wsName(ws.id)}
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
            )}
          </div>
        )}
      </main>
    </>
  )
}
