'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter, useParams, useSearchParams } from 'next/navigation'
import { useTranslations } from 'next-intl'
import Link from 'next/link'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { AssignmentRuleEditor, AssignmentRuleSummary, type AssignmentRule } from '@/components/assignment-rule-editor'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SensitiveValueInput } from '@/components/sensitive-value-input'
import { getAuthState, isAdmin } from '@/lib/auth'
import { useConfirm } from '@/lib/use-confirm'
import { apiFetch, fetchAllPages } from '@/lib/api'
import { usePollingInterval } from '@/lib/use-polling-interval'

interface VarsetAttrs {
  name: string
  description: string
  global: boolean
  priority: boolean
  'var-count': number
  'workspace-count': number
  'created-at': string
  /** Workspace selector (#1440). Null when membership is assigned by hand. */
  'assignment-rule'?: Record<string, unknown> | null
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
    structured: boolean
    sensitive: boolean
    description: string
  }
}

interface WorkspaceRef {
  id: string
  attributes: {
    name: string
    /** How the set came to apply (#1440). Only 'explicit' is unbindable here. */
    'assignment-source'?: 'explicit' | 'global' | 'rule'
  }
}

type Tab = 'settings' | 'variables' | 'workspaces'
const VALID_TABS: Set<string> = new Set(['settings', 'variables', 'workspaces'])

