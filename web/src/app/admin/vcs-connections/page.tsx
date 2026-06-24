'use client'

import { useEffect, useState, useCallback, useRef } from 'react'
import { useRouter } from 'next/navigation'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { useSortable } from '@/lib/use-sortable'
import { usePollingInterval } from '@/lib/use-polling-interval'

interface VCSConnection {
  id: string
  attributes: {
    name: string
    provider: string
    'server-url': string
    status: string
    'github-app-id': string | null
    'github-installation-id': string | null
    'github-account-login': string | null
    'has-token': boolean
    'has-webhook-secret'?: boolean
    'created-at': string
  }
}

type VCSSortKey = 'name' | 'provider' | 'server-url' | 'status' | 'created'

export default function VCSConnectionsPage() {
  const router = useRouter()
  const [connections, setConnections] = useState<VCSConnection[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  // Create form
  const [showCreate, setShowCreate] = useState(false)
  const [provider, setProvider] = useState<'github' | 'gitlab'>('github')
  const [name, setName] = useState('')
  const [serverUrl, setServerUrl] = useState('')
  // GitHub fields
  const [appId, setAppId] = useState('')
  const [installationId, setInstallationId] = useState('')
  const [privateKey, setPrivateKey] = useState('')
  const [webhookSecret, setWebhookSecret] = useState('')
  const [pemDragOver, setPemDragOver] = useState(false)
  const pemFileRef = useRef<HTMLInputElement>(null)
  // GitLab fields
  const [token, setToken] = useState('')
  const [creating, setCreating] = useState(false)
  // When set, the form is editing this connection (PATCH) rather than creating.
  const [editId, setEditId] = useState<string | null>(null)

  // Delete confirmation
  const [deleteId, setDeleteId] = useState<string | null>(null)

  function resetForm() {
    setName(''); setServerUrl(''); setAppId(''); setInstallationId('')
    setPrivateKey(''); setToken(''); setWebhookSecret(''); setProvider('github'); setEditId(null)
  }

  function startEdit(conn: VCSConnection) {
    setEditId(conn.id)
    setName(conn.attributes.name)
    setProvider(conn.attributes.provider as 'github' | 'gitlab')
    setServerUrl(conn.attributes['server-url'] || '')
    setAppId(conn.attributes['github-app-id'] ? String(conn.attributes['github-app-id']) : '')
    setInstallationId(
      conn.attributes['github-installation-id']
        ? String(conn.attributes['github-installation-id'])
        : '',
    )
    // Credentials are write-only — never returned. Leave blank = keep.
    setPrivateKey('')
    setToken('')
    setWebhookSecret('')
    setShowCreate(true)
    setError(''); setSuccess('')
  }

  const vcsAccessor = useCallback((item: VCSConnection, key: VCSSortKey) => {
    switch (key) {
      case 'name': return item.attributes.name
      case 'provider': return item.attributes.provider
      case 'server-url': return item.attributes['server-url']
      case 'status': return item.attributes.status
      case 'created': return item.attributes['created-at']
    }
  }, [])

  const { sortedItems, sortState, toggleSort } = useSortable<VCSConnection, VCSSortKey>(
    connections, 'name', 'asc', vcsAccessor,
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadConnections()
  }, [router])

  usePollingInterval(!loading, 60_000, loadConnections)

  async function loadConnections() {
    try {
      const res = await apiFetch('/api/terrapod/v1/vcs-connections')
      if (!res.ok) throw new Error('Failed to load VCS connections')
      const data = await res.json()
      setConnections(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load VCS connections')
    } finally {
      setLoading(false)
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    setSuccess('')
    try {
      const editing = editId !== null
      const attrs: Record<string, unknown> = { name }
      if (!editing) attrs.provider = provider
      if (serverUrl || editing) attrs['server-url'] = serverUrl
      if (provider === 'github') {
        attrs['github-app-id'] = appId
        attrs['github-installation-id'] = installationId
        // On edit, credentials are optional — only send when rotating.
        if (privateKey) attrs['private-key'] = privateKey
        else if (!editing) attrs['private-key'] = privateKey
        // Optional per-connection webhook secret — only send when set/rotating.
        if (webhookSecret) attrs['webhook-secret'] = webhookSecret
      } else {
        if (token) attrs.token = token
        else if (!editing) attrs.token = token
      }
      const url = editing
        ? `/api/terrapod/v1/vcs-connections/${editId}`
        : '/api/terrapod/v1/vcs-connections'
      const res = await apiFetch(url, {
        method: editing ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'vcs-connections', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(
          data.detail || `Failed to ${editing ? 'update' : 'create'} connection (${res.status})`,
        )
      }
      setSuccess(`VCS connection "${name}" ${editing ? 'updated' : 'created'}`)
      resetForm()
      setShowCreate(false)
      await loadConnections()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save connection')
    } finally {
      setCreating(false)
    }
  }

  async function handleDelete(id: string) {
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/vcs-connections/${id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete connection')
      setDeleteId(null)
      setSuccess('VCS connection deleted')
      await loadConnections()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete connection')
    }
  }

  function providerBadge(p: string) {
    return p === 'github'
      ? 'bg-slate-700 text-slate-200'
      : 'bg-orange-900/50 text-orange-300'
  }

  function statusBadge(s: string) {
    return s === 'active'
      ? 'bg-green-900/50 text-green-300'
      : 'bg-slate-700 text-slate-400'
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="VCS Connections"
          description="Manage version control system integrations"
          actions={
            <button
              onClick={() => {
                if (showCreate) { setShowCreate(false); resetForm() }
                else { resetForm(); setShowCreate(true) }
              }}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showCreate ? 'Cancel' : 'New Connection'}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}
        {success && (
          <div className="mb-4 p-3 bg-green-900/30 text-green-400 rounded-lg text-sm border border-green-800/50">{success}</div>
        )}

        {showCreate && (
          <form onSubmit={handleSubmit} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div>
                <label htmlFor="vcs-name" className="block text-sm font-medium text-slate-300 mb-1">Name</label>
                <input id="vcs-name" type="text" value={name} onChange={(e) => setName(e.target.value)} required
                  pattern="[a-zA-Z0-9][a-zA-Z0-9_\-]*"
                  title="Letters, numbers, hyphens, and underscores only. Must start with a letter or number."
                  placeholder="my-github-app"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
              <div>
                <label htmlFor="vcs-provider" className="block text-sm font-medium text-slate-300 mb-1">Provider</label>
                <select id="vcs-provider" value={provider} disabled={editId !== null}
                  onChange={(e) => setProvider(e.target.value as 'github' | 'gitlab')}
                  title={editId !== null ? 'Provider is immutable — delete and recreate to change it' : undefined}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 disabled:opacity-60 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                  <option value="github">GitHub</option>
                  <option value="gitlab">GitLab</option>
                </select>
              </div>
              <div>
                <label htmlFor="vcs-url" className="block text-sm font-medium text-slate-300 mb-1">Server URL (optional)</label>
                <input id="vcs-url" type="text" value={serverUrl} onChange={(e) => setServerUrl(e.target.value)}
                  pattern="https?://.+"
                  title="Must be an HTTP or HTTPS URL (e.g. https://github.mycompany.com)"
                  placeholder={provider === 'github' ? 'https://api.github.com' : 'https://gitlab.com'}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
            </div>

            {provider === 'github' ? (
              <div className="space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="gh-app-id" className="block text-sm font-medium text-slate-300 mb-1">App ID</label>
                    <input id="gh-app-id" type="text" value={appId} onChange={(e) => setAppId(e.target.value)} required
                      pattern="[0-9]+"
                      title="GitHub App ID — numeric digits only"
                      placeholder="123456"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="gh-install-id" className="block text-sm font-medium text-slate-300 mb-1">Installation ID</label>
                    <input id="gh-install-id" type="text" value={installationId} onChange={(e) => setInstallationId(e.target.value)} required
                      pattern="[0-9]+"
                      title="GitHub App Installation ID — numeric digits only"
                      placeholder="789012"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                </div>
                <div>
                  <div className="flex items-center gap-2 mb-1">
                    <label htmlFor="gh-key" className="block text-sm font-medium text-slate-300">Private Key (PEM)</label>
                    <button type="button" onClick={() => pemFileRef.current?.click()}
                      className="text-xs text-brand-400 hover:text-brand-300 transition-colors">Browse...</button>
                    <input ref={pemFileRef} type="file" accept=".pem,.key" className="hidden"
                      onChange={(e) => { const f = e.target.files?.[0]; if (f) f.text().then(t => setPrivateKey(t)) }} />
                  </div>
                  <textarea id="gh-key" value={privateKey} onChange={(e) => setPrivateKey(e.target.value)} required={editId === null} rows={4}
                    placeholder={editId !== null
                      ? 'Leave blank to keep the current key — paste a new PEM to rotate'
                      : '-----BEGIN RSA PRIVATE KEY-----&#10;...&#10;&#10;Drop a .pem file here or click Browse'}
                    className={`w-full px-3 py-2 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent font-mono text-xs transition-colors ${pemDragOver ? 'border-2 border-dashed border-brand-400 bg-brand-900/20' : 'border border-slate-600'}`}
                    onDragOver={(e) => { e.preventDefault(); setPemDragOver(true) }}
                    onDragLeave={() => setPemDragOver(false)}
                    onDrop={(e) => {
                      e.preventDefault()
                      setPemDragOver(false)
                      const f = e.dataTransfer.files[0]
                      if (f) f.text().then(t => setPrivateKey(t))
                    }}
                  />
                </div>
                <div>
                  <label htmlFor="gh-webhook-secret" className="block text-sm font-medium text-slate-300 mb-1">
                    Webhook Secret <span className="text-slate-500 font-normal">(optional)</span>
                  </label>
                  <input id="gh-webhook-secret" type="password" value={webhookSecret}
                    onChange={(e) => setWebhookSecret(e.target.value)}
                    placeholder={editId !== null
                      ? (connections.find((c) => c.id === editId)?.attributes['has-webhook-secret']
                          ? 'Set — leave blank to keep, enter a new value to rotate'
                          : 'Leave blank to use the global secret')
                      : 'Leave blank to use the global webhook secret'}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  <p className="mt-1 text-xs text-slate-500">
                    Per-connection HMAC secret for this installation&apos;s webhooks. When unset, the global secret is used.
                  </p>
                </div>
              </div>
            ) : (
              <div>
                <label htmlFor="gl-token" className="block text-sm font-medium text-slate-300 mb-1">Access Token</label>
                <input id="gl-token" type="password" value={token} onChange={(e) => setToken(e.target.value)} required={editId === null}
                  placeholder={editId !== null ? 'Leave blank to keep the current token' : 'glpat-...'}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
            )}

            <button type="submit" disabled={creating}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
              {creating
                ? (editId !== null ? 'Saving...' : 'Creating...')
                : (editId !== null ? 'Save Changes' : 'Create Connection')}
            </button>
          </form>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : connections.length === 0 ? (
          <EmptyState message="No VCS connections configured." />
        ) : (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <SortableHeader label="Name" sortKey="name" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label="Provider" sortKey="provider" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label="Server URL" sortKey="server-url" sortState={sortState} onSort={toggleSort} className="hidden sm:table-cell" />
                  <SortableHeader label="Status" sortKey="status" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
                  <SortableHeader label="Created" sortKey="created" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                  <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700/30">
                {sortedItems.map((conn) => (
                  <tr key={conn.id} className="hover:bg-slate-700/20 transition-colors">
                    <td className="px-4 py-3 text-sm text-slate-200">{conn.attributes.name}</td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${providerBadge(conn.attributes.provider)}`}>
                        {conn.attributes.provider}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-400 hidden sm:table-cell">
                      {conn.attributes['server-url'] || '-'}
                    </td>
                    <td className="px-4 py-3 hidden md:table-cell">
                      <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${statusBadge(conn.attributes.status)}`}>
                        {conn.attributes.status}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-500 hidden lg:table-cell">
                      {conn.attributes['created-at'] ? new Date(conn.attributes['created-at']).toLocaleDateString() : ''}
                    </td>
                    <td className="px-4 py-3 text-right">
                      {deleteId === conn.id ? (
                        <div className="flex justify-end gap-2">
                          <button onClick={() => setDeleteId(null)} className="text-xs text-slate-400 hover:text-slate-200">Cancel</button>
                          <button onClick={() => handleDelete(conn.id)} className="text-xs text-red-400 hover:text-red-300">Confirm</button>
                        </div>
                      ) : (
                        <div className="flex justify-end gap-3">
                          <button onClick={() => startEdit(conn)} className="text-xs text-brand-400 hover:text-brand-300">Edit</button>
                          <button onClick={() => setDeleteId(conn.id)} className="text-xs text-red-400 hover:text-red-300">Delete</button>
                        </div>
                      )}
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
