'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { useSortable } from '@/lib/use-sortable'
import { usePollingInterval } from '@/lib/use-polling-interval'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

interface CachedBinary {
  id: string
  attributes: {
    tool: string
    version: string
    os: string
    arch: string
    shasum: string
    'download-url': string
    'cached-at': string | null
  }
}

interface CachedProvider {
  id: string
  attributes: {
    hostname: string
    namespace: string
    'provider-type': string
    version: string
    os: string
    arch: string
    shasum: string
    'cached-at': string | null
  }
}

export default function CachePage() {
  const router = useRouter()
  const [entries, setEntries] = useState<CachedBinary[]>([])
  const [providerEntries, setProviderEntries] = useState<CachedProvider[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  type BinarySortKey = 'tool' | 'version' | 'os' | 'arch' | 'cached-at'
  const binaryAccessor = useCallback((item: CachedBinary, key: BinarySortKey) => {
    switch (key) {
      case 'tool': return item.attributes.tool
      case 'version': return item.attributes.version
      case 'os': return item.attributes.os
      case 'arch': return item.attributes.arch
      case 'cached-at': return item.attributes['cached-at']
    }
  }, [])
  const { sortedItems: sortedEntries, sortState: binarySortState, toggleSort: toggleBinarySort } = useSortable<CachedBinary, BinarySortKey>(
    entries, 'cached-at', 'desc', binaryAccessor,
  )

  type ProviderSortKey = 'provider' | 'version' | 'hostname' | 'os' | 'arch' | 'cached-at'
  const providerAccessor = useCallback((item: CachedProvider, key: ProviderSortKey) => {
    switch (key) {
      case 'provider': return `${item.attributes.namespace}/${item.attributes['provider-type']}`
      case 'version': return item.attributes.version
      case 'hostname': return item.attributes.hostname
      case 'os': return item.attributes.os
      case 'arch': return item.attributes.arch
      case 'cached-at': return item.attributes['cached-at']
    }
  }, [])
  const { sortedItems: sortedProviders, sortState: providerSortState, toggleSort: toggleProviderSort } = useSortable<CachedProvider, ProviderSortKey>(
    providerEntries, 'cached-at', 'desc', providerAccessor,
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadAll()
  }, [router])

  usePollingInterval(!loading, 60_000, loadAll)

  async function loadAll() {
    setLoading(true)
    try {
      const [binaryRes, providerRes] = await Promise.all([
        apiFetch('/api/v2/admin/binary-cache'),
        apiFetch('/api/v2/admin/provider-cache'),
      ])
      if (!binaryRes.ok) throw new Error('Failed to load binary cache')
      const binaryData = await binaryRes.json()
      setEntries(binaryData.data || [])

      if (providerRes.ok) {
        const providerData = await providerRes.json()
        setProviderEntries(providerData.data || [])
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load cache')
    } finally {
      setLoading(false)
    }
  }

  async function handlePurge(tool: string, version: string) {
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/v2/admin/binary-cache/${tool}/${version}`, {
        method: 'DELETE',
      })
      if (!res.ok) throw new Error(`Purge failed (${res.status})`)
      const data = await res.json()
      setSuccess(`Purged ${data.count || 0} entries for ${tool} ${version}`)
      await loadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to purge')
    }
  }

  async function handleProviderPurge(hostname: string, namespace: string, type: string, version: string) {
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/v2/admin/provider-cache/${hostname}/${namespace}/${type}/${version}`, {
        method: 'DELETE',
      })
      if (!res.ok) throw new Error(`Purge failed (${res.status})`)
      const data = await res.json()
      setSuccess(`Purged ${data.count || 0} entries for ${namespace}/${type} ${version}`)
      await loadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to purge provider')
    }
  }

  function formatDate(iso: string | null): string {
    if (!iso) return '-'
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
          title="Cache"
          description="CLI binary and provider cache management"
        />

        {error && <ErrorBanner message={error} />}
        {success && (
          <div className="mb-4 p-3 bg-green-900/30 text-green-400 rounded-lg text-sm border border-green-800/50">
            {success}
          </div>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : (
          <>
            {/* Binary Cache Section */}
            <h2 className="text-lg font-semibold text-slate-200 mb-3">CLI Binaries</h2>
            {entries.length === 0 ? (
              <EmptyState message="No cached CLI binaries yet." />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden mb-8">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <SortableHeader label="Tool" sortKey="tool" sortState={binarySortState} onSort={toggleBinarySort} />
                      <SortableHeader label="Version" sortKey="version" sortState={binarySortState} onSort={toggleBinarySort} />
                      <SortableHeader label="OS" sortKey="os" sortState={binarySortState} onSort={toggleBinarySort} />
                      <SortableHeader label="Arch" sortKey="arch" sortState={binarySortState} onSort={toggleBinarySort} />
                      <SortableHeader label="Cached At" sortKey="cached-at" sortState={binarySortState} onSort={toggleBinarySort} />
                      <th className="text-right px-4 py-3 text-slate-400 font-medium">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedEntries.map((entry) => (
                      <tr key={entry.id} className="border-b border-slate-700/30 last:border-0">
                        <td className="px-4 py-3 text-slate-200 font-mono">{entry.attributes.tool}</td>
                        <td className="px-4 py-3 text-slate-200 font-mono">{entry.attributes.version}</td>
                        <td className="px-4 py-3 text-slate-400">{entry.attributes.os}</td>
                        <td className="px-4 py-3 text-slate-400">{entry.attributes.arch}</td>
                        <td className="px-4 py-3 text-slate-400 text-xs">{formatDate(entry.attributes['cached-at'])}</td>
                        <td className="px-4 py-3 text-right">
                          <button
                            onClick={() => handlePurge(entry.attributes.tool, entry.attributes.version)}
                            className="text-xs text-red-400 hover:text-red-300 transition-colors"
                          >
                            Purge
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Provider Cache Section */}
            <h2 className="text-lg font-semibold text-slate-200 mb-3">Provider Binaries</h2>
            {providerEntries.length === 0 ? (
              <EmptyState message="No cached provider binaries yet." />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <SortableHeader label="Provider" sortKey="provider" sortState={providerSortState} onSort={toggleProviderSort} />
                      <SortableHeader label="Version" sortKey="version" sortState={providerSortState} onSort={toggleProviderSort} />
                      <SortableHeader label="Hostname" sortKey="hostname" sortState={providerSortState} onSort={toggleProviderSort} />
                      <SortableHeader label="OS" sortKey="os" sortState={providerSortState} onSort={toggleProviderSort} />
                      <SortableHeader label="Arch" sortKey="arch" sortState={providerSortState} onSort={toggleProviderSort} />
                      <SortableHeader label="Cached At" sortKey="cached-at" sortState={providerSortState} onSort={toggleProviderSort} />
                      <th className="text-right px-4 py-3 text-slate-400 font-medium">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedProviders.map((entry) => (
                      <tr key={entry.id} className="border-b border-slate-700/30 last:border-0">
                        <td className="px-4 py-3 text-slate-200 font-mono">
                          {entry.attributes.namespace}/{entry.attributes['provider-type']}
                        </td>
                        <td className="px-4 py-3 text-slate-200 font-mono">{entry.attributes.version}</td>
                        <td className="px-4 py-3 text-slate-400 text-xs">{entry.attributes.hostname}</td>
                        <td className="px-4 py-3 text-slate-400">{entry.attributes.os}</td>
                        <td className="px-4 py-3 text-slate-400">{entry.attributes.arch}</td>
                        <td className="px-4 py-3 text-slate-400 text-xs">{formatDate(entry.attributes['cached-at'])}</td>
                        <td className="px-4 py-3 text-right">
                          <button
                            onClick={() => handleProviderPurge(
                              entry.attributes.hostname,
                              entry.attributes.namespace,
                              entry.attributes['provider-type'],
                              entry.attributes.version,
                            )}
                            className="text-xs text-red-400 hover:text-red-300 transition-colors"
                          >
                            Purge
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </main>
    </>
  )
}
