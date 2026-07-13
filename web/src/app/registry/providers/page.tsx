'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import Link from 'next/link'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { getAuthState } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { usePollingInterval } from '@/lib/use-polling-interval'

interface Provider {
  id: string
  attributes: {
    name: string
    namespace: string
    'created-at': string | null
  }
}

export default function ProvidersPage() {
  const router = useRouter()
  const t = useTranslations('registry')
  const [providers, setProviders] = useState<Provider[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [creating, setCreating] = useState(false)


  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadProviders()
  }, [router])

  usePollingInterval(!loading, 60_000, loadProviders)

  async function loadProviders() {
    try {
      const res = await apiFetch('/api/terrapod/v1/registry-providers')
      if (!res.ok) throw new Error(t('providers.loadFailed'))
      const data = await res.json()
      setProviders(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : t('providers.loadFailed'))
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    try {
      const res = await apiFetch('/api/terrapod/v1/registry-providers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'registry-providers',
            attributes: { name: newName },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('providers.createFailedStatus', { status: res.status }))
      }
      setNewName('')
      setShowCreate(false)
      await loadProviders()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('providers.createFailed'))
    } finally {
      setCreating(false)
    }
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={t('providers.title')}
          description={t('providers.description')}
          actions={
            <button
              onClick={() => setShowCreate(!showCreate)}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showCreate ? t('providers.cancel') : t('providers.createProvider')}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}

        {showCreate && (
          <form onSubmit={handleCreate} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
            <div>
              <label htmlFor="prov-name" className="block text-sm font-medium text-slate-300 mb-1">{t('providers.form.name')}</label>
              <input
                id="prov-name"
                type="text"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                required
                pattern="[a-z][a-z0-9-]*"
                title={t('providers.form.slugTitle')}
                placeholder="aws"
                className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              />
            </div>
            <button
              type="submit"
              disabled={creating}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
            >
              {creating ? t('providers.form.creating') : t('providers.form.create')}
            </button>
          </form>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : providers.length === 0 ? (
          <EmptyState message={t('providers.empty')} />
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {providers.map((prov) => (
              <Link
                key={prov.id}
                href={`/registry/providers/${prov.attributes.name}`}
                className="bg-slate-800/50 rounded-lg border border-slate-700/50 hover:border-brand-600/30 p-4 transition-colors"
              >
                <h3 className="font-semibold text-slate-200">{prov.attributes.name}</h3>
              </Link>
            ))}
          </div>
        )}
      </main>
    </>
  )
}
