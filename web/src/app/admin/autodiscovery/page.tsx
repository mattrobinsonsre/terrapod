'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { LabelsEditor } from '@/components/labels-editor'
import {
  StringListEditor,
  RunTaskTemplatesEditor,
  NotificationTemplatesEditor,
  type RunTaskSpec,
  type NotificationSpec,
} from '@/components/template-editors'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { useSortable } from '@/lib/use-sortable'
import { useFormat } from '@/lib/format'

interface AutodiscoveryRule {
  id: string
  attributes: {
    name: string
    'name-template': string
    'vcs-connection-id': string
    'repo-url': string
    branch: string
    pattern: string
    'ignore-patterns': string[]
    enabled: boolean
    'execution-mode': string
    'execution-backend': string
    'agent-pool-id': string | null
    'terraform-version': string
    'resource-cpu': string
    'resource-memory': string
    'auto-apply': boolean
    'on-directory-delete': 'flag' | 'destroy'
    labels: Record<string, string>
    'owner-email': string
    'var-files': string[]
    'execution-hook-templates'?: string[]
    'run-task-templates': RunTaskSpec[]
    'notification-templates': NotificationSpec[]
    'created-at': string
    'updated-at': string
  }
}

interface VCSConnection {
  id: string
  attributes: { name: string; provider: string }
}

interface AgentPool {
  id: string
  attributes: { name: string }
}

interface ExecutionHookRef {
  id: string
  attributes: { name: string; 'hook-point': string; enabled: boolean }
}

type SortKey = 'name' | 'repo' | 'pattern' | 'enabled' | 'created'

