'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter, useParams } from 'next/navigation'
import { useTranslations } from 'next-intl'
import Link from 'next/link'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { ConnectionStatus } from '@/components/connection-status'
import { LabelsEditor } from '@/components/labels-editor'
import { getAuthState } from '@/lib/auth'
import { useConfirm } from '@/lib/use-confirm'
import { apiFetch } from '@/lib/api'
import { usePoolEvents } from '@/lib/use-pool-events'
import { usePollingInterval } from '@/lib/use-polling-interval'

interface PoolAttrs {
  name: string
  description: string
  'is-default': boolean
  labels: Record<string, string>
  'owner-email': string
  permission?: string
  'created-at': string
  status?: 'online' | 'offline' | 'degraded'
}

function statusDotClass(status: string | undefined): string {
  if (status === 'online') return 'bg-green-400'
  if (status === 'cert-expired') return 'bg-red-400'
  if (status === 'degraded') return 'bg-amber-400'
  return 'bg-slate-500'
}

// Maps a listener status to the catalog key of its explanatory tooltip, or
// null when the status has no tooltip. The caller resolves the key via `t`.
function statusTitleKey(status: string | undefined): string | null {
  if (status === 'cert-expired') return 'listeners.tooltips.certExpired'
  if (status === 'degraded') return 'listeners.tooltips.degraded'
  return null
}

