'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { getAuthState, getUserId } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

interface Token {
  id: string
  attributes: {
    description: string
    'token-type': string
    'created-at': string | null
    'last-used-at': string | null
    token: string | null
  }
}

export default function TokensPage() {
  const router = useRouter()
  const [tokens, setTokens] = useState<Token[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [showCreate, setShowCreate] = useState(false)
  const [description, setDescription] = useState('')
  const [creating, setCreating] = useState(false)
  const [createdToken, setCreatedToken] = useState<string | null>(null)

  const userId = getUserId()

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadTokens()
  }, [router])

  async function loadTokens() {
    setLoading(true)
    try {
      const res = await apiFetch(`/api/v2/users/${userId}/authentication-tokens`)
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
            attributes: { description },
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
      await loadTokens()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to revoke token')
    }
  }

  function formatDate(iso: string | null): string {
    if (!iso) return 'Never'
    return new Date(iso).toLocaleDateString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="API Tokens"
          description="Manage authentication tokens for CLI and automation"
          actions={
            <button
              onClick={() => { setShowCreate(!showCreate); setCreatedToken(null) }}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showCreate ? 'Cancel' : 'Create Token'}
            </button>
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
            <button
              type="submit"
              disabled={creating}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
            >
              {creating ? 'Creating...' : 'Create'}
            </button>
          </form>
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
                  <th className="text-left px-4 py-3 text-slate-400 font-medium">Description</th>
                  <th className="text-left px-4 py-3 text-slate-400 font-medium">Created</th>
                  <th className="text-left px-4 py-3 text-slate-400 font-medium">Last Used</th>
                  <th className="text-right px-4 py-3 text-slate-400 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {tokens.map((tok) => (
                  <tr key={tok.id} className="border-b border-slate-700/30 last:border-0">
                    <td className="px-4 py-3 text-slate-200">
                      {tok.attributes.description || <span className="text-slate-500 italic">No description</span>}
                    </td>
                    <td className="px-4 py-3 text-slate-400 text-xs">
                      {formatDate(tok.attributes['created-at'])}
                    </td>
                    <td className="px-4 py-3 text-slate-400 text-xs">
                      {formatDate(tok.attributes['last-used-at'])}
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
