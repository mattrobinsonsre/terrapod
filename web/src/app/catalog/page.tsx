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

interface CatalogItem {
  id: string
  attributes: {
    name: string
    'display-name': string
    description: string
    enabled: boolean
    'module-id': string
    'module-name': string
    'module-provider': string
    'default-version-pin': string | null
    labels?: Record<string, string> | null
  }
}

export default function CatalogPage() {
  const t = useTranslations('catalog')
  const router = useRouter()
  const [items, setItems] = useState<CatalogItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  // True when the catalog feature flag is off (GET returns 404). We then show
  // an info banner rather than an error spew, matching the feature-gate UX.
  const [disabled, setDisabled] = useState(false)

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadItems()
  }, [router])

  usePollingInterval(!loading && !disabled, 60_000, loadItems)

  async function loadItems() {
    try {
      const res = await apiFetch('/api/terrapod/v1/catalog-items')
      if (res.status === 404) {
        setDisabled(true)
        setItems([])
        return
      }
      if (!res.ok) throw new Error(t('errors.loadItems'))
      const data = await res.json()
      setDisabled(false)
      setItems(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadItems'))
    } finally {
      setLoading(false)
    }
  }

  // Browse surface shows only enabled items (selection-first); disabled items
  // live in the admin catalog page.
  const enabledItems = items.filter((i) => i.attributes.enabled)

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={t('title')}
          description={t('description')}
        />

        {error && <ErrorBanner message={error} />}

        {loading ? (
          <LoadingSpinner />
        ) : disabled ? (
          <div className="p-4 bg-slate-800/50 text-slate-400 rounded-lg text-sm border border-slate-700/50">
            {t('notEnabled')}
          </div>
        ) : enabledItems.length === 0 ? (
          <EmptyState message={t('empty')} />
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {enabledItems.map((item) => {
              const a = item.attributes
              return (
                <Link
                  key={item.id}
                  href={`/catalog/${item.id}`}
                  className="bg-slate-800/50 rounded-lg border border-slate-700/50 hover:border-brand-600/30 p-4 transition-colors flex flex-col gap-2"
                >
                  <h3 className="font-semibold text-slate-200">
                    {a['display-name'] || a.name}
                  </h3>
                  {a.description && (
                    <p className="text-sm text-slate-400 line-clamp-3">{a.description}</p>
                  )}
                  <div className="flex flex-wrap items-center gap-2 mt-auto pt-1">
                    {(a['module-name'] || a['module-provider']) && (
                      <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-700 text-slate-300">
                        {a['module-name']}
                        {a['module-provider'] ? `/${a['module-provider']}` : ''}
                      </span>
                    )}
                    <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-brand-900/50 text-brand-300">
                      {a['default-version-pin']
                        ? t('card.pinned', { version: a['default-version-pin'] })
                        : t('card.latest')}
                    </span>
                  </div>
                </Link>
              )
            })}
          </div>
        )}
      </main>
    </>
  )
}