export default function AutodiscoveryPage() {
  const router = useRouter()
  const t = useTranslations('adminAutodiscovery')
  const fmt = useFormat()
  const [rules, setRules] = useState<AutodiscoveryRule[]>([])
  const [connections, setConnections] = useState<VCSConnection[]>([])
  const [pools, setPools] = useState<AgentPool[]>([])
  const [hooks, setHooks] = useState<ExecutionHookRef[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  // Create / edit form
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [vcsConnectionId, setVcsConnectionId] = useState('')
  const [repoUrl, setRepoUrl] = useState('')
  const [branch, setBranch] = useState('')
  const [pattern, setPattern] = useState('accounts/*/**/*.tf')
  const [ignorePatternsText, setIgnorePatternsText] = useState('modules/**')
  const [nameTemplate, setNameTemplate] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [executionMode, setExecutionMode] = useState<'agent'>('agent')
  const [agentPoolId, setAgentPoolId] = useState('')
  const [executionBackend, setExecutionBackend] = useState<'tofu' | 'terraform'>('tofu')
  const [terraformVersion, setTerraformVersion] = useState('1.11')
  const [resourceCpu, setResourceCpu] = useState('1')
  const [resourceMemory, setResourceMemory] = useState('2Gi')
  const [autoApply, setAutoApply] = useState(false)
  const [onDirectoryDelete, setOnDirectoryDelete] = useState<'flag' | 'destroy'>('flag')
  const [labels, setLabels] = useState<Record<string, string>>({})
  const [ownerEmail, setOwnerEmail] = useState('')
  const [varFiles, setVarFiles] = useState<string[]>([])
  const [executionHookTemplates, setExecutionHookTemplates] = useState<string[]>([])
  const [runTaskTemplates, setRunTaskTemplates] = useState<RunTaskSpec[]>([])
  const [notificationTemplates, setNotificationTemplates] = useState<NotificationSpec[]>([])
  const [submitting, setSubmitting] = useState(false)

  const [deleteId, setDeleteId] = useState<string | null>(null)

  // Preview / on-demand scan modal (#311). Three lifecycle states:
  //   { id: null }                          → modal closed
  //   { id, loading: true }                  → fetching /preview
  //   { id, entries: [...] }                 → preview loaded; awaiting confirm
  //   { id, entries: [...], scanning: true } → user clicked Provision; running /scan
  const [previewModal, setPreviewModal] = useState<{
    id: string
    ruleName: string
    loading?: boolean
    error?: string
    ref?: string
    filesWalked?: number
    entries?: Array<{
      workspace_name: string
      working_directory: string
      collision: boolean
      existing_autodiscovered: boolean
    }>
    scanning?: boolean
  } | null>(null)

  const accessor = useCallback((r: AutodiscoveryRule, key: SortKey) => {
    switch (key) {
      case 'name': return r.attributes.name
      case 'repo': return r.attributes['repo-url']
      case 'pattern': return r.attributes.pattern
      case 'enabled': return r.attributes.enabled ? '1' : '0'
      case 'created': return r.attributes['created-at']
    }
  }, [])

  const { sortedItems, sortState, toggleSort } = useSortable<AutodiscoveryRule, SortKey>(
    rules, 'name', 'asc', accessor,
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadAll()
  }, [router])

  async function loadAll() {
    try {
      const [rulesRes, connsRes, poolsRes, hooksRes] = await Promise.all([
        apiFetch('/api/terrapod/v1/autodiscovery-rules'),
        apiFetch('/api/terrapod/v1/vcs-connections'),
        apiFetch('/api/terrapod/v1/agent-pools'),
        apiFetch('/api/terrapod/v1/execution-hooks'),
      ])
      if (!rulesRes.ok) throw new Error(t('errors.loadRules'))
      const rulesData = await rulesRes.json()
      setRules(rulesData.data || [])
      if (connsRes.ok) {
        const cd = await connsRes.json()
        setConnections(cd.data || [])
      }
      if (poolsRes.ok) {
        const pd = await poolsRes.json()
        setPools(pd.data || [])
      }
      if (hooksRes.ok) {
        const hd = await hooksRes.json()
        setHooks(hd.data || [])
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.load'))
    } finally {
      setLoading(false)
    }
  }

  function resetForm() {
    setEditingId(null)
    setName('')
    setVcsConnectionId('')
    setRepoUrl('')
    setBranch('')
    setPattern('accounts/*/**/*.tf')
    setIgnorePatternsText('modules/**')
    setNameTemplate('')
    setEnabled(true)
    setExecutionMode('agent')
    setAgentPoolId('')
    setExecutionBackend('tofu')
    setTerraformVersion('1.11')
    setResourceCpu('1')
    setResourceMemory('2Gi')
    setAutoApply(false)
    setOnDirectoryDelete('flag')
    setLabels({})
    setOwnerEmail('')
    setVarFiles([])
    setExecutionHookTemplates([])
    setRunTaskTemplates([])
    setNotificationTemplates([])
  }

  function openCreateForm() {
    resetForm()
    setShowForm(true)
  }

  function openEditForm(r: AutodiscoveryRule) {
    setEditingId(r.id)
    const a = r.attributes
    setName(a.name)
    setVcsConnectionId(a['vcs-connection-id'] ? `vcs-${a['vcs-connection-id']}` : '')
    setRepoUrl(a['repo-url'])
    setBranch(a.branch)
    setPattern(a.pattern)
    setIgnorePatternsText((a['ignore-patterns'] || []).join('\n'))
    setNameTemplate(a['name-template'])
    setEnabled(a.enabled)
    setExecutionMode('agent')
    setAgentPoolId(a['agent-pool-id'] ? `apool-${a['agent-pool-id']}` : '')
    setExecutionBackend((a['execution-backend'] as 'tofu' | 'terraform') || 'tofu')
    setTerraformVersion(a['terraform-version'])
    setResourceCpu(a['resource-cpu'])
    setResourceMemory(a['resource-memory'])
    setAutoApply(a['auto-apply'])
    setOnDirectoryDelete(a['on-directory-delete'] === 'destroy' ? 'destroy' : 'flag')
    setLabels(a.labels || {})
    setOwnerEmail(a['owner-email'] || '')
    setVarFiles(a['var-files'] || [])
    setExecutionHookTemplates(a['execution-hook-templates'] || [])
    setRunTaskTemplates(a['run-task-templates'] || [])
    setNotificationTemplates(a['notification-templates'] || [])
    setShowForm(true)
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    setError('')
    setSuccess('')

    const ignorePatterns = ignorePatternsText
      .split('\n')
      .map(s => s.trim())
      .filter(Boolean)

    const attrs: Record<string, unknown> = {
      name,
      'vcs-connection-id': vcsConnectionId,
      'repo-url': repoUrl,
      branch,
      pattern,
      'ignore-patterns': ignorePatterns,
      'name-template': nameTemplate,
      enabled,
      'execution-mode': executionMode,
      'execution-backend': executionBackend,
      'agent-pool-id': agentPoolId || null,
      'terraform-version': terraformVersion,
      'resource-cpu': resourceCpu,
      'resource-memory': resourceMemory,
      'auto-apply': autoApply,
      'on-directory-delete': onDirectoryDelete,
      labels,
      'owner-email': ownerEmail,
      'var-files': varFiles.map(s => s.trim()).filter(Boolean),
      'execution-hook-templates': executionHookTemplates.map(s => s.trim()).filter(Boolean),
      'run-task-templates': runTaskTemplates,
      'notification-templates': notificationTemplates,
    }

    try {
      const path = editingId
        ? `/api/terrapod/v1/autodiscovery-rules/${editingId}`
        : '/api/terrapod/v1/autodiscovery-rules'
      const method = editingId ? 'PATCH' : 'POST'
      const res = await apiFetch(path, {
        method,
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'autodiscovery-rules', attributes: attrs } }),
      })
      if (!res.ok) {
        const txt = await res.text()
        throw new Error(
          editingId
            ? t('errors.updateFailed', { status: res.status, detail: txt })
            : t('errors.createFailed', { status: res.status, detail: txt }),
        )
      }
      setSuccess(editingId ? t('success.updated', { name }) : t('success.created', { name }))
      setShowForm(false)
      resetForm()
      loadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.save'))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleDelete(id: string) {
    setError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/autodiscovery-rules/${id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(t('errors.deleteRule'))
      setSuccess(t('success.deleted'))
      setDeleteId(null)
      loadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.delete'))
    }
  }

  async function openPreview(id: string, ruleName: string) {
    setPreviewModal({ id, ruleName, loading: true })
    try {
      const res = await apiFetch(`/api/terrapod/v1/autodiscovery-rules/${id}/preview`)
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || t('errors.previewFailed', { status: res.status }))
      }
      const json = await res.json()
      const attrs = json?.data?.attributes ?? {}
      setPreviewModal({
        id,
        ruleName,
        ref: attrs.ref,
        filesWalked: attrs['files-walked'],
        entries: attrs.entries ?? [],
      })
    } catch (err) {
      setPreviewModal({
        id,
        ruleName,
        error: err instanceof Error ? err.message : t('errors.preview'),
      })
    }
  }

  // Preview an *unsaved* rule — the create/edit form's "Preview" button
  // wires here so the operator can iterate on pattern + name_template +
  // ignore_patterns before any persistence (which would trigger an
  // immediate initial scan).
  async function previewFormRule() {
    if (!vcsConnectionId || !repoUrl || !pattern) {
      setError(t('errors.previewPrereq'))
      return
    }
    // id is empty for the unsaved-rule preview; the modal hides the
    // Provision button when collisions === 0 or when no id is set.
    setPreviewModal({ id: '', ruleName: name || t('unsavedRule'), loading: true })
    try {
      const payload = {
        data: {
          type: 'autodiscovery-rules',
          attributes: {
            name: name || 'preview',
            'vcs-connection-id': vcsConnectionId,
            'repo-url': repoUrl,
            branch,
            pattern,
            'ignore-patterns': ignorePatternsText
              .split('\n')
              .map(s => s.trim())
              .filter(Boolean),
            'name-template': nameTemplate,
            'execution-mode': executionMode,
            'agent-pool-id': agentPoolId || null,
            'execution-backend': executionBackend,
            'terraform-version': terraformVersion,
            'resource-cpu': resourceCpu,
            'resource-memory': resourceMemory,
            'auto-apply': autoApply,
            labels,
            'owner-email': ownerEmail,
          },
        },
      }
      const res = await apiFetch('/api/terrapod/v1/autodiscovery-rules/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify(payload),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || t('errors.previewFailed', { status: res.status }))
      }
      const json = await res.json()
      const attrs = json?.data?.attributes ?? {}
      setPreviewModal({
        id: '',
        ruleName: name || t('unsavedRule'),
        ref: attrs.ref,
        filesWalked: attrs['files-walked'],
        entries: attrs.entries ?? [],
      })
    } catch (err) {
      setPreviewModal({
        id: '',
        ruleName: name || t('unsavedRule'),
        error: err instanceof Error ? err.message : t('errors.preview'),
      })
    }
  }

  async function handleProvision() {
    if (!previewModal) return
    setPreviewModal({ ...previewModal, scanning: true, error: undefined })
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/autodiscovery-rules/${previewModal.id}/scan`,
        { method: 'POST' },
      )
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || t('errors.scanFailed', { status: res.status }))
      }
      const json = await res.json()
      const created = json?.data?.attributes?.['workspaces-created'] ?? 0
      setSuccess(t('success.provisioned', { count: created, ruleName: previewModal.ruleName }))
      setPreviewModal(null)
      loadAll()
    } catch (err) {
      setPreviewModal({
        ...previewModal,
        scanning: false,
        error: err instanceof Error ? err.message : t('errors.scan'),
      })
    }
  }

  if (loading) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={t('title')}
          description={t('description')}
          actions={
            <button
              onClick={() => (showForm ? setShowForm(false) : openCreateForm())}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showForm ? t('actions.cancel') : t('actions.newRule')}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}
        {success && (
          <div className="mb-4 px-4 py-3 rounded-lg bg-green-900/30 border border-green-800 text-green-300 text-sm">
            {success}
          </div>
        )}

        {showForm && (
          <form onSubmit={handleSubmit} className="mb-6 p-6 rounded-lg bg-slate-900/60 border border-slate-800 space-y-4">
            <h2 className="text-lg font-semibold text-slate-100">
              {editingId ? t('form.editTitle') : t('form.createTitle')}
            </h2>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm text-slate-300 mb-1">{t('form.name')}</label>
                <input
                  required
                  value={name}
                  onChange={e => setName(e.target.value)}
                  placeholder="monorepo"
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label className="block text-sm text-slate-300 mb-1">{t('form.vcsConnection')}</label>
                <select
                  required
                  value={vcsConnectionId}
                  onChange={e => setVcsConnectionId(e.target.value)}
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                >
                  <option value="">{t('form.selectConnection')}</option>
                  {connections.map(c => (
                    <option key={c.id} value={c.id}>
                      {c.attributes.name} ({c.attributes.provider})
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm text-slate-300 mb-1">{t('form.repoUrl')}</label>
                <input
                  required
                  value={repoUrl}
                  onChange={e => setRepoUrl(e.target.value)}
                  placeholder="https://github.com/org/monorepo"
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label className="block text-sm text-slate-300 mb-1">{t('form.branch')}</label>
                <input
                  value={branch}
                  onChange={e => setBranch(e.target.value)}
                  placeholder="main"
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                />
              </div>
            </div>

            <div>
              <label className="block text-sm text-slate-300 mb-1">{t('form.matchPattern')}</label>
              <input
                required
                value={pattern}
                onChange={e => setPattern(e.target.value)}
                placeholder="accounts/*/**/*.tf"
                className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm font-mono"
              />
              <p className="text-xs text-slate-500 mt-1">{t.rich('form.matchPatternHint', { code: (c) => <code>{c}</code> })}</p>
            </div>

            <div>
              <label className="block text-sm text-slate-300 mb-1">{t('form.ignorePatterns')}</label>
              <textarea
                rows={3}
                value={ignorePatternsText}
                onChange={e => setIgnorePatternsText(e.target.value)}
                placeholder="modules/**&#10;deprecated/**"
                className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm font-mono"
              />
            </div>

            <div>
              <label className="block text-sm text-slate-300 mb-1">{t('form.nameTemplate')}</label>
              <input
                value={nameTemplate}
                onChange={e => setNameTemplate(e.target.value)}
                placeholder="ws-{path}"
                className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm font-mono"
              />
              <p className="text-xs text-slate-500 mt-1">{t.rich('form.nameTemplateHint', { code: (c) => <code>{c}</code>, path: () => <code>{'{path}'}</code>, root: () => <code>{'{root}'}</code> })}</p>
            </div>

            <details className="group">
              <summary className="text-sm text-slate-300 cursor-pointer">{t('form.templateDefaults')}</summary>
              <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-4 pt-3 border-t border-slate-800">
                <div>
                  <label className="block text-sm text-slate-300 mb-1">{t('form.executionMode')}</label>
                  <input
                    value="agent"
                    disabled
                    className="w-full bg-slate-950/60 border border-slate-800 rounded px-3 py-2 text-sm text-slate-500"
                  />
                  <p className="text-xs text-slate-500 mt-1">{t('form.executionModeHint')}</p>
                </div>
                <div>
                  <label className="block text-sm text-slate-300 mb-1">{t('form.agentPool')}</label>
                  <select
                    value={agentPoolId}
                    onChange={e => setAgentPoolId(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                  >
                    <option value="">{t('form.none')}</option>
                    {pools.map(p => (
                      <option key={p.id} value={p.id}>{p.attributes.name}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-sm text-slate-300 mb-1">{t('form.executionBackend')}</label>
                  <select
                    value={executionBackend}
                    onChange={e => setExecutionBackend(e.target.value as 'tofu' | 'terraform')}
                    className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                  >
                    <option value="tofu">tofu</option>
                    <option value="terraform">terraform</option>
                  </select>
                </div>
                <div>
                  <label className="block text-sm text-slate-300 mb-1">{t('form.terraformVersion')}</label>
                  <input
                    value={terraformVersion}
                    onChange={e => setTerraformVersion(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label className="block text-sm text-slate-300 mb-1">{t('form.cpuRequest')}</label>
                  <input
                    value={resourceCpu}
                    onChange={e => setResourceCpu(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label className="block text-sm text-slate-300 mb-1">{t('form.memoryRequest')}</label>
                  <input
                    value={resourceMemory}
                    onChange={e => setResourceMemory(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label className="block text-sm text-slate-300 mb-1">{t('form.ownerEmail')}</label>
                  <input
                    value={ownerEmail}
                    onChange={e => setOwnerEmail(e.target.value)}
                    placeholder="platform@example.com"
                    className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                  />
                </div>
                <div className="flex items-center gap-4 pt-6">
                  <label className="flex items-center gap-2 text-sm text-slate-300">
                    <input
                      type="checkbox"
                      checked={autoApply}
                      onChange={e => setAutoApply(e.target.checked)}
                    />
                    {t('form.autoApply')}
                  </label>
                  <label className="flex items-center gap-2 text-sm text-slate-300">
                    <input
                      type="checkbox"
                      checked={enabled}
                      onChange={e => setEnabled(e.target.checked)}
                    />
                    {t('form.enabled')}
                  </label>
                </div>
              </div>
              <div className="mt-4 pt-4 border-t border-slate-800">
                <label className="block text-sm text-slate-300 mb-1">{t('form.onDirectoryDelete')}</label>
                <select
                  value={onDirectoryDelete}
                  onChange={e => setOnDirectoryDelete(e.target.value as 'flag' | 'destroy')}
                  className="w-full sm:w-1/2 bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                >
                  <option value="flag">{t('form.onDeleteFlag')}</option>
                  <option value="destroy">{t('form.onDeleteDestroy')}</option>
                </select>
                <p className="text-xs text-slate-500 mt-1">
                  {t('form.onDirectoryDeleteHint')}
                </p>
                {onDirectoryDelete === 'destroy' && (
                  <p className="text-xs text-red-400 mt-2 font-medium">
                    {t('form.onDeleteDestroyWarning')}
                  </p>
                )}
              </div>
              <div className="mt-4">
                <label className="block text-sm text-slate-300 mb-1">{t('form.labels')}</label>
                <LabelsEditor labels={labels} onChange={setLabels} />
              </div>
              <div className="mt-4 pt-4 border-t border-slate-800">
                <label className="block text-sm text-slate-300 mb-1">{t('form.varFiles')}</label>
                <p className="text-xs text-slate-500 mb-2">{t.rich('form.varFilesHint', { code: (c) => <code>{c}</code> })}</p>
                <StringListEditor
                  values={varFiles}
                  onChange={setVarFiles}
                  placeholder="env/prod.tfvars"
                  addLabel={t('form.addVarFile')}
                />
              </div>
              <div className="mt-4 pt-4 border-t border-slate-800">
                <label className="block text-sm text-slate-300 mb-1">{t('form.executionHooks')}</label>
                <p className="text-xs text-slate-500 mb-2">
                  {t.rich('form.executionHooksHint', { page: (c) => <span className="text-slate-400">{c}</span> })}
                </p>
                {hooks.length === 0 ? (
                  <p className="text-xs text-slate-500 italic">
                    {t('form.noHooks')}
                  </p>
                ) : (
                  <div className="space-y-1.5 rounded-lg border border-slate-800 bg-slate-900/40 p-3 max-h-56 overflow-y-auto">
                    {hooks.map(h => {
                      const checked = executionHookTemplates.includes(h.id)
                      return (
                        <label key={h.id} className="flex items-center gap-2 text-sm cursor-pointer">
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={e => {
                              setExecutionHookTemplates(prev =>
                                e.target.checked
                                  ? [...prev, h.id]
                                  : prev.filter(id => id !== h.id),
                              )
                            }}
                            className="rounded border-slate-600 bg-slate-800 text-brand-500 focus:ring-brand-500"
                          />
                          <span className="text-slate-200">{h.attributes.name}</span>
                          <span className="font-mono text-xs text-slate-500">{h.attributes['hook-point']}</span>
                          {!h.attributes.enabled && (
                            <span className="text-xs text-amber-400/80">{t('form.hookDisabled')}</span>
                          )}
                        </label>
                      )
                    })}
                  </div>
                )}
                {/* Preserve any templated hook id no longer present in the library (e.g. a
                    since-deleted hook) so editing an existing rule doesn't silently drop it. */}
                {executionHookTemplates
                  .filter(id => !hooks.some(h => h.id === id))
                  .map(id => (
                    <label key={id} className="flex items-center gap-2 text-xs mt-1.5 text-slate-500">
                      <input
                        type="checkbox"
                        checked
                        onChange={() =>
                          setExecutionHookTemplates(prev => prev.filter(x => x !== id))
                        }
                        className="rounded border-slate-600 bg-slate-800 text-brand-500 focus:ring-brand-500"
                      />
                      <span className="font-mono">{id}</span>
                      <span className="text-amber-400/80">{t('form.hookNotInLibrary')}</span>
                    </label>
                  ))}
              </div>
              <div className="mt-4 pt-4 border-t border-slate-800">
                <label className="block text-sm text-slate-300 mb-1">{t('form.runTaskTemplates')}</label>
                <RunTaskTemplatesEditor items={runTaskTemplates} onChange={setRunTaskTemplates} />
              </div>
              <div className="mt-4 pt-4 border-t border-slate-800">
                <label className="block text-sm text-slate-300 mb-1">{t('form.notificationTemplates')}</label>
                <NotificationTemplatesEditor
                  items={notificationTemplates}
                  onChange={setNotificationTemplates}
                />
              </div>
            </details>

            <div className="flex gap-2 pt-2">
              <button
                type="submit"
                disabled={submitting}
                className="px-4 py-2 rounded-lg bg-brand-600 hover:bg-brand-500 text-white text-sm font-medium disabled:opacity-50"
              >
                {submitting ? t('form.saving') : (editingId ? t('actions.update') : t('actions.create'))}
              </button>
              <button
                type="button"
                onClick={previewFormRule}
                disabled={submitting}
                className="px-4 py-2 rounded-lg bg-slate-700 hover:bg-slate-600 text-slate-100 text-sm disabled:opacity-50"
                title={t('form.previewTitle')}
              >
                {t('actions.preview')}
              </button>
              <button
                type="button"
                onClick={() => { setShowForm(false); resetForm() }}
                className="px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm"
              >
                {t('actions.cancel')}
              </button>
            </div>
          </form>
        )}

        {sortedItems.length === 0 ? (
          <EmptyState message={t('empty')} />
        ) : (
          <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-slate-400 border-b border-slate-800">
              <tr>
                <SortableHeader label={t('table.name')} sortKey="name" sortState={sortState} onSort={toggleSort} />
                <SortableHeader label={t('table.repo')} sortKey="repo" sortState={sortState} onSort={toggleSort} />
                <SortableHeader label={t('table.pattern')} sortKey="pattern" sortState={sortState} onSort={toggleSort} />
                <SortableHeader label={t('table.enabled')} sortKey="enabled" sortState={sortState} onSort={toggleSort} />
                <SortableHeader label={t('table.created')} sortKey="created" sortState={sortState} onSort={toggleSort} />
                <th className="py-2"></th>
              </tr>
            </thead>
            <tbody>
              {sortedItems.map(r => (
                <tr key={r.id} className="border-b border-slate-900 hover:bg-slate-900/40">
                  <td className="py-3 text-slate-200">{r.attributes.name}</td>
                  <td className="py-3 text-slate-400 font-mono text-xs">
                    {r.attributes['repo-url'].replace(/^https?:\/\//, '')}
                  </td>
                  <td className="py-3 text-slate-400 font-mono text-xs">{r.attributes.pattern}</td>
                  <td className="py-3">
                    {r.attributes.enabled ? (
                      <span className="text-green-400">{t('status.enabled')}</span>
                    ) : (
                      <span className="text-slate-500">{t('status.disabled')}</span>
                    )}
                  </td>
                  <td className="py-3 text-slate-400">
                    {fmt.date(r.attributes['created-at'])}
                  </td>
                  <td className="py-3 text-right">
                    <div className="flex justify-end gap-2">
                      <button
                        onClick={() => openPreview(r.id, r.attributes.name)}
                        className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-brand-300 transition-colors"
                        title={t('table.previewTitle')}
                      >
                        {t('actions.preview')}
                      </button>
                      <button
                        onClick={() => openEditForm(r)}
                        className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors"
                      >
                        {t('actions.edit')}
                      </button>
                      <button
                        onClick={() => setDeleteId(r.id)}
                        className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors"
                      >
                        {t('actions.delete')}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}

        {deleteId && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
            <div className="bg-slate-900 border border-slate-700 rounded-lg p-6 max-w-md w-full">
              <h3 className="text-lg font-semibold text-slate-100 mb-2">{t('deleteModal.title')}</h3>
              <p className="text-sm text-slate-400 mb-4">
                {t('deleteModal.body')}
              </p>
              <div className="flex gap-2 justify-end">
                <button
                  onClick={() => setDeleteId(null)}
                  className="px-4 py-2 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm"
                >
                  {t('actions.cancel')}
                </button>
                <button
                  onClick={() => handleDelete(deleteId)}
                  className="px-4 py-2 rounded bg-red-700 hover:bg-red-600 text-white text-sm"
                >
                  {t('actions.delete')}
                </button>
              </div>
            </div>
          </div>
        )}

        {previewModal && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
            <div className="bg-slate-900 border border-slate-700 rounded-lg p-6 max-w-3xl w-full max-h-[90vh] flex flex-col">
              <h3 className="text-lg font-semibold text-slate-100 mb-1">
                {t('preview.title', { ruleName: previewModal.ruleName })}
              </h3>
              <p className="text-xs text-slate-500 mb-4">
                {previewModal.ref ? (
                  t.rich('preview.walkedFiles', {
                    count: previewModal.filesWalked ?? 0,
                    ref: previewModal.ref,
                    files: (c) => <span className="text-slate-300">{c}</span>,
                    refspan: (c) => <span className="text-slate-300 font-mono">{c}</span>,
                  })
                ) : (
                  t('preview.walking')
                )}
              </p>

              {previewModal.loading && <LoadingSpinner />}

              {previewModal.error && (
                <div className="text-sm text-red-300 bg-red-900/20 border border-red-800/50 rounded p-3 mb-4">
                  {previewModal.error}
                </div>
              )}

              {previewModal.entries && previewModal.entries.length === 0 && (
                <div className="text-sm text-slate-400 py-4">
                  {t('preview.noMatches')}
                </div>
              )}

              {previewModal.entries && previewModal.entries.length > 0 && (
                <div className="overflow-auto flex-1 mb-4 border border-slate-700/50 rounded">
                  <table className="w-full text-sm">
                    <thead className="bg-slate-800/50 sticky top-0">
                      <tr className="border-b border-slate-700/50">
                        <th className="text-left px-3 py-2 text-xs font-medium text-slate-400 uppercase">{t('preview.colWorkspace')}</th>
                        <th className="text-left px-3 py-2 text-xs font-medium text-slate-400 uppercase">{t('preview.colDirectory')}</th>
                        <th className="text-left px-3 py-2 text-xs font-medium text-slate-400 uppercase">{t('preview.colAction')}</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-700/30">
                      {previewModal.entries.map(e => (
                        <tr key={e.working_directory}>
                          <td className="px-3 py-2 font-mono text-slate-200">{e.workspace_name}</td>
                          <td className="px-3 py-2 font-mono text-slate-400">{e.working_directory || t('preview.repoRoot')}</td>
                          <td className="px-3 py-2">
                            {!e.collision ? (
                              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-green-900/40 text-green-300">
                                {t('preview.actionCreate')}
                              </span>
                            ) : e.existing_autodiscovered ? (
                              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-700/40 text-slate-300" title={t('preview.skipDiscoveredTitle')}>
                                {t('preview.skipDiscovered')}
                              </span>
                            ) : (
                              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900/40 text-amber-300" title={t('preview.skipCollisionTitle')}>
                                {t('preview.skipCollision')}
                              </span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {previewModal.entries && previewModal.entries.length > 0 && (
                <p className="text-xs text-slate-500 mb-4">
                  {t('preview.willCreate', { count: previewModal.entries.filter(e => !e.collision).length })}
                </p>
              )}

              <div className="flex gap-2 justify-end">
                <button
                  onClick={() => setPreviewModal(null)}
                  disabled={previewModal.scanning}
                  className="px-4 py-2 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm disabled:opacity-50"
                >
                  {t('actions.close')}
                </button>
                {previewModal.id && previewModal.entries && previewModal.entries.filter(e => !e.collision).length > 0 && (
                  <button
                    onClick={handleProvision}
                    disabled={previewModal.scanning}
                    className="px-4 py-2 rounded bg-brand-600 hover:bg-brand-500 text-white text-sm disabled:opacity-50"
                  >
                    {previewModal.scanning
                      ? t('preview.provisioning')
                      : t('preview.provision', { count: previewModal.entries.filter(e => !e.collision).length })}
                  </button>
                )}
                {!previewModal.id && previewModal.entries && previewModal.entries.length > 0 && (
                  <span className="self-center text-xs text-slate-500 italic">
                    {t('preview.saveToProvision')}
                  </span>
                )}
              </div>
            </div>
          </div>
        )}
      </main>
    </>
  )
}
