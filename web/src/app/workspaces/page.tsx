'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { getAuthState } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

interface Workspace {
  id: string
  attributes: {
    name: string
    'execution-mode': string
    'auto-apply': boolean
    'terraform-version': string
    locked: boolean
    'resource-cpu': string
    'resource-memory': string
    'created-at': string
  }
}

export default function WorkspacesPage() {
  const router = useRouter()
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Create form
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [newExecMode, setNewExecMode] = useState('local')
  const [newAutoApply, setNewAutoApply] = useState(false)
  const [newCpu, setNewCpu] = useState('1')
  const [newMemory, setNewMemory] = useState('2Gi')
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadWorkspaces()
  }, [router])

  async function loadWorkspaces() {
    setLoading(true)
    try {
      const res = await apiFetch('/api/v2/organizations/default/workspaces')
      if (!res.ok) throw new Error('Failed to load workspaces')
      const data = await res.json()
      setWorkspaces(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load workspaces')
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    try {
      const res = await apiFetch('/api/v2/organizations/default/workspaces', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'workspaces',
            attributes: {
              name: newName,
              'execution-mode': newExecMode,
              'auto-apply': newAutoApply,
              'resource-cpu': newCpu,
              'resource-memory': newMemory,
            },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to create workspace (${res.status})`)
      }
      setNewName('')
      setNewExecMode('local')
      setNewAutoApply(false)
      setNewCpu('1')
      setNewMemory('2Gi')
      setShowCreate(false)
      await loadWorkspaces()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create workspace')
    } finally {
      setCreating(false)
    }
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="Workspaces"
          description="Manage Terraform workspaces, state, and runs"
          actions={
            <button
              onClick={() => setShowCreate(!showCreate)}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showCreate ? 'Cancel' : 'New Workspace'}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}

        {showCreate && (
          <form onSubmit={handleCreate} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              <div>
                <label htmlFor="ws-name" className="block text-sm font-medium text-slate-300 mb-1">Name</label>
                <input
                  id="ws-name"
                  type="text"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  required
                  placeholder="my-workspace"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
              <div>
                <label htmlFor="ws-exec" className="block text-sm font-medium text-slate-300 mb-1">Execution Mode</label>
                <select
                  id="ws-exec"
                  value={newExecMode}
                  onChange={(e) => setNewExecMode(e.target.value)}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                >
                  <option value="local">Local</option>
                  <option value="remote">Remote</option>
                </select>
              </div>
              <div>
                <label htmlFor="ws-cpu" className="block text-sm font-medium text-slate-300 mb-1">CPU Request</label>
                <input
                  id="ws-cpu"
                  type="text"
                  value={newCpu}
                  onChange={(e) => setNewCpu(e.target.value)}
                  placeholder="1"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
              <div>
                <label htmlFor="ws-mem" className="block text-sm font-medium text-slate-300 mb-1">Memory Request</label>
                <input
                  id="ws-mem"
                  type="text"
                  value={newMemory}
                  onChange={(e) => setNewMemory(e.target.value)}
                  placeholder="2Gi"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
              <div className="flex items-end">
                <label className="flex items-center gap-2 px-3 py-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={newAutoApply}
                    onChange={(e) => setNewAutoApply(e.target.checked)}
                    className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
                  />
                  <span className="text-sm text-slate-300">Auto Apply</span>
                </label>
              </div>
            </div>
            <button
              type="submit"
              disabled={creating}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
            >
              {creating ? 'Creating...' : 'Create Workspace'}
            </button>
          </form>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : workspaces.length === 0 ? (
          <EmptyState message="No workspaces yet. Create one to get started." />
        ) : (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">Name</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden sm:table-cell">Mode</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden md:table-cell">Resources</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden lg:table-cell">Status</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden lg:table-cell">Created</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700/30">
                {workspaces.map((ws) => (
                  <tr key={ws.id} className="hover:bg-slate-700/20 transition-colors">
                    <td className="px-4 py-3">
                      <Link
                        href={`/workspaces/${ws.id}`}
                        className="text-sm font-medium text-brand-400 hover:text-brand-300"
                      >
                        {ws.attributes.name}
                      </Link>
                    </td>
                    <td className="px-4 py-3 hidden sm:table-cell">
                      <span className="text-xs text-slate-400">{ws.attributes['execution-mode']}</span>
                    </td>
                    <td className="px-4 py-3 hidden md:table-cell">
                      <span className="text-xs text-slate-400">
                        {ws.attributes['resource-cpu']} CPU / {ws.attributes['resource-memory']}
                      </span>
                    </td>
                    <td className="px-4 py-3 hidden lg:table-cell">
                      <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                        ws.attributes.locked
                          ? 'bg-amber-900/50 text-amber-300'
                          : 'bg-green-900/50 text-green-300'
                      }`}>
                        {ws.attributes.locked ? 'Locked' : 'Unlocked'}
                      </span>
                    </td>
                    <td className="px-4 py-3 hidden lg:table-cell">
                      <span className="text-xs text-slate-500">
                        {ws.attributes['created-at'] ? new Date(ws.attributes['created-at']).toLocaleDateString() : ''}
                      </span>
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