export default function VariableSetDetailPage() {
  const router = useRouter()
  const params = useParams()
  const varsetId = params.id as string
  const t = useTranslations('adminVariableSets')
  const { confirmDelete } = useConfirm()

  const [varset, setVarset] = useState<Varset | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  // Tab state lives in the URL, not component state, so a tab survives reload,
  // back, and a shared deep link. It was `useState`-only, which silently sent
  // every `?tab=` link to Settings (#1440).
  const searchParams = useSearchParams()
  const tabParam = searchParams.get('tab') || 'settings'
  const activeTab: Tab = VALID_TABS.has(tabParam) ? (tabParam as Tab) : 'settings'

  function setActiveTab(tab: Tab) {
    router.replace(`?tab=${tab}`, { scroll: false })
  }

  // Settings editing
  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const [editGlobal, setEditGlobal] = useState(false)
  const [editPriority, setEditPriority] = useState(false)
  const [editRule, setEditRule] = useState<AssignmentRule | null>(null)
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
      if (!res.ok) throw new Error(t('errors.load'))
      const data = await res.json()
      setVarset(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.load'))
    } finally {
      setLoading(false)
    }
  }, [varsetId, t])

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadVarset()
  }, [router, loadVarset])

  usePollingInterval(!loading, 60_000, loadVarset)

  useEffect(() => {
    if (!varset) return
    if (activeTab === 'variables') loadVariables()
    if (activeTab === 'workspaces') { loadWorkspaces(); loadAllWorkspaces() }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- initial mount load; the loader is a hoisted function declaration recreated each render, so depending on it would re-fetch on every render
  }, [activeTab, varset])

  async function loadVariables() {
    try {
      setVariables(await fetchAllPages<Variable>(`/api/v2/varsets/${varsetId}/relationships/vars`))
    } catch (err) {
      setError(err instanceof Error ? err.message : t('detail.errors.loadVars'))
    } finally {
      setVarsLoading(false)
    }
  }

  async function loadWorkspaces() {
    setWsLoading(true)
    try {
      // The association view (#1440), not the varset's own relationships block.
      // The latter carries only explicitly-assigned rows, so a rule-based set
      // rendered as "no workspaces" — the opposite of the truth. A global set
      // is skipped because the banner above already states the complete answer,
      // and listing an entire estate to repeat it is not worth the round trips.
      if (varset?.attributes.global) {
        setWorkspaces([])
        return
      }
      setWorkspaces(
        await fetchAllPages<WorkspaceRef>(
          `/api/terrapod/v1/varsets/${varsetId}/relationships/workspaces`,
        ),
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : t('detail.errors.loadWorkspaces'))
    } finally {
      setWsLoading(false)
    }
  }

  async function loadAllWorkspaces() {
    try {
      // Page through the whole list so every workspace is assignable.
      setAllWorkspaces(await fetchAllPages<WorkspaceRef>('/api/v2/organizations/default/workspaces'))
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
    setEditRule((varset.attributes['assignment-rule'] as AssignmentRule) ?? null)
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
              // Always sent, including as null, so clearing the rule actually
              // clears it. Omitting this made the whole editor inert: it
              // rendered, previewed a live match count, reported success, and
              // saved nothing.
              'assignment-rule': editGlobal ? null : editRule,
            },
          },
        }),
      })
      if (!res.ok) {
        // The rule validation messages say exactly what is wrong with a filter;
        // replacing them with a generic "update failed" would waste them.
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || t('detail.errors.update'))
      }
      const data = await res.json()
      setVarset(data.data)
      setEditing(false)
      setSuccess(t('detail.success.updated'))
    } catch (err) {
      setError(err instanceof Error ? err.message : t('detail.errors.update'))
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete() {
    setDeleting(true)
    try {
      const res = await apiFetch(`/api/v2/varsets/${varsetId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(t('detail.errors.delete'))
      router.push('/admin/variable-sets')
    } catch (err) {
      setError(err instanceof Error ? err.message : t('detail.errors.delete'))
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
              structured: varHcl,
            },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('detail.errors.addVarStatus', { status: res.status }))
      }
      setVarKey('')
      setVarValue('')
      setVarCategory('terraform')
      setVarSensitive(false)
      setVarHcl(false)
      setShowAddVar(false)
      await loadVariables()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('detail.errors.addVar'))
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
    setEditVarHcl(v.attributes.structured)
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
        structured: editVarHcl,
      }
      if (editVarValue !== '') attrs.value = editVarValue
      const res = await apiFetch(`/api/v2/varsets/${varsetId}/relationships/vars/${editingVarId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'vars', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('detail.errors.updateVar'))
      }
      setEditingVarId(null)
      await loadVariables()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('detail.errors.updateVar'))
    } finally {
      setSavingVar(false)
    }
  }

  async function handleDeleteVariable(varId: string) {
    if (!confirmDelete(t('detail.confirmDeleteVar'))) return
    setError('')
    try {
      const res = await apiFetch(`/api/v2/varsets/${varsetId}/relationships/vars/${varId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(t('detail.errors.deleteVar'))
      await loadVariables()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('detail.errors.deleteVar'))
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
        throw new Error(data.detail || t('detail.errors.addWorkspace'))
      }
      setSelectedWsId('')
      setShowAddWs(false)
      setSuccess(t('detail.success.workspaceAdded'))
      await loadWorkspaces()
      await loadVarset()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('detail.errors.addWorkspace'))
    } finally {
      setAddingWs(false)
    }
  }

  async function handleRemoveWorkspace(wsId: string) {
    if (!confirmDelete(t('detail.confirmRemoveWorkspace'))) return
    setError('')
    try {
      const res = await apiFetch(`/api/v2/varsets/${varsetId}/relationships/workspaces`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: [{ id: wsId, type: 'workspaces' }],
        }),
      })
      if (!res.ok) throw new Error(t('detail.errors.removeWorkspace'))
      setSuccess(t('detail.success.workspaceRemoved'))
      await loadWorkspaces()
      await loadVarset()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('detail.errors.removeWorkspace'))
    }
  }

  const tabs: { key: Tab; label: string }[] = [
    { key: 'settings', label: t('detail.tabs.settings') },
    { key: 'variables', label: t('detail.tabs.variables') },
    { key: 'workspaces', label: t('detail.tabs.workspaces') },
  ]

  if (loading) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>
  if (!varset) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><ErrorBanner message={t('detail.notFound')} /></main></>

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <div className="mb-4">
          <Link href="/admin/variable-sets" className="text-sm text-slate-400 hover:text-slate-200">
            &larr; {t('detail.back')}
          </Link>
        </div>

        <PageHeader
          title={varset.attributes.name}
          description={varset.attributes.description || t('detail.headerFallback')}
        />

        {error && <ErrorBanner message={error} />}
        {success && (
          <div className="mb-4 p-3 bg-green-900/30 text-green-400 rounded-lg text-sm border border-green-800/50">{success}</div>
        )}

        {/* Tabs */}
        <div className="border-b border-slate-700/50 mb-6 overflow-x-auto">
          <div className="flex gap-1 -mb-px whitespace-nowrap">
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
                <h3 className="text-sm font-medium text-slate-300">{t('detail.tabs.settings')}</h3>
                {!editing ? (
                  <button onClick={startEditing} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors">{t('detail.edit')}</button>
                ) : (
                  <div className="flex gap-2">
                    <button onClick={() => setEditing(false)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors">{t('detail.cancel')}</button>
                    <button onClick={handleSave} disabled={saving} className="px-2.5 py-1 rounded-md text-xs font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors disabled:opacity-50">
                      {saving ? t('detail.saving') : t('detail.save')}
                    </button>
                  </div>
                )}
              </div>
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <dt className="text-xs text-slate-500">{t('form.name')}</dt>
                  {editing ? (
                    <input type="text" value={editName} onChange={(e) => setEditName(e.target.value)}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{varset.attributes.name}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('form.descriptionLabel')}</dt>
                  {editing ? (
                    <input type="text" value={editDesc} onChange={(e) => setEditDesc(e.target.value)}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{varset.attributes.description || t('none')}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('detail.global')}</dt>
                  {editing ? (
                    <label className="flex items-center gap-2 mt-1">
                      <input type="checkbox" checked={editGlobal} onChange={(e) => setEditGlobal(e.target.checked)}
                        className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                      <span className="text-sm text-slate-200">{editGlobal ? t('detail.yes') : t('detail.no')}</span>
                    </label>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{varset.attributes.global ? t('detail.yes') : t('detail.no')}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('detail.priority')}</dt>
                  {editing ? (
                    <label className="flex items-center gap-2 mt-1">
                      <input type="checkbox" checked={editPriority} onChange={(e) => setEditPriority(e.target.checked)}
                        className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                      <span className="text-sm text-slate-200">{editPriority ? t('detail.yes') : t('detail.no')}</span>
                    </label>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{varset.attributes.priority ? t('detail.yes') : t('detail.no')}</dd>
                  )}
                </div>
              </dl>

              {/* The rule (#1440): editable in edit mode, read-only otherwise.
                  A global set applies everywhere already, so a rule alongside it
                  is a contradiction the server rejects — the editor is disabled
                  rather than offering a combination that cannot be saved. */}
              {editing ? (
                <AssignmentRuleEditor
                  rule={editRule}
                  onChange={setEditRule}
                  disabled={editGlobal}
                />
              ) : (
                <div className="mt-4 pt-4 border-t border-slate-700/50">
                  <dt className="text-xs text-slate-500">{t('detail.assignmentRule')}</dt>
                  {varset.attributes['assignment-rule'] ? (
                    <>
                      <p className="mt-1 text-xs text-slate-500">{t('detail.assignmentRuleDescription')}</p>
                      <AssignmentRuleSummary rule={varset.attributes['assignment-rule'] as AssignmentRule} />
                    </>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-400">{t('detail.assignmentRuleNone')}</dd>
                  )}
                </div>
              )}
            </div>

            <div className="bg-slate-800/50 rounded-lg border border-red-900/30 p-6">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-sm font-medium text-red-400">{t('detail.deleteTitle')}</h3>
                  <p className="text-sm text-slate-400 mt-1">{t('detail.deleteDescription')}</p>
                </div>
                {!showDeleteConfirm ? (
                  <button onClick={() => setShowDeleteConfirm(true)}
                    className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600/20 hover:bg-red-600/40 text-red-400 transition-colors">
                    {t('detail.delete')}
                  </button>
                ) : (
                  <div className="flex gap-2">
                    <button onClick={() => setShowDeleteConfirm(false)} className="px-3 py-1.5 rounded-lg text-sm font-medium text-slate-400 hover:text-slate-200">{t('detail.cancel')}</button>
                    <button onClick={handleDelete} disabled={deleting}
                      className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-500 text-white transition-colors">
                      {deleting ? t('detail.deleting') : t('detail.confirmDelete')}
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
                {showAddVar ? t('detail.cancel') : t('detail.addVariable')}
              </button>
            </div>

            {showAddVar && (
              <form onSubmit={handleAddVariable} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="var-key" className="block text-sm font-medium text-slate-300 mb-1">{t('detail.varKey')}</label>
                    <input id="var-key" type="text" value={varKey} onChange={(e) => setVarKey(e.target.value)} required placeholder="AWS_REGION"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="var-val" className="block text-sm font-medium text-slate-300 mb-1">{t('detail.varValue')}</label>
                    <SensitiveValueInput id="var-val" value={varValue} onChange={setVarValue} sensitive={varSensitive} placeholder="us-east-1"
                      rows={2} className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent resize-y" />
                  </div>
                  <div>
                    <label htmlFor="var-cat" className="block text-sm font-medium text-slate-300 mb-1">{t('detail.varCategory')}</label>
                    <select id="var-cat" value={varCategory} onChange={(e) => setVarCategory(e.target.value)}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      <option value="terraform">{t('detail.categoryTerraform')}</option>
                      <option value="env">{t('detail.categoryEnv')}</option>
                      <option value="git_http_auth">Git HTTPS credential</option>
                      <option value="git_ssh_auth">Git SSH credential</option>
                    </select>
                  </div>
                  <div className="flex items-end gap-4">
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input type="checkbox" checked={varSensitive} onChange={(e) => setVarSensitive(e.target.checked)}
                        className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                      <span className="text-sm text-slate-300">{t('detail.sensitive')}</span>
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
                  {addingVar ? t('detail.adding') : t('detail.addVariable')}
                </button>
              </form>
            )}

            {varsLoading ? (
              <LoadingSpinner />
            ) : variables.length === 0 ? (
              <EmptyState message={t('detail.emptyVars')} />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="px-4 py-3 text-start text-xs font-medium text-slate-400 uppercase tracking-wider">{t('detail.varKey')}</th>
                      <th className="px-4 py-3 text-start text-xs font-medium text-slate-400 uppercase tracking-wider">{t('detail.varValue')}</th>
                      <th className="px-4 py-3 text-start text-xs font-medium text-slate-400 uppercase tracking-wider hidden sm:table-cell">{t('detail.varCategory')}</th>
                      <th className="px-4 py-3 text-end text-xs font-medium text-slate-400 uppercase tracking-wider">{t('detail.actions')}</th>
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
                            <SensitiveValueInput value={editVarValue} onChange={setEditVarValue}
                              sensitive={editVarSensitive}
                              placeholder={editVarSensitive ? t('detail.enterNewValue') : ''}
                              rows={2}
                              className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 font-mono focus:outline-none focus:ring-1 focus:ring-brand-500 resize-y" />
                          </td>
                          <td className="px-4 py-3 hidden sm:table-cell">
                            <div className="flex items-center gap-3">
                              <select value={editVarCategory} onChange={(e) => setEditVarCategory(e.target.value)}
                                className="px-2 py-1 text-xs border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500">
                                <option value="terraform">terraform</option>
                                <option value="env">env</option>
                                <option value="git_http_auth">Git HTTPS credential</option>
                                <option value="git_ssh_auth">Git SSH credential</option>
                              </select>
                              <label className="flex items-center gap-1 cursor-pointer">
                                <input type="checkbox" checked={editVarSensitive} onChange={(e) => setEditVarSensitive(e.target.checked)}
                                  className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                                <span className="text-xs text-slate-400">{t('detail.sensAbbr')}</span>
                              </label>
                              <label className="flex items-center gap-1 cursor-pointer">
                                <input type="checkbox" checked={editVarHcl} onChange={(e) => setEditVarHcl(e.target.checked)}
                                  className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                                <span className="text-xs text-slate-400">HCL</span>
                              </label>
                            </div>
                          </td>
                          <td className="px-4 py-3 text-end">
                            <div className="flex justify-end gap-2">
                              <button onClick={() => setEditingVarId(null)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors">{t('detail.cancel')}</button>
                              <button onClick={handleSaveVar} disabled={savingVar} className="px-2.5 py-1 rounded-md text-xs font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors disabled:opacity-50">
                                {savingVar ? t('detail.saving') : t('detail.save')}
                              </button>
                            </div>
                          </td>
                        </tr>
                      ) : (
                        <tr key={v.id} className="hover:bg-slate-700/20 transition-colors">
                          <td className="px-4 py-3 text-sm text-slate-200 font-mono">{v.attributes.key}</td>
                          <td className="px-4 py-3 text-sm text-slate-400 font-mono">
                            {v.attributes.sensitive ? '***' : (v.attributes.value || <span className="text-slate-600 italic">{t('detail.empty')}</span>)}
                          </td>
                          <td className="px-4 py-3 text-xs text-slate-400 hidden sm:table-cell">
                            <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                              v.attributes.category === 'terraform' ? 'bg-purple-900/50 text-purple-300' : 'bg-cyan-900/50 text-cyan-300'
                            }`}>
                              {v.attributes.category}
                            </span>
                          </td>
                          <td className="px-4 py-3 text-end">
                            <div className="flex justify-end gap-2">
                              <button onClick={() => startEditingVar(v)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors">{t('detail.edit')}</button>
                              <button onClick={() => handleDeleteVariable(v.id)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors">{t('detail.delete')}</button>
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
                  {showAddWs ? t('detail.cancel') : t('detail.addWorkspace')}
                </button>
              </div>
            )}

            {varset.attributes.global && (
              <div className="mb-4 p-3 bg-blue-900/20 text-blue-300 rounded-lg text-sm border border-blue-800/50">
                {t('detail.globalBanner')}
              </div>
            )}

            {showAddWs && (
              <form onSubmit={handleAddWorkspace} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 flex items-end gap-3">
                <div className="flex-1">
                  <label htmlFor="ws-select" className="block text-sm font-medium text-slate-300 mb-1">{t('detail.workspace')}</label>
                  <select id="ws-select" value={selectedWsId} onChange={(e) => setSelectedWsId(e.target.value)}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                    <option value="">{t('detail.selectWorkspace')}</option>
                    {allWorkspaces
                      .filter((ws) => !workspaces.some((assigned) => assigned.id === ws.id))
                      .map((ws) => (
                        <option key={ws.id} value={ws.id}>{ws.attributes.name}</option>
                      ))}
                  </select>
                </div>
                <button type="submit" disabled={addingWs || !selectedWsId}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {addingWs ? t('detail.adding') : t('detail.add')}
                </button>
              </form>
            )}

            {wsLoading ? (
              <LoadingSpinner />
            ) : workspaces.length === 0 && !varset.attributes.global ? (
              <EmptyState message={t('detail.emptyWorkspaces')} />
            ) : workspaces.length > 0 ? (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="px-4 py-3 text-start text-xs font-medium text-slate-400 uppercase tracking-wider">{t('detail.workspace')}</th>
                      <th className="px-4 py-3 text-start text-xs font-medium text-slate-400 uppercase tracking-wider">{t('detail.assignedBy')}</th>
                      <th className="px-4 py-3 text-end text-xs font-medium text-slate-400 uppercase tracking-wider">{t('detail.actions')}</th>
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
                        <td className="px-4 py-3">
                          <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                            (ws.attributes?.['assignment-source'] ?? 'explicit') === 'rule'
                              ? 'bg-violet-900/40 text-violet-300'
                              : 'bg-slate-700 text-slate-200'
                          }`}>
                            {t(`detail.source.${ws.attributes?.['assignment-source'] ?? 'explicit'}`)}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-end">
                          {/* Only an explicit binding can be removed here. A
                              rule-matched workspace has no row to delete — the
                              rule is edited on the Settings tab — so offering a
                              Remove that silently did nothing would mislead. */}
                          {(ws.attributes?.['assignment-source'] ?? 'explicit') === 'explicit' ? (
                            <button onClick={() => handleRemoveWorkspace(ws.id)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors">{t('detail.remove')}</button>
                          ) : (
                            <span className="text-xs text-slate-500">{t('detail.managedByRule')}</span>
                          )}
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
