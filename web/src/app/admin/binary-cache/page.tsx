'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { useSortable } from '@/lib/use-sortable'
import { usePollingInterval } from '@/lib/use-polling-interval'
import { getAuthState, isAdmin } from '@/lib/auth'
import { useConfirm } from '@/lib/use-confirm'
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
  const t = useTranslations('adminBinaryCache')
  const router = useRouter()
  const { confirmDelete, confirmTouchMutation } = useConfirm()
  const [entries, setEntries] = useState<CachedBinary[]>([])
  const [providerEntries, setProviderEntries] = useState<CachedProvider[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  // Multi-select state
  const [selectedBinaries, setSelectedBinaries] = useState<Set<string>>(new Set())
  const [selectedProviders, setSelectedProviders] = useState<Set<string>>(new Set())
  const [purging, setPurging] = useState(false)

  // Bulk-warm state
  const [showWarm, setShowWarm] = useState(false)
  const [warmBinariesText, setWarmBinariesText] = useState('')
  const [warmProvidersText, setWarmProvidersText] = useState('')
  const [warming, setWarming] = useState(false)
  const [warmResults, setWarmResults] = useState<
    { kind: string; ref: string; ok: boolean; error?: string }[] | null
  >(null)

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
    try {
      const [binaryRes, providerRes] = await Promise.all([
        apiFetch('/api/terrapod/v1/admin/binary-cache'),
        apiFetch('/api/terrapod/v1/admin/provider-cache'),
      ])
      if (!binaryRes.ok) throw new Error(t('errors.loadBinary'))
      const binaryData = await binaryRes.json()
      setEntries(binaryData.data || [])

      if (providerRes.ok) {
        const providerData = await providerRes.json()
        setProviderEntries(providerData.data || [])
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadCache'))
    } finally {
      setLoading(false)
    }
  }

  // Parse "os/arch os/arch ..." trailing tokens into platform objects.
  function parsePlatforms(tokens: string[]): { os: string; arch: string }[] {
    return tokens
      .map((t) => t.split('/'))
      .filter((p) => p.length === 2 && p[0] && p[1])
      .map((p) => ({ os: p[0], arch: p[1] }))
  }

  async function handleBulkWarm() {
    if (!confirmTouchMutation(t('warm.confirm'))) return
    setError('')
    setSuccess('')
    setWarmResults(null)

    // Each line: "<tool> <version> [os/arch ...]" / "<source> <version> [os/arch ...]".
    const binaries = warmBinariesText
      .split('\n')
      .map((l) => l.trim())
      .filter(Boolean)
      .map((l) => {
        const [tool, version, ...rest] = l.split(/\s+/)
        return { tool, version, platforms: parsePlatforms(rest) }
      })
    const providers = warmProvidersText
      .split('\n')
      .map((l) => l.trim())
      .filter(Boolean)
      .map((l) => {
        const [source, version, ...rest] = l.split(/\s+/)
        return { source, version, platforms: parsePlatforms(rest) }
      })

    if (binaries.length === 0 && providers.length === 0) {
      setError(t('warm.errorEmpty'))
      return
    }

    setWarming(true)
    try {
      const res = await apiFetch('/api/terrapod/v1/admin/binary-cache/warm-bulk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ binaries, providers }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        // FastAPI validation errors (422) return `detail` as an array of
        // {msg, loc, ...}; plain errors return a string. Surface either readably.
        const detail = data.detail
        const msg = Array.isArray(detail)
          ? detail.map((d) => d?.msg || JSON.stringify(d)).join('; ')
          : detail || t('warm.errorFailedStatus', { status: res.status })
        throw new Error(msg)
      }
      setWarmResults(data.results || [])
      setSuccess(t('warm.success', { succeeded: data.succeeded, total: data.total, failed: data.failed }))
      await loadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('warm.errorFailed'))
    } finally {
      setWarming(false)
    }
  }

  async function handlePurge(tool: string, version: string) {
    if (!confirmDelete(t('purge.confirmBinary', { tool, version }))) return
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/admin/binary-cache/${tool}/${version}`, {
        method: 'DELETE',
      })
      if (!res.ok) throw new Error(t('purge.errorStatus', { status: res.status }))
      const data = await res.json()
      setSuccess(t('purge.successBinary', { count: data.count || 0, tool, version }))
      await loadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('purge.error'))
    }
  }

  async function handleProviderPurge(hostname: string, namespace: string, type: string, version: string) {
    if (!confirmDelete(t('purge.confirmProvider', { provider: `${namespace}/${type}`, version }))) return
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/admin/provider-cache/${hostname}/${namespace}/${type}/${version}`, {
        method: 'DELETE',
      })
      if (!res.ok) throw new Error(t('purge.errorStatus', { status: res.status }))
      const data = await res.json()
      setSuccess(t('purge.successProvider', { count: data.count || 0, provider: `${namespace}/${type}`, version }))
      await loadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('purge.errorProvider'))
    }
  }

  function toggleBinarySelection(id: string) {
    setSelectedBinaries((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  function toggleAllBinaries() {
    if (selectedBinaries.size === entries.length) {
      setSelectedBinaries(new Set())
    } else {
      setSelectedBinaries(new Set(entries.map((e) => e.id)))
    }
  }

  function toggleProviderSelection(id: string) {
    setSelectedProviders((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  function toggleAllProviders() {
    if (selectedProviders.size === providerEntries.length) {
      setSelectedProviders(new Set())
    } else {
      setSelectedProviders(new Set(providerEntries.map((e) => e.id)))
    }
  }

  async function handleBatchPurgeBinaries() {
    if (!confirmDelete(t('batchPurge.confirmBinary'))) return
    setPurging(true)
    setError('')
    setSuccess('')
    try {
      // Deduplicate by tool+version (API purges all platforms for a tool+version)
      const keys = new Map<string, { tool: string; version: string }>()
      for (const entry of entries) {
        if (selectedBinaries.has(entry.id)) {
          const key = `${entry.attributes.tool}/${entry.attributes.version}`
          if (!keys.has(key)) keys.set(key, { tool: entry.attributes.tool, version: entry.attributes.version })
        }
      }
      let totalPurged = 0
      for (const { tool, version } of keys.values()) {
        const res = await apiFetch(`/api/terrapod/v1/admin/binary-cache/${tool}/${version}`, { method: 'DELETE' })
        if (!res.ok) throw new Error(t('batchPurge.errorBinaryItem', { tool, version }))
        const data = await res.json()
        totalPurged += data.count || 0
      }
      setSuccess(t('batchPurge.successBinary', { count: totalPurged }))
      setSelectedBinaries(new Set())
      await loadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('purge.error'))
    } finally {
      setPurging(false)
    }
  }

  async function handleBatchPurgeProviders() {
    if (!confirmDelete(t('batchPurge.confirmProvider'))) return
    setPurging(true)
    setError('')
    setSuccess('')
    try {
      // Deduplicate by hostname+namespace+type+version
      const keys = new Map<string, { hostname: string; namespace: string; type: string; version: string }>()
      for (const entry of providerEntries) {
        if (selectedProviders.has(entry.id)) {
          const key = `${entry.attributes.hostname}/${entry.attributes.namespace}/${entry.attributes['provider-type']}/${entry.attributes.version}`
          if (!keys.has(key)) keys.set(key, {
            hostname: entry.attributes.hostname,
            namespace: entry.attributes.namespace,
            type: entry.attributes['provider-type'],
            version: entry.attributes.version,
          })
        }
      }
      let totalPurged = 0
      for (const { hostname, namespace, type, version } of keys.values()) {
        const res = await apiFetch(`/api/terrapod/v1/admin/provider-cache/${hostname}/${namespace}/${type}/${version}`, { method: 'DELETE' })
        if (!res.ok) throw new Error(t('batchPurge.errorProviderItem', { provider: `${namespace}/${type}`, version }))
        const data = await res.json()
        totalPurged += data.count || 0
      }
      setSuccess(t('batchPurge.successProvider', { count: totalPurged }))
      setSelectedProviders(new Set())
      await loadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('purge.errorProviders'))
    } finally {
      setPurging(false)
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
          title={t('title')}
          description={t('description')}
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
            {/* Bulk warm (pre-population) */}
            <div className="mb-6 bg-slate-800/50 rounded-lg border border-slate-700/50">
              <button
                onClick={() => setShowWarm((v) => !v)}
                className="w-full flex items-center justify-between px-4 py-3 text-left text-slate-200 font-semibold"
                aria-expanded={showWarm}
              >
                <span>{t('warm.heading')}</span>
                <span className="text-slate-400 text-sm">{showWarm ? t('warm.hide') : t('warm.prePopulate')}</span>
              </button>
              {showWarm && (
                <div className="px-4 pb-4 space-y-4 border-t border-slate-700/50 pt-4">
                  <p className="text-sm text-slate-400">
                    {t.rich('warm.help', {
                      code: (chunks) => <code className="text-slate-300">{chunks}</code>,
                    })}
                  </p>
                  <div className="grid gap-4 md:grid-cols-2">
                    <div>
                      <label htmlFor="warm-binaries" className="block text-sm text-slate-300 mb-1">
                        {t('warm.binariesLabel')} — <span className="text-slate-500">tool version [os/arch …]</span>
                      </label>
                      <textarea
                        id="warm-binaries"
                        value={warmBinariesText}
                        onChange={(e) => setWarmBinariesText(e.target.value)}
                        rows={4}
                        spellCheck={false}
                        placeholder={'tofu 1.9.0\nterraform 1.12.0 linux/amd64 linux/arm64'}
                        className="w-full px-3 py-2 rounded-lg bg-slate-900/70 border border-slate-700 text-sm text-slate-200 font-mono"
                      />
                    </div>
                    <div>
                      <label htmlFor="warm-providers" className="block text-sm text-slate-300 mb-1">
                        {t('warm.providersLabel')} — <span className="text-slate-500">host/ns/type version [os/arch …]</span>
                      </label>
                      <textarea
                        id="warm-providers"
                        value={warmProvidersText}
                        onChange={(e) => setWarmProvidersText(e.target.value)}
                        rows={4}
                        spellCheck={false}
                        placeholder={'registry.terraform.io/hashicorp/aws 5.60.0'}
                        className="w-full px-3 py-2 rounded-lg bg-slate-900/70 border border-slate-700 text-sm text-slate-200 font-mono"
                      />
                    </div>
                  </div>
                  <button
                    onClick={handleBulkWarm}
                    disabled={warming}
                    className="px-4 py-2 rounded-lg text-sm font-medium bg-violet-600/30 hover:bg-violet-600/50 disabled:opacity-50 text-violet-200 transition-colors"
                  >
                    {warming ? t('warm.warming') : t('warm.warm')}
                  </button>
                  {warmResults && warmResults.length > 0 && (
                    <ul className="space-y-1 text-sm">
                      {warmResults.map((r, i) => (
                        <li key={i} className={r.ok ? 'text-green-400' : 'text-red-400'}>
                          {r.ok ? '✓' : '✗'} <span className="text-slate-300">{r.ref}</span>
                          {!r.ok && r.error ? ` — ${r.error}` : ''}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </div>

            {/* Binary Cache Section */}
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-lg font-semibold text-slate-200">{t('sections.cliBinaries')}</h2>
              {selectedBinaries.size > 0 && (
                <button
                  onClick={handleBatchPurgeBinaries}
                  disabled={purging}
                  className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600/20 hover:bg-red-600/40 disabled:opacity-50 text-red-400 transition-colors"
                >
                  {purging ? t('batchPurge.purging') : t('batchPurge.purgeSelected', { count: selectedBinaries.size })}
                </button>
              )}
            </div>
            {entries.length === 0 ? (
              <EmptyState message={t('empty.binaries')} />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-x-auto mb-8">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="px-4 py-3 w-8">
                        <input type="checkbox" checked={selectedBinaries.size === entries.length && entries.length > 0} onChange={toggleAllBinaries}
                          className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                      </th>
                      <SortableHeader label={t('columns.tool')} sortKey="tool" sortState={binarySortState} onSort={toggleBinarySort} />
                      <SortableHeader label={t('columns.version')} sortKey="version" sortState={binarySortState} onSort={toggleBinarySort} />
                      <SortableHeader label={t('columns.os')} sortKey="os" sortState={binarySortState} onSort={toggleBinarySort} />
                      <SortableHeader label={t('columns.arch')} sortKey="arch" sortState={binarySortState} onSort={toggleBinarySort} />
                      <SortableHeader label={t('columns.cachedAt')} sortKey="cached-at" sortState={binarySortState} onSort={toggleBinarySort} />
                      <th className="text-right px-4 py-3 text-slate-400 font-medium">{t('columns.actions')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedEntries.map((entry) => (
                      <tr key={entry.id} className={`border-b border-slate-700/30 last:border-0 ${selectedBinaries.has(entry.id) ? 'bg-brand-900/10' : ''}`}>
                        <td className="px-4 py-3">
                          <input type="checkbox" checked={selectedBinaries.has(entry.id)} onChange={() => toggleBinarySelection(entry.id)}
                            className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                        </td>
                        <td className="px-4 py-3 text-slate-200 font-mono">{entry.attributes.tool}</td>
                        <td className="px-4 py-3 text-slate-200 font-mono">{entry.attributes.version}</td>
                        <td className="px-4 py-3 text-slate-400">{entry.attributes.os}</td>
                        <td className="px-4 py-3 text-slate-400">{entry.attributes.arch}</td>
                        <td className="px-4 py-3 text-slate-400 text-xs">{formatDate(entry.attributes['cached-at'])}</td>
                        <td className="px-4 py-3 text-right">
                          <button
                            onClick={() => handlePurge(entry.attributes.tool, entry.attributes.version)}
                            className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors"
                          >
                            {t('purge.button')}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Provider Cache Section */}
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-lg font-semibold text-slate-200">{t('sections.providerBinaries')}</h2>
              {selectedProviders.size > 0 && (
                <button
                  onClick={handleBatchPurgeProviders}
                  disabled={purging}
                  className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600/20 hover:bg-red-600/40 disabled:opacity-50 text-red-400 transition-colors"
                >
                  {purging ? t('batchPurge.purging') : t('batchPurge.purgeSelected', { count: selectedProviders.size })}
                </button>
              )}
            </div>
            {providerEntries.length === 0 ? (
              <EmptyState message={t('empty.providers')} />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="px-4 py-3 w-8">
                        <input type="checkbox" checked={selectedProviders.size === providerEntries.length && providerEntries.length > 0} onChange={toggleAllProviders}
                          className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                      </th>
                      <SortableHeader label={t('columns.provider')} sortKey="provider" sortState={providerSortState} onSort={toggleProviderSort} />
                      <SortableHeader label={t('columns.version')} sortKey="version" sortState={providerSortState} onSort={toggleProviderSort} />
                      <SortableHeader label={t('columns.hostname')} sortKey="hostname" sortState={providerSortState} onSort={toggleProviderSort} />
                      <SortableHeader label={t('columns.os')} sortKey="os" sortState={providerSortState} onSort={toggleProviderSort} />
                      <SortableHeader label={t('columns.arch')} sortKey="arch" sortState={providerSortState} onSort={toggleProviderSort} />
                      <SortableHeader label={t('columns.cachedAt')} sortKey="cached-at" sortState={providerSortState} onSort={toggleProviderSort} />
                      <th className="text-right px-4 py-3 text-slate-400 font-medium">{t('columns.actions')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedProviders.map((entry) => (
                      <tr key={entry.id} className={`border-b border-slate-700/30 last:border-0 ${selectedProviders.has(entry.id) ? 'bg-brand-900/10' : ''}`}>
                        <td className="px-4 py-3">
                          <input type="checkbox" checked={selectedProviders.has(entry.id)} onChange={() => toggleProviderSelection(entry.id)}
                            className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                        </td>
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
                            className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors"
                          >
                            {t('purge.button')}
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
