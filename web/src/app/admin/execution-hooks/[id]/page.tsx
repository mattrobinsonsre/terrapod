'use client'

import { useEffect, useState, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import { useRouter, useParams } from 'next/navigation'
import Link from 'next/link'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { getAuthState, isAdmin } from '@/lib/auth'
import { useConfirm } from '@/lib/use-confirm'
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
  const t = useTranslations('execHookDetail')
  const router = useRouter()
  const params = useParams()
  const hookId = params.id as string
  const { confirmDelete } = useConfirm()

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
      if (!res.ok) throw new Error(t('errors.load'))
      const data = await res.json()
      setHook(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.load'))
    } finally {
      setLoading(false)
    }
  }, [hookId, t])

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
        throw new Error(data.detail || t('errors.update'))
      }
      const data = await res.json()
      setHook(data.data)
      setEditing(false)
      setSuccess(t('success.updated'))
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.update'))
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete() {
    setDeleting(true)
    try {
      const res = await apiFetch(`/api/terrapod/v1/execution-hooks/${hookId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(t('errors.delete'))
      router.push('/admin/execution-hooks')
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.delete'))
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
        throw new Error(data.detail || t('errors.addWorkspace'))
      }
      setSelectedWsId('')
      setShowAddWs(false)
      setSuccess(t('success.workspaceAssociated'))
      await loadHook()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.addWorkspace'))
    } finally {
      setAddingWs(false)
    }
  }

  async function handleRemoveWorkspace(wsId: string) {
    if (!confirmDelete(t('confirm.removeWorkspace'))) return
    setError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/execution-hooks/${hookId}/relationships/workspaces`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: [{ id: wsId, type: 'workspaces' }] }),
      })
      if (!res.ok) throw new Error(t('errors.removeWorkspace'))
      setSuccess(t('success.workspaceRemoved'))
      await loadHook()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.removeWorkspace'))
    }
  }

  const tabs: { key: Tab; label: string }[] = [
    { key: 'settings', label: t('tabs.settings') },
    { key: 'workspaces', label: t('tabs.workspaces') },
  ]

  if (loading) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>
  if (!hook) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><ErrorBanner message={t('notFound')} /></main></>

  const assigned = hook.relationships?.workspaces?.data || []
  const wsName = (id: string) => allWorkspaces.find((w) => w.id === id)?.attributes?.name || id

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <div className="mb-4">
          <Link href="/admin/execution-hooks" className="text-sm text-slate-400 hover:text-slate-200">
            &larr; {t('backLink')}
          </Link>
        </div>

        <PageHeader
          title={hook.attributes.name}
          description={hook.attributes.description || t('defaultDescription')}
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
                <h3 className="text-sm font-medium text-slate-300">{t('settings.heading')}</h3>
                {!editing ? (
                  <button onClick={startEditing} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors">{t('actions.edit')}</button>
                ) : (
                  <div className="flex gap-2">
                    <button onClick={() => setEditing(false)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors">{t('actions.cancel')}</button>
                    <button onClick={handleSave} disabled={saving} className="px-2.5 py-1 rounded-md text-xs font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors disabled:opacity-50">
                      {saving ? t('actions.saving') : t('actions.save')}
                    </button>
                  </div>
                )}
              </div>
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.name')}</dt>
                  {editing ? (
                    <input type="text" value={editName} onChange={(e) => setEditName(e.target.value)}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{hook.attributes.name}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.description')}</dt>
                  {editing ? (
                    <input type="text" value={editDesc} onChange={(e) => setEditDesc(e.target.value)}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{hook.attributes.description || '-'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.hookPoint')}</dt>
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
                  <dt className="text-xs text-slate-500">{t('fields.priority')}</dt>
                  {editing ? (
                    <input type="number" value={editPriority} onChange={(e) => setEditPriority(Number(e.target.value))}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{hook.attributes.priority ?? 0}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.enabled')}</dt>
                  {editing ? (
                    <label className="flex items-center gap-2 mt-1">
                      <input type="checkbox" checked={editEnabled} onChange={(e) => setEditEnabled(e.target.checked)}
                        className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                      <span className="text-sm text-slate-200">{editEnabled ? t('common.yes') : t('common.no')}</span>
                    </label>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{hook.attributes.enabled ? t('common.yes') : t('common.no')}</dd>
                  )}
                </div>
              </dl>
              <div className="mt-4">
                <dt className="text-xs text-slate-500 mb-1">{t.rich('fields.script', { code: (chunks) => <code>{chunks}</code> })}</dt>
                {editing ? (
                  <textarea value={editScript} onChange={(e) => setEditScript(e.target.value)} rows={5}
                    className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 font-mono focus:outline-none focus:ring-1 focus:ring-brand-500 resize-y" />
                ) : (
                  <pre className="mt-1 p-3 rounded bg-slate-900/60 border border-slate-700/50 text-xs text-slate-300 font-mono overflow-x-auto whitespace-pre-wrap">{hook.attributes.script || t('scriptEmpty')}</pre>
                )}
              </div>
            </div>

            <div className="bg-slate-800/50 rounded-lg border border-red-900/30 p-6">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-sm font-medium text-red-400">{t('delete.heading')}</h3>
                  <p className="text-sm text-slate-400 mt-1">{t('delete.description')}</p>
                </div>
                {!showDeleteConfirm ? (
                  <button onClick={() => setShowDeleteConfirm(true)}
                    className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600/20 hover:bg-red-600/40 text-red-400 transition-colors">
                    {t('actions.delete')}
                  </button>
                ) : (
                  <div className="flex gap-2">
                    <button onClick={() => setShowDeleteConfirm(false)} className="px-3 py-1.5 rounded-lg text-sm font-medium text-slate-400 hover:text-slate-200">{t('actions.cancel')}</button>
                    <button onClick={handleDelete} disabled={deleting}
                      className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-500 text-white transition-colors">
                      {deleting ? t('actions.deleting') : t('actions.confirmDelete')}
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
              {t('workspaces.scopeNote')}
            </div>
            <div className="flex justify-end mb-4">
              <button
                onClick={() => setShowAddWs(!showAddWs)}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors"
              >
                {showAddWs ? t('actions.cancel') : t('workspaces.associate')}
              </button>
            </div>

            {showAddWs && (
              <form onSubmit={handleAddWorkspace} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 flex items-end gap-3">
                <div className="flex-1">
                  <label htmlFor="hook-ws-select" className="block text-sm font-medium text-slate-300 mb-1">{t('workspaces.workspaceLabel')}</label>
                  <select id="hook-ws-select" value={selectedWsId} onChange={(e) => setSelectedWsId(e.target.value)}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                    <option value="">{t('workspaces.selectPlaceholder')}</option>
                    {allWorkspaces
                      .filter((ws) => !assigned.some((a) => a.id === ws.id))
                      .map((ws) => (
                        <option key={ws.id} value={ws.id}>{ws.attributes?.name || ws.id}</option>
                      ))}
                  </select>
                </div>
                <button type="submit" disabled={addingWs || !selectedWsId}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {addingWs ? t('actions.adding') : t('actions.add')}
                </button>
              </form>
            )}

            {wsLoading ? (
              <LoadingSpinner />
            ) : assigned.length === 0 ? (
              <EmptyState message={t('workspaces.empty')} />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">{t('workspaces.colWorkspace')}</th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">{t('workspaces.colActions')}</th>
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
                          <button onClick={() => handleRemoveWorkspace(ws.id)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors">{t('actions.remove')}</button>
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
