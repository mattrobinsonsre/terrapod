'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
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

export default function BinaryCachePage() {
  const router = useRouter()
  const [entries, setEntries] = useState<CachedBinary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  // Warm form
  const [showWarm, setShowWarm] = useState(false)
  const [warmTool, setWarmTool] = useState('terraform')
  const [warmVersion, setWarmVersion] = useState('')
  const [warmOs, setWarmOs] = useState('linux')
  const [warmArch, setWarmArch] = useState('amd64')
  const [warming, setWarming] = useState(false)

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadEntries()
  }, [router])

  async function loadEntries() {
    setLoading(true)
    try {
      const res = await apiFetch('/api/v2/admin/binary-cache')
      if (!res.ok) throw new Error('Failed to load binary cache')
      const data = await res.json()
      setEntries(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load binary cache')
    } finally {
      setLoading(false)
    }
  }

  async function handleWarm(e: React.FormEvent) {
    e.preventDefault()
    setWarming(true)
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch('/api/v2/admin/binary-cache/warm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool: warmTool, version: warmVersion, os: warmOs, arch: warmArch }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Warm failed (${res.status})`)
      }
      setSuccess(`Cached ${warmTool} ${warmVersion} (${warmOs}/${warmArch})`)
      setWarmVersion('')
      setShowWarm(false)
      await loadEntries()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to warm binary')
    } finally {
      setWarming(false)
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
      await loadEntries()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to purge')
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
          title="Binary Cache"
          description="Terraform and tofu CLI binary cache management"
          actions={
            <button
              onClick={() => setShowWarm(!showWarm)}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showWarm ? 'Cancel' : 'Warm Cache'}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}
        {success && (
          <div className="mb-4 p-3 bg-green-900/30 text-green-400 rounded-lg text-sm border border-green-800/50">
            {success}
          </div>
        )}

        {showWarm && (
          <form onSubmit={handleWarm} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              <div>
                <label htmlFor="w-tool" className="block text-sm font-medium text-slate-300 mb-1">Tool</label>
                <select id="w-tool" value={warmTool} onChange={(e) => setWarmTool(e.target.value)}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                  <option value="terraform">terraform</option>
                  <option value="tofu">tofu</option>
                </select>
              </div>
              <div>
                <label htmlFor="w-ver" className="block text-sm font-medium text-slate-300 mb-1">Version</label>
                <input id="w-ver" type="text" value={warmVersion} onChange={(e) => setWarmVersion(e.target.value)} required placeholder="1.9.0"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
              <div>
                <label htmlFor="w-os" className="block text-sm font-medium text-slate-300 mb-1">OS</label>
                <input id="w-os" type="text" value={warmOs} onChange={(e) => setWarmOs(e.target.value)} required
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
              <div>
                <label htmlFor="w-arch" className="block text-sm font-medium text-slate-300 mb-1">Arch</label>
                <input id="w-arch" type="text" value={warmArch} onChange={(e) => setWarmArch(e.target.value)} required
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
            </div>
            <button type="submit" disabled={warming}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
              {warming ? 'Warming...' : 'Warm'}
            </button>
          </form>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : entries.length === 0 ? (
          <EmptyState message="No cached binaries yet." />
        ) : (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <th className="text-left px-4 py-3 text-slate-400 font-medium">Tool</th>
                  <th className="text-left px-4 py-3 text-slate-400 font-medium">Version</th>
                  <th className="text-left px-4 py-3 text-slate-400 font-medium">OS</th>
                  <th className="text-left px-4 py-3 text-slate-400 font-medium">Arch</th>
                  <th className="text-left px-4 py-3 text-slate-400 font-medium">Cached At</th>
                  <th className="text-right px-4 py-3 text-slate-400 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => (
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
      </main>
    </>
  )
}