function defaultExpiryLocal(): string {
  // Mirror the server's default_join_token_ttl_seconds (3600s = 1h) and format
  // as a value the <input type="datetime-local"> accepts: "YYYY-MM-DDTHH:mm"
  // in the browser's local TZ. The form submits this back as a local datetime
  // and the API parses it as TZ-aware.
  const d = new Date(Date.now() + 60 * 60 * 1000)
  const pad = (n: number) => n.toString().padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

interface Pool {
  id: string
  attributes: PoolAttrs
}

interface PoolToken {
  id: string
  attributes: {
    description: string
    token: string | null
    'use-count': number
    'max-uses': number | null
    'expires-at': string | null
    revoked: boolean
    'created-by': string
    'created-at': string
  }
}

interface Listener {
  id: string
  attributes: {
    name: string
    status: string
    'replica-count'?: number
    'certificate-expires-at': string | null
    'created-at': string
  }
}

type Tab = 'settings' | 'tokens' | 'listeners'

export default function AgentPoolDetailPage() {
  const router = useRouter()
  const t = useTranslations('adminAgentPools')
  const params = useParams()
  const poolId = params.id as string
  const { confirmDelete } = useConfirm()

  const [pool, setPool] = useState<Pool | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [activeTab, setActiveTab] = useState<Tab>('settings')

  // Settings editing
  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const [editLabels, setEditLabels] = useState<Record<string, string>>({})
  const [editOwner, setEditOwner] = useState('')
  const [saving, setSaving] = useState(false)

  // Delete pool
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [deleting, setDeleting] = useState(false)

  // Tokens
  const [tokens, setTokens] = useState<PoolToken[]>([])
  const [tokensLoading, setTokensLoading] = useState(false)
  const [showCreateToken, setShowCreateToken] = useState(false)
  const [tokenDesc, setTokenDesc] = useState('')
  // Prepopulate to mirror the server-side defaults
  // (settings.agent_pools.default_join_token_max_uses=2, default_join_token_ttl_seconds=3600).
  // An empty form submitted as-is would still get these defaults applied at the
  // API, but blank inputs imply "no default" to the user — show what they'll
  // actually get so the choice is informed.
  const [tokenMaxUses, setTokenMaxUses] = useState('2')
  const [tokenExpiry, setTokenExpiry] = useState(() => defaultExpiryLocal())
  const [creatingToken, setCreatingToken] = useState(false)
  const [createdToken, setCreatedToken] = useState<string | null>(null)

  // Listeners
  const [listeners, setListeners] = useState<Listener[]>([])
  const [listenersLoading, setListenersLoading] = useState(false)

  const loadPool = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/terrapod/v1/agent-pools/${poolId}`)
      if (!res.ok) throw new Error(t('errors.loadPool'))
      const data = await res.json()
      setPool(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadPool'))
    } finally {
      setLoading(false)
    }
  }, [poolId, t])

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadPool()
  }, [router, loadPool])

  useEffect(() => {
    if (!pool) return
    if (activeTab === 'tokens') loadTokens()
    if (activeTab === 'listeners') loadListeners()
  }, [activeTab, pool])

  async function loadTokens() {
    try {
      const res = await apiFetch(`/api/terrapod/v1/agent-pools/${poolId}/tokens`)
      if (!res.ok) throw new Error(t('errors.loadTokens'))
      const data = await res.json()
      setTokens(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadTokens'))
    } finally {
      setTokensLoading(false)
    }
  }

  const loadListeners = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/terrapod/v1/agent-pools/${poolId}/listeners`)
      if (!res.ok) throw new Error(t('errors.loadListeners'))
      const data = await res.json()
      setListeners(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadListeners'))
    } finally {
      setListenersLoading(false)
    }
  }, [poolId, t])

  // Real-time listener updates via SSE (heartbeats, joins)
  const { connected: poolConnected } = usePoolEvents(poolId, useCallback(() => {
    loadListeners()
  }, [loadListeners]))

  // 60s polling fallback for listener offline detection (Redis TTL expiry doesn't generate events)
  usePollingInterval(activeTab === 'listeners', 60_000, loadListeners)

  function startEditing() {
    if (!pool) return
    setEditName(pool.attributes.name)
    setEditDesc(pool.attributes.description || '')
    setEditLabels(pool.attributes.labels || {})
    setEditOwner(pool.attributes['owner-email'] || '')
    setEditing(true)
  }

  async function handleSave() {
    setSaving(true)
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/agent-pools/${poolId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'agent-pools',
            attributes: {
              name: editName,
              description: editDesc,
              labels: editLabels,
              'owner-email': editOwner || null,
            },
          },
        }),
      })
      if (!res.ok) throw new Error(t('errors.updatePool'))
      const data = await res.json()
      setPool(data.data)
      setEditing(false)
      setSuccess(t('success.poolUpdated'))
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.updatePool'))
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete() {
    setDeleting(true)
    try {
      const res = await apiFetch(`/api/terrapod/v1/agent-pools/${poolId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(t('errors.deletePool'))
      router.push('/admin/agent-pools')
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.deletePool'))
      setDeleting(false)
    }
  }

  async function handleCreateToken(e: React.FormEvent) {
    e.preventDefault()
    setCreatingToken(true)
    setError('')
    setSuccess('')
    setCreatedToken(null)
    try {
      const attrs: Record<string, unknown> = { description: tokenDesc }
      if (tokenMaxUses) attrs['max-uses'] = parseInt(tokenMaxUses)
      if (tokenExpiry) attrs['expires-at'] = tokenExpiry

      const res = await apiFetch(`/api/terrapod/v1/agent-pools/${poolId}/tokens`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'agent-pool-tokens', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.createTokenStatus', { status: res.status }))
      }
      const data = await res.json()
      setCreatedToken(data.data?.attributes?.token || null)
      setTokenDesc('')
      setTokenMaxUses('2')
      setTokenExpiry(defaultExpiryLocal())
      setShowCreateToken(false)
      await loadTokens()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.createToken'))
    } finally {
      setCreatingToken(false)
    }
  }

  async function handleRevokeToken(tokenId: string) {
    if (!confirmDelete(t('confirm.revokeToken'))) return
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/agent-pools/${poolId}/tokens/${tokenId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(t('errors.revokeToken'))
      setSuccess(t('success.tokenRevoked'))
      await loadTokens()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.revokeToken'))
    }
  }

  async function handleDeleteListener(listenerId: string) {
    if (!confirmDelete(t('confirm.deleteListener'))) return
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/listeners/${listenerId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(t('errors.deleteListener'))
      setSuccess(t('success.listenerDeleted'))
      await loadListeners()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.deleteListener'))
    }
  }

  function formatDate(iso: string | null): string {
    if (!iso) return '-'
    return new Date(iso).toLocaleDateString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  }

  const poolPerm = pool?.attributes.permission || 'read'
  const isPoolAdmin = poolPerm === 'admin'

  const tabs: { key: Tab; label: string }[] = [
    { key: 'settings', label: t('tabs.settings') },
    ...(isPoolAdmin ? [{ key: 'tokens' as Tab, label: t('tabs.tokens') }] : []),
    { key: 'listeners', label: t('tabs.listeners') },
  ]

  if (loading) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>
  if (!pool) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><ErrorBanner message={t('errors.poolNotFound')} /></main></>

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <div className="mb-4">
          <Link href="/admin/agent-pools" className="text-sm text-slate-400 hover:text-slate-200">
            &larr; {t('detail.back')}
          </Link>
        </div>

        <PageHeader
          title={pool.attributes.name}
          description={pool.attributes.description || t('detail.defaultDescription')}
          actions={<ConnectionStatus connected={poolConnected} />}
        />

        {error && <ErrorBanner message={error} />}
        {success && (
          <div className="mb-4 p-3 bg-green-900/30 text-green-400 rounded-lg text-sm border border-green-800/50">{success}</div>
        )}

        {createdToken && (
          <div className="mb-6 p-4 bg-green-900/30 rounded-lg border border-green-800/50">
            <p className="text-sm text-green-300 font-medium mb-1">{t('tokens.created.title')}</p>
            <p className="text-xs text-green-400 mb-2">{t('tokens.created.copyNow')}</p>
            <div className="flex items-center gap-2">
              <code className="flex-1 text-sm text-green-200 bg-green-900/30 p-2 rounded font-mono overflow-x-auto">
                {createdToken}
              </code>
              <button
                onClick={() => navigator.clipboard.writeText(createdToken)}
                className="px-3 py-1 rounded text-xs font-medium bg-green-800/50 hover:bg-green-700/50 text-green-200 transition-colors flex-shrink-0"
              >
                {t('tokens.created.copy')}
              </button>
            </div>
          </div>
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
                <h3 className="text-sm font-medium text-slate-300">{t('settings.title')}</h3>
                {isPoolAdmin && (!editing ? (
                  <button onClick={startEditing} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors">{t('actions.edit')}</button>
                ) : (
                  <div className="flex gap-2">
                    <button onClick={() => setEditing(false)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors">{t('actions.cancel')}</button>
                    <button onClick={handleSave} disabled={saving} className="px-2.5 py-1 rounded-md text-xs font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors disabled:opacity-50">
                      {saving ? t('actions.saving') : t('actions.save')}
                    </button>
                  </div>
                ))}
              </div>
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <dt className="text-xs text-slate-500">{t('settings.fields.name')}</dt>
                  {editing ? (
                    <input type="text" value={editName} onChange={(e) => setEditName(e.target.value)}
                      pattern="[a-zA-Z0-9][a-zA-Z0-9_\-]*"
                      title={t('form.namePattern')}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{pool.attributes.name}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('settings.fields.description')}</dt>
                  {editing ? (
                    <input type="text" value={editDesc} onChange={(e) => setEditDesc(e.target.value)}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{pool.attributes.description || '-'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('settings.fields.created')}</dt>
                  <dd className="mt-1 text-sm text-slate-200">{formatDate(pool.attributes['created-at'])}</dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('settings.fields.owner')}</dt>
                  {editing ? (
                    <input type="email" value={editOwner} onChange={(e) => setEditOwner(e.target.value)}
                      placeholder="user@example.com"
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{pool.attributes['owner-email'] || '-'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('settings.fields.yourPermission')}</dt>
                  <dd className="mt-1">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                      poolPerm === 'admin' ? 'bg-purple-900/50 text-purple-300' :
                      poolPerm === 'write' ? 'bg-blue-900/50 text-blue-300' :
                      'bg-slate-700/50 text-slate-300'
                    }`}>{t(`settings.permissions.${poolPerm}`)}</span>
                  </dd>
                </div>
                <div className="sm:col-span-2">
                  <dt className="text-xs text-slate-500 mb-1">{t('settings.fields.labels')}</dt>
                  <dd>
                    <LabelsEditor
                      labels={editing ? editLabels : (pool.attributes.labels || {})}
                      onChange={editing ? setEditLabels : undefined}
                      readOnly={!editing}
                    />
                  </dd>
                </div>
              </dl>
            </div>

            {isPoolAdmin && !pool.attributes['is-default'] && (
              <div className="bg-slate-800/50 rounded-lg border border-red-900/30 p-6">
                <div className="flex items-center justify-between">
                  <div>
                    <h3 className="text-sm font-medium text-red-400">{t('danger.title')}</h3>
                    <p className="text-sm text-slate-400 mt-1">{t('danger.description')}</p>
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
            )}
          </div>
        )}

        {/* Tokens Tab */}
        {activeTab === 'tokens' && (
          <div>
            <div className="flex justify-end mb-4">
              <button
                onClick={() => { setShowCreateToken(!showCreateToken); setCreatedToken(null) }}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors"
              >
                {showCreateToken ? t('actions.cancel') : t('tokens.create')}
              </button>
            </div>

            {showCreateToken && (
              <form onSubmit={handleCreateToken} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <div>
                    <label htmlFor="tok-desc" className="block text-sm font-medium text-slate-300 mb-1">{t('tokens.form.description')}</label>
                    <input id="tok-desc" type="text" value={tokenDesc} onChange={(e) => setTokenDesc(e.target.value)}
                      placeholder={t('tokens.form.descriptionPlaceholder')}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="tok-max" className="block text-sm font-medium text-slate-300 mb-1">{t('tokens.form.maxUses')}</label>
                    <input id="tok-max" type="number" value={tokenMaxUses} onChange={(e) => setTokenMaxUses(e.target.value)}
                      min="1"
                      step="1"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                    <p className="mt-1 text-xs text-slate-500">{t('tokens.form.maxUsesHint')}</p>
                  </div>
                  <div>
                    <label htmlFor="tok-exp" className="block text-sm font-medium text-slate-300 mb-1">{t('tokens.form.expiresAt')}</label>
                    <input id="tok-exp" type="datetime-local" value={tokenExpiry} onChange={(e) => setTokenExpiry(e.target.value)}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                    <p className="mt-1 text-xs text-slate-500">{t('tokens.form.expiresAtHint')}</p>
                  </div>
                </div>
                <button type="submit" disabled={creatingToken}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {creatingToken ? t('actions.creating') : t('tokens.create')}
                </button>
              </form>
            )}

            {tokensLoading ? (
              <LoadingSpinner />
            ) : tokens.length === 0 ? (
              <EmptyState message={t('tokens.empty')} />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">{t('tokens.columns.description')}</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden sm:table-cell">{t('tokens.columns.uses')}</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden md:table-cell">{t('tokens.columns.expires')}</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden md:table-cell">{t('tokens.columns.status')}</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden lg:table-cell">{t('tokens.columns.createdBy')}</th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">{t('tokens.columns.actions')}</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {tokens.map((tok) => (
                      <tr key={tok.id} className="hover:bg-slate-700/20 transition-colors">
                        <td className="px-4 py-3 text-sm text-slate-200">{tok.attributes.description || '-'}</td>
                        <td className="px-4 py-3 text-xs text-slate-400 hidden sm:table-cell">
                          {tok.attributes['use-count']}{tok.attributes['max-uses'] ? ` / ${tok.attributes['max-uses']}` : ''}
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-400 hidden md:table-cell">
                          {formatDate(tok.attributes['expires-at'])}
                        </td>
                        <td className="px-4 py-3 hidden md:table-cell">
                          <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                            tok.attributes.revoked ? 'bg-red-900/50 text-red-300' : 'bg-green-900/50 text-green-300'
                          }`}>
                            {tok.attributes.revoked ? t('tokens.badges.revoked') : t('tokens.badges.active')}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-400 hidden lg:table-cell">{tok.attributes['created-by']}</td>
                        <td className="px-4 py-3 text-right">
                          {!tok.attributes.revoked && (
                            <button onClick={() => handleRevokeToken(tok.id)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors">{t('tokens.revoke')}</button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {/* Listeners Tab */}
        {activeTab === 'listeners' && (
          <div>
            {listenersLoading ? (
              <LoadingSpinner />
            ) : listeners.length === 0 ? (
              <EmptyState message={t('listeners.empty')} />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">{t('listeners.columns.name')}</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">{t('listeners.columns.status')}</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">{t('listeners.columns.replicas')}</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden md:table-cell">{t('listeners.columns.certExpires')}</th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">{t('listeners.columns.actions')}</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {listeners.map((l) => (
                      <tr key={l.id} className="hover:bg-slate-700/20 transition-colors">
                        <td className="px-4 py-3 text-sm text-slate-200">{l.attributes.name}</td>
                        <td className="px-4 py-3">
                          <span className="flex items-center gap-1.5" title={(() => { const k = statusTitleKey(l.attributes.status); return k ? t(k) : undefined })()}>
                            <span className={`w-2 h-2 rounded-full ${statusDotClass(l.attributes.status)}`} />
                            <span className="text-xs text-slate-400">{t(`listeners.status.${l.attributes.status}`)}</span>
                          </span>
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-300">
                          {/* `replica-count` is omitted entirely for listeners on
                              pre-0.19.0 images (they don't write per-pod keys, so
                              the count would always look like 0). When present,
                              0 is meaningful: tracking is on, no pods are
                              currently heartbeating. */}
                          {typeof l.attributes['replica-count'] === 'number'
                            ? <span className={l.attributes['replica-count'] === 0 ? 'text-amber-400' : ''}>{l.attributes['replica-count']}</span>
                            : <span className="text-slate-500">—</span>}
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-400 hidden md:table-cell">
                          {formatDate(l.attributes['certificate-expires-at'])}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <button onClick={() => handleDeleteListener(l.id)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors">{t('actions.delete')}</button>
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
