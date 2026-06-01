'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
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

type SortKey = 'name' | 'repo' | 'pattern' | 'enabled' | 'created'

export default function AutodiscoveryPage() {
  const router = useRouter()
  const [rules, setRules] = useState<AutodiscoveryRule[]>([])
  const [connections, setConnections] = useState<VCSConnection[]>([])
  const [pools, setPools] = useState<AgentPool[]>([])
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
      const [rulesRes, connsRes, poolsRes] = await Promise.all([
        apiFetch('/api/terrapod/v1/autodiscovery-rules'),
        apiFetch('/api/terrapod/v1/vcs-connections'),
        apiFetch('/api/terrapod/v1/agent-pools'),
      ])
      if (!rulesRes.ok) throw new Error('Failed to load autodiscovery rules')
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
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load')
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
        throw new Error(`${editingId ? 'Update' : 'Create'} failed: ${res.status} ${txt}`)
      }
      setSuccess(editingId ? `Updated ${name}` : `Created ${name}`)
      setShowForm(false)
      resetForm()
      loadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save')
    } finally {
      setSubmitting(false)
    }
  }

  async function handleDelete(id: string) {
    setError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/autodiscovery-rules/${id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete rule')
      setSuccess('Deleted rule')
      setDeleteId(null)
      loadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete')
    }
  }

  async function openPreview(id: string, ruleName: string) {
    setPreviewModal({ id, ruleName, loading: true })
    try {
      const res = await apiFetch(`/api/terrapod/v1/autodiscovery-rules/${id}/preview`)
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || `Preview failed (${res.status})`)
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
        error: err instanceof Error ? err.message : 'Preview failed',
      })
    }
  }

  // Preview an *unsaved* rule — the create/edit form's "Preview" button
  // wires here so the operator can iterate on pattern + name_template +
  // ignore_patterns before any persistence (which would trigger an
  // immediate initial scan).
  async function previewFormRule() {
    if (!vcsConnectionId || !repoUrl || !pattern) {
      setError('Set VCS connection, repo URL, and pattern before previewing')
      return
    }
    // id is empty for the unsaved-rule preview; the modal hides the
    // Provision button when collisions === 0 or when no id is set.
    setPreviewModal({ id: '', ruleName: name || '(unsaved rule)', loading: true })
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
        throw new Error(body.detail || `Preview failed (${res.status})`)
      }
      const json = await res.json()
      const attrs = json?.data?.attributes ?? {}
      setPreviewModal({
        id: '',
        ruleName: name || '(unsaved rule)',
        ref: attrs.ref,
        filesWalked: attrs['files-walked'],
        entries: attrs.entries ?? [],
      })
    } catch (err) {
      setPreviewModal({
        id: '',
        ruleName: name || '(unsaved rule)',
        error: err instanceof Error ? err.message : 'Preview failed',
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
        throw new Error(body.detail || `Scan failed (${res.status})`)
      }
      const json = await res.json()
      const created = json?.data?.attributes?.['workspaces-created'] ?? 0
      setSuccess(`Created ${created} workspace${created === 1 ? '' : 's'} from "${previewModal.ruleName}"`)
      setPreviewModal(null)
      loadAll()
    } catch (err) {
      setPreviewModal({
        ...previewModal,
        scanning: false,
        error: err instanceof Error ? err.message : 'Scan failed',
      })
    }
  }

  if (loading) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="Autodiscovery"
          description="Auto-create workspaces in monorepos when PRs touch matching paths"
          actions={
            <button
              onClick={() => (showForm ? setShowForm(false) : openCreateForm())}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showForm ? 'Cancel' : 'New Rule'}
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
              {editingId ? 'Edit rule' : 'Create rule'}
            </h2>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm text-slate-300 mb-1">Name</label>
                <input
                  required
                  value={name}
                  onChange={e => setName(e.target.value)}
                  placeholder="monorepo"
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label className="block text-sm text-slate-300 mb-1">VCS connection</label>
                <select
                  required
                  value={vcsConnectionId}
                  onChange={e => setVcsConnectionId(e.target.value)}
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                >
                  <option value="">Select connection</option>
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
                <label className="block text-sm text-slate-300 mb-1">Repo URL</label>
                <input
                  required
                  value={repoUrl}
                  onChange={e => setRepoUrl(e.target.value)}
                  placeholder="https://github.com/org/monorepo"
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label className="block text-sm text-slate-300 mb-1">Branch (empty = default)</label>
                <input
                  value={branch}
                  onChange={e => setBranch(e.target.value)}
                  placeholder="main"
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                />
              </div>
            </div>

            <div>
              <label className="block text-sm text-slate-300 mb-1">Match pattern</label>
              <input
                required
                value={pattern}
                onChange={e => setPattern(e.target.value)}
                placeholder="accounts/*/**/*.tf"
                className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm font-mono"
              />
              <p className="text-xs text-slate-500 mt-1">Gitignore-style globs. <code>**</code> matches across path segments. Only <code>*.tf*</code> files trigger autodiscovery.</p>
            </div>

            <div>
              <label className="block text-sm text-slate-300 mb-1">Ignore patterns (one per line)</label>
              <textarea
                rows={3}
                value={ignorePatternsText}
                onChange={e => setIgnorePatternsText(e.target.value)}
                placeholder="modules/**&#10;deprecated/**"
                className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm font-mono"
              />
            </div>

            <div>
              <label className="block text-sm text-slate-300 mb-1">Workspace name template (optional)</label>
              <input
                value={nameTemplate}
                onChange={e => setNameTemplate(e.target.value)}
                placeholder="ws-{path}"
                className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm font-mono"
              />
              <p className="text-xs text-slate-500 mt-1">Use <code>{'{path}'}</code> for the directory with <code>/</code> replaced by <code>-</code>, or <code>{'{root}'}</code> for the directory as-is. Empty = default (just the dashed path).</p>
            </div>

            <details className="group">
              <summary className="text-sm text-slate-300 cursor-pointer">Workspace template defaults</summary>
              <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-4 pt-3 border-t border-slate-800">
                <div>
                  <label className="block text-sm text-slate-300 mb-1">Execution mode</label>
                  <input
                    value="agent"
                    disabled
                    className="w-full bg-slate-950/60 border border-slate-800 rounded px-3 py-2 text-sm text-slate-500"
                  />
                  <p className="text-xs text-slate-500 mt-1">Autodiscovery is VCS-driven; only agent execution is supported.</p>
                </div>
                <div>
                  <label className="block text-sm text-slate-300 mb-1">Agent pool</label>
                  <select
                    value={agentPoolId}
                    onChange={e => setAgentPoolId(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                  >
                    <option value="">(none)</option>
                    {pools.map(p => (
                      <option key={p.id} value={p.id}>{p.attributes.name}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-sm text-slate-300 mb-1">Execution backend</label>
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
                  <label className="block text-sm text-slate-300 mb-1">Terraform version</label>
                  <input
                    value={terraformVersion}
                    onChange={e => setTerraformVersion(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label className="block text-sm text-slate-300 mb-1">CPU request</label>
                  <input
                    value={resourceCpu}
                    onChange={e => setResourceCpu(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label className="block text-sm text-slate-300 mb-1">Memory request</label>
                  <input
                    value={resourceMemory}
                    onChange={e => setResourceMemory(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label className="block text-sm text-slate-300 mb-1">Owner email</label>
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
                    Auto-apply
                  </label>
                  <label className="flex items-center gap-2 text-sm text-slate-300">
                    <input
                      type="checkbox"
                      checked={enabled}
                      onChange={e => setEnabled(e.target.checked)}
                    />
                    Enabled
                  </label>
                </div>
              </div>
              <div className="mt-4 pt-4 border-t border-slate-800">
                <label className="block text-sm text-slate-300 mb-1">On directory delete</label>
                <select
                  value={onDirectoryDelete}
                  onChange={e => setOnDirectoryDelete(e.target.value as 'flag' | 'destroy')}
                  className="w-full sm:w-1/2 bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                >
                  <option value="flag">Flag (safe — mark pending deletion, requires manual action)</option>
                  <option value="destroy">Destroy (DANGER — tears down infrastructure then archives the workspace)</option>
                </select>
                <p className="text-xs text-slate-500 mt-1">
                  What happens to an autodiscovered workspace when its directory is removed on the tracked branch.
                </p>
                {onDirectoryDelete === 'destroy' && (
                  <p className="text-xs text-red-400 mt-2 font-medium">
                    This will run a real terraform destroy on the workspace when its directory is removed on the tracked branch.
                  </p>
                )}
              </div>
              <div className="mt-4">
                <label className="block text-sm text-slate-300 mb-1">Labels (inherited by created workspaces)</label>
                <LabelsEditor labels={labels} onChange={setLabels} />
              </div>
              <div className="mt-4 pt-4 border-t border-slate-800">
                <label className="block text-sm text-slate-300 mb-1">Var files (inherited by created workspaces)</label>
                <p className="text-xs text-slate-500 mb-2">Relative <code>.tfvars</code> paths passed to plan/apply.</p>
                <StringListEditor
                  values={varFiles}
                  onChange={setVarFiles}
                  placeholder="env/prod.tfvars"
                  addLabel="Add var file"
                />
              </div>
              <div className="mt-4 pt-4 border-t border-slate-800">
                <label className="block text-sm text-slate-300 mb-1">Run task templates (created on each new workspace)</label>
                <RunTaskTemplatesEditor items={runTaskTemplates} onChange={setRunTaskTemplates} />
              </div>
              <div className="mt-4 pt-4 border-t border-slate-800">
                <label className="block text-sm text-slate-300 mb-1">Notification templates (created on each new workspace)</label>
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
                {submitting ? 'Saving...' : (editingId ? 'Update' : 'Create')}
              </button>
              <button
                type="button"
                onClick={previewFormRule}
                disabled={submitting}
                className="px-4 py-2 rounded-lg bg-slate-700 hover:bg-slate-600 text-slate-100 text-sm disabled:opacity-50"
                title="Walk the repo and preview which workspaces this rule WOULD create — no persistence"
              >
                Preview
              </button>
              <button
                type="button"
                onClick={() => { setShowForm(false); resetForm() }}
                className="px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm"
              >
                Cancel
              </button>
            </div>
          </form>
        )}

        {sortedItems.length === 0 ? (
          <EmptyState message="No autodiscovery rules yet. Create one to start auto-provisioning workspaces from PRs in your monorepo." />
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-slate-400 border-b border-slate-800">
              <tr>
                <SortableHeader label="Name" sortKey="name" sortState={sortState} onSort={toggleSort} />
                <SortableHeader label="Repo" sortKey="repo" sortState={sortState} onSort={toggleSort} />
                <SortableHeader label="Pattern" sortKey="pattern" sortState={sortState} onSort={toggleSort} />
                <SortableHeader label="Enabled" sortKey="enabled" sortState={sortState} onSort={toggleSort} />
                <SortableHeader label="Created" sortKey="created" sortState={sortState} onSort={toggleSort} />
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
                      <span className="text-green-400">enabled</span>
                    ) : (
                      <span className="text-slate-500">disabled</span>
                    )}
                  </td>
                  <td className="py-3 text-slate-400">
                    {r.attributes['created-at']
                      ? new Date(r.attributes['created-at']).toLocaleDateString()
                      : ''}
                  </td>
                  <td className="py-3 text-right">
                    <button
                      onClick={() => openPreview(r.id, r.attributes.name)}
                      className="text-brand-400 hover:text-brand-300 text-xs px-2 py-1"
                      title="Walk the repo and preview which workspaces this rule would create"
                    >
                      Preview
                    </button>
                    <button
                      onClick={() => openEditForm(r)}
                      className="text-slate-400 hover:text-slate-200 text-xs px-2 py-1"
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => setDeleteId(r.id)}
                      className="text-red-400 hover:text-red-300 text-xs px-2 py-1"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {deleteId && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
            <div className="bg-slate-900 border border-slate-700 rounded-lg p-6 max-w-md w-full">
              <h3 className="text-lg font-semibold text-slate-100 mb-2">Delete rule?</h3>
              <p className="text-sm text-slate-400 mb-4">
                Workspaces auto-created by this rule keep working. Future PRs touching new paths under this rule won&apos;t auto-create more workspaces.
              </p>
              <div className="flex gap-2 justify-end">
                <button
                  onClick={() => setDeleteId(null)}
                  className="px-4 py-2 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm"
                >
                  Cancel
                </button>
                <button
                  onClick={() => handleDelete(deleteId)}
                  className="px-4 py-2 rounded bg-red-700 hover:bg-red-600 text-white text-sm"
                >
                  Delete
                </button>
              </div>
            </div>
          </div>
        )}

        {previewModal && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
            <div className="bg-slate-900 border border-slate-700 rounded-lg p-6 max-w-3xl w-full max-h-[90vh] flex flex-col">
              <h3 className="text-lg font-semibold text-slate-100 mb-1">
                Preview: {previewModal.ruleName}
              </h3>
              <p className="text-xs text-slate-500 mb-4">
                {previewModal.ref ? (
                  <>
                    Walked <span className="text-slate-300">{previewModal.filesWalked}</span> files on{' '}
                    <span className="text-slate-300 font-mono">{previewModal.ref}</span>
                  </>
                ) : (
                  'Walking repository…'
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
                  No directories in this repo match the rule&apos;s pattern. Nothing to create.
                </div>
              )}

              {previewModal.entries && previewModal.entries.length > 0 && (
                <div className="overflow-auto flex-1 mb-4 border border-slate-700/50 rounded">
                  <table className="w-full text-sm">
                    <thead className="bg-slate-800/50 sticky top-0">
                      <tr className="border-b border-slate-700/50">
                        <th className="text-left px-3 py-2 text-xs font-medium text-slate-400 uppercase">Workspace</th>
                        <th className="text-left px-3 py-2 text-xs font-medium text-slate-400 uppercase">Directory</th>
                        <th className="text-left px-3 py-2 text-xs font-medium text-slate-400 uppercase">Action</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-700/30">
                      {previewModal.entries.map(e => (
                        <tr key={e.working_directory}>
                          <td className="px-3 py-2 font-mono text-slate-200">{e.workspace_name}</td>
                          <td className="px-3 py-2 font-mono text-slate-400">{e.working_directory || '(repo root)'}</td>
                          <td className="px-3 py-2">
                            {!e.collision ? (
                              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-green-900/40 text-green-300">
                                Create
                              </span>
                            ) : e.existing_autodiscovered ? (
                              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-700/40 text-slate-300" title="Already created by this rule">
                                Skip (already discovered)
                              </span>
                            ) : (
                              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900/40 text-amber-300" title="A workspace with this name already exists for another reason">
                                Skip (name collision)
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
                  {previewModal.entries.filter(e => !e.collision).length} workspace(s) will be created.{' '}
                  Existing workspaces (collisions) are left untouched.
                </p>
              )}

              <div className="flex gap-2 justify-end">
                <button
                  onClick={() => setPreviewModal(null)}
                  disabled={previewModal.scanning}
                  className="px-4 py-2 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm disabled:opacity-50"
                >
                  Close
                </button>
                {previewModal.id && previewModal.entries && previewModal.entries.filter(e => !e.collision).length > 0 && (
                  <button
                    onClick={handleProvision}
                    disabled={previewModal.scanning}
                    className="px-4 py-2 rounded bg-brand-600 hover:bg-brand-500 text-white text-sm disabled:opacity-50"
                  >
                    {previewModal.scanning
                      ? 'Provisioning…'
                      : `Provision ${previewModal.entries.filter(e => !e.collision).length} workspace(s)`}
                  </button>
                )}
                {!previewModal.id && previewModal.entries && previewModal.entries.length > 0 && (
                  <span className="self-center text-xs text-slate-500 italic">
                    Save the rule to provision these workspaces.
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
