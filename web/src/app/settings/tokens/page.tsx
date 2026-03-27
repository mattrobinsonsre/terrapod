'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { getAuthState, getUserId, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { useSortable } from '@/lib/use-sortable'

interface Token {
  id: string
  attributes: {
    description: string
    'token-type': string
    'created-by': string
    'created-at': string | null
    'last-used-at': string | null
    'expires-at': string | null
    'lifespan-hours': number | null
    token: string | null
  }
}

const LIFESPAN_OPTIONS = [
  { label: '30 days', hours: 720 },
  { label: '90 days', hours: 2160 },
  { label: '180 days', hours: 4320 },
  { label: '1 year', hours: 8760 },
]

export default function TokensPage() {
  const router = useRouter()
  const [tokens, setTokens] = useState<Token[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [showCreate, setShowCreate] = useState(false)
  const [description, setDescription] = useState('')
  const [lifespanHours, setLifespanHours] = useState<number>(8760)
  const [creating, setCreating] = useState(false)
  const [createdToken, setCreatedToken] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [revoking, setRevoking] = useState(false)
  const [admin, setAdmin] = useState(false)
  const [userId, setUserId] = useState('')

  type TokenSortKey = 'description' | 'created-by' | 'created-at' | 'last-used-at' | 'expires-at'
  const { sortedItems: sortedTokens, sortState, toggleSort } = useSortable<Token, TokenSortKey>(
    tokens, 'created-at', 'desc',
    useCallback((item: Token, key: TokenSortKey) => {
      switch (key) {
        case 'description': return item.attributes.description
        case 'created-by': return item.attributes['created-by']
        case 'created-at': return item.attributes['created-at']
        case 'last-used-at': return item.attributes['last-used-at']
        case 'expires-at': return item.attributes['expires-at']
      }
    }, []),
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    setAdmin(isAdmin())
    setUserId(getUserId())
  }, [router])

  useEffect(() => {
    if (!userId) return
    loadTokens()
  }, [userId, showAll])

  async function loadTokens() {
    setLoading(true)
    try {
      const url = showAll
        ? '/api/v2/admin/authentication-tokens'
        : `/api/v2/users/${userId}/authentication-tokens`
      const res = await apiFetch(url)
      if (!res.ok) throw new Error('Failed to load tokens')
      const data = await res.json()
      setTokens(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load tokens')
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    setCreatedToken(null)
    try {
      const res = await apiFetch(`/api/v2/users/${userId}/authentication-tokens`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'authentication-tokens',
            attributes: {
              description,
              lifespan_hours: lifespanHours,
            },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to create token (${res.status})`)
      }
      const data = await res.json()
      setCreatedToken(data.data?.attributes?.token || null)
      setDescription('')
      setLifespanHours(8760)
      setShowCreate(false)
      await loadTokens()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create token')
    } finally {
      setCreating(false)
    }
  }

  async function handleRevoke(tokenId: string) {
    setError('')
    try {
      const res = await apiFetch(`/api/v2/authentication-tokens/${tokenId}`, {
        method: 'DELETE',
      })
      if (!res.ok && res.status !== 204) throw new Error(`Failed to revoke token (${res.status})`)
      setSelected((prev) => { const next = new Set(prev); next.delete(tokenId); return next })
      await loadTokens()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to revoke token')
    }
  }

  async function handleBulkRevoke() {
    if (selected.size === 0) return
    if (!confirm(`Revoke ${selected.size} token${selected.size > 1 ? 's' : ''}?`)) return
    setRevoking(true)
    setError('')
    try {
      const ids = [...selected]
      await Promise.all(ids.map((id) =>
        apiFetch(`/api/v2/authentication-tokens/${id}`, { method: 'DELETE' })
      ))
      setSelected(new Set())
      await loadTokens()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to revoke tokens')
    } finally {
      setRevoking(false)
    }
  }

  function toggleSelect(id: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  function toggleSelectAll() {
    if (selected.size === sortedTokens.length) {
      setSelected(new Set())
    } else {
      setSelected(new Set(sortedTokens.map((t) => t.id)))
    }
  }

  function formatDate(iso: string | null): string {
    if (!iso) return 'Never'
    return new Date(iso).toLocaleDateString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  }

  function expiryColor(iso: string | null): string {
    if (!iso) return 'text-slate-400'
    const now = Date.now()
    const expires = new Date(iso).getTime()
    if (expires <= now) return 'text-red-400'
    if (expires - now < 30 * 24 * 60 * 60 * 1000) return 'text-amber-400'
    return 'text-slate-400'
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="API Tokens"
          description={showAll ? 'All tokens across all users' : 'Manage authentication tokens for CLI and automation'}
          actions={
            <div className="flex items-center gap-3">
              {admin && (
                <button
                  onClick={() => setShowAll(!showAll)}
                  className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                    showAll
                      ? 'bg-amber-600 hover:bg-amber-500 text-white'
                      : 'bg-slate-700 hover:bg-slate-600 text-slate-300'
                  }`}
                >
                  {showAll ? 'My Tokens' : 'All Tokens'}
                </button>
              )}
              <button
                onClick={() => { setShowCreate(!showCreate); setCreatedToken(null) }}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
              >
                {showCreate ? 'Cancel' : 'Create Token'}
              </button>
            </div>
          }
        />

        {error && <ErrorBanner message={error} />}

        {createdToken && (
          <div className="mb-6 p-4 bg-green-900/30 rounded-lg border border-green-800/50">
            <p className="text-sm text-green-300 font-medium mb-1">Token created successfully</p>
            <p className="text-xs text-green-400 mb-2">Copy this token now — it will not be shown again.</p>
            <div className="flex items-center gap-2">
              <code className="flex-1 text-sm text-green-200 bg-green-900/30 p-2 rounded font-mono overflow-x-auto">
                {createdToken}
              </code>
              <button
                onClick={() => navigator.clipboard.writeText(createdToken)}
                className="px-3 py-1 rounded text-xs font-medium bg-green-800/50 hover:bg-green-700/50 text-green-200 transition-colors flex-shrink-0"
              >
                Copy
              </button>
            </div>
          </div>
        )}

        {showCreate && (
          <form onSubmit={handleCreate} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 flex items-end gap-3">
            <div className="flex-1">
              <label htmlFor="tok-desc" className="block text-sm font-medium text-slate-300 mb-1">Description</label>
              <input
                id="tok-desc"
                type="text"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="CI/CD pipeline token"
                className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              />
            </div>
            <div className="w-40">
              <label htmlFor="tok-lifespan" className="block text-sm font-medium text-slate-300 mb-1">Lifespan</label>
              <select
                id="tok-lifespan"
                value={lifespanHours}
                onChange={(e) => setLifespanHours(Number(e.target.value))}
                className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              >
                {LIFESPAN_OPTIONS.map((opt) => (
                  <option key={opt.hours} value={opt.hours}>{opt.label}</option>
                ))}
              </select>
            </div>
            <button
              type="submit"
              disabled={creating}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
            >
              {creating ? 'Creating...' : 'Create'}
            </button>
          </form>
        )}

        {selected.size > 0 && (
          <div className="mb-4 flex items-center gap-3 p-3 bg-red-900/20 rounded-lg border border-red-800/30">
            <span className="text-sm text-slate-300">{selected.size} token{selected.size > 1 ? 's' : ''} selected</span>
            <button
              onClick={handleBulkRevoke}
              disabled={revoking}
              className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-500 disabled:bg-red-800 disabled:text-red-400 text-white transition-colors"
            >
              {revoking ? 'Revoking...' : `Revoke Selected`}
            </button>
            <button
              onClick={() => setSelected(new Set())}
              className="text-sm text-slate-400 hover:text-slate-300 transition-colors"
            >
              Clear selection
            </button>
          </div>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : tokens.length === 0 ? (
          <EmptyState message="No API tokens yet." />
        ) : (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <th className="w-10 px-4 py-3">
                    <input
                      type="checkbox"
                      checked={sortedTokens.length > 0 && selected.size === sortedTokens.length}
                      onChange={toggleSelectAll}
                      className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
                    />
                  </th>
                  <SortableHeader label="Description" sortKey="description" sortState={sortState} onSort={toggleSort} />
                  {showAll && <SortableHeader label="Created By" sortKey="created-by" sortState={sortState} onSort={toggleSort} />}
                  <SortableHeader label="Created" sortKey="created-at" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label="Last Used" sortKey="last-used-at" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label="Expires" sortKey="expires-at" sortState={sortState} onSort={toggleSort} />
                  <th className="text-right px-4 py-3 text-slate-400 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {sortedTokens.map((tok) => (
                  <tr key={tok.id} className={`border-b border-slate-700/30 last:border-0 ${selected.has(tok.id) ? 'bg-brand-900/10' : ''}`}>
                    <td className="w-10 px-4 py-3">
                      <input
                        type="checkbox"
                        checked={selected.has(tok.id)}
                        onChange={() => toggleSelect(tok.id)}
                        className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
                      />
                    </td>
                    <td className="px-4 py-3 text-slate-200">
                      {tok.attributes.description || <span className="text-slate-500 italic">No description</span>}
                    </td>
                    {showAll && (
                      <td className="px-4 py-3 text-slate-400 text-xs">
                        {tok.attributes['created-by']}
                      </td>
                    )}
                    <td className="px-4 py-3 text-slate-400 text-xs">
                      {formatDate(tok.attributes['created-at'])}
                    </td>
                    <td className="px-4 py-3 text-slate-400 text-xs">
                      {formatDate(tok.attributes['last-used-at'])}
                    </td>
                    <td className={`px-4 py-3 text-xs ${expiryColor(tok.attributes['expires-at'])}`}>
                      {formatDate(tok.attributes['expires-at'])}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <button
                        onClick={() => handleRevoke(tok.id)}
                        className="text-xs text-red-400 hover:text-red-300 transition-colors"
                      >
                        Revoke
                      </button>
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
