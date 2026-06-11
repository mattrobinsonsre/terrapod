'use client'

import { useEffect, useState } from 'react'
import { useParams, useRouter } from 'next/navigation'
import { ChevronDown, ChevronRight } from 'lucide-react'
import * as Dialog from '@radix-ui/react-dialog'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { LabelsEditor } from '@/components/labels-editor'
import { usePollingInterval } from '@/lib/use-polling-interval'

interface ProviderPermissions {
  'can-update': boolean
  'can-destroy': boolean
  'can-create-version': boolean
}

interface ProviderMeta {
  id: string
  attributes: {
    name: string
    namespace: string
    labels: Record<string, string>
    'owner-email': string
    'created-at': string | null
    'updated-at': string | null
    permissions: ProviderPermissions
  }
}

interface Platform {
  os: string
  arch: string
  filename: string
}

interface Version {
  id: string
  attributes: {
    version: string
    protocols: string[]
    'shasums-uploaded': boolean
    'shasums-sig-uploaded': boolean
    platforms: Platform[]
    'created-at': string | null
  }
}

export default function ProviderDetailPage() {
  const router = useRouter()
  const params = useParams<{ name: string }>()
  const { name } = params

  const [versions, setVersions] = useState<Version[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [expandedVersion, setExpandedVersion] = useState<string | null>(null)

  // Provider metadata
  const [providerMeta, setProviderMeta] = useState<ProviderMeta | null>(null)
  const [editingMeta, setEditingMeta] = useState(false)
  const [editLabels, setEditLabels] = useState<Record<string, string>>({})
  const [editOwner, setEditOwner] = useState('')
  const [savingMeta, setSavingMeta] = useState(false)

  // Delete confirmation
  const [deleteTarget, setDeleteTarget] = useState<{ type: string; version?: string; os?: string; arch?: string } | null>(null)

  // Label lockout warning
  const [lockoutWarning, setLockoutWarning] = useState('')

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadVersions()
    loadProviderMeta()
  }, [router, name])

  usePollingInterval(!loading, 60_000, loadVersions)

  const basePath = `/api/terrapod/v1/registry-providers/private/default/${name}`

  async function loadVersions() {
    try {
      const res = await apiFetch(`${basePath}/versions`)
      if (!res.ok) throw new Error('Failed to load versions')
      const data = await res.json()
      setVersions(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load versions')
    } finally {
      setLoading(false)
    }
  }

  async function loadProviderMeta() {
    try {
      const res = await apiFetch(basePath)
      if (res.ok) {
        const data = await res.json()
        setProviderMeta(data.data)
      }
    } catch {}
  }

  function startEditingMeta() {
    if (!providerMeta) return
    setEditLabels(providerMeta.attributes.labels || {})
    setEditOwner(providerMeta.attributes['owner-email'] || '')
    setEditingMeta(true)
  }

  async function handleSaveMeta(force = false) {
    setSavingMeta(true)
    setError('')
    setLockoutWarning('')
    try {
      const res = await apiFetch(basePath, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'registry-providers', attributes: { labels: editLabels, ...(isAdmin() ? { 'owner-email': editOwner } : {}), ...(force ? { force: true } : {}) } },
        }),
      })
      if (res.status === 409) {
        const errData = await res.json()
        const detail = errData.errors?.[0]?.detail || 'This label change would reduce your access.'
        setLockoutWarning(detail)
        return
      }
      if (!res.ok) throw new Error('Failed to update provider')
      const data = await res.json()
      setProviderMeta(data.data)
      setEditingMeta(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update provider')
    } finally {
      setSavingMeta(false)
    }
  }

  async function handleDelete() {
    if (!deleteTarget) return
    setError('')
    try {
      let path: string
      if (deleteTarget.type === 'provider') {
        path = basePath
      } else if (deleteTarget.type === 'version') {
        path = `${basePath}/versions/${deleteTarget.version}`
      } else {
        path = `${basePath}/versions/${deleteTarget.version}/platforms/${deleteTarget.os}/${deleteTarget.arch}`
      }

      const res = await apiFetch(path, { method: 'DELETE' })
      if (!res.ok && res.status !== 204) throw new Error(`Delete failed (${res.status})`)

      setDeleteTarget(null)
      if (deleteTarget.type === 'provider') {
        router.push('/registry/providers')
      } else {
        await loadVersions()
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Delete failed')
      setDeleteTarget(null)
    }
  }

  const provPerms = providerMeta?.attributes.permissions || {} as ProviderPermissions

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        {error && <ErrorBanner message={error} />}

        <PageHeader
          title={name}
          description="Provider registry"
          actions={
            <div className="flex gap-2">
              {provPerms['can-destroy'] && (
                <button
                  onClick={() => setDeleteTarget({ type: 'provider' })}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-red-900/50 hover:bg-red-800/50 text-red-300 border border-red-800/50 transition-colors"
                >
                  Delete Provider
                </button>
              )}
            </div>
          }
        />

        {/* Publishing note: provider versions are now published via the CLI */}
        <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6">
          <p className="text-sm text-slate-400">
            Provider versions are published with the{' '}
            <code className="text-xs bg-slate-700 px-1.5 py-0.5 rounded text-slate-300">terrapod-publish</code>{' '}
            CLI, which client-signs the binaries. Uploading and signing provider binaries from the
            browser is no longer supported. This page is read-only for versions and platforms.
          </p>
        </div>

        {/* Metadata: Owner & Labels */}
        {providerMeta && (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-5 mb-6">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-medium text-slate-300">Metadata</h3>
              {!editingMeta ? (
                provPerms['can-update'] && <button onClick={startEditingMeta} className="text-xs text-brand-400 hover:text-brand-300">Edit</button>
              ) : (
                <div className="flex gap-2">
                  <button onClick={() => { setEditingMeta(false); setLockoutWarning('') }} className="text-xs text-slate-400 hover:text-slate-200">Cancel</button>
                  <button onClick={() => handleSaveMeta()} disabled={savingMeta} className="text-xs text-brand-400 hover:text-brand-300">{savingMeta ? 'Saving...' : 'Save'}</button>
                </div>
              )}
            </div>
            <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <dt className="text-xs text-slate-500">Owner</dt>
                {editingMeta && isAdmin() ? (
                  <input type="email" value={editOwner} onChange={(e) => setEditOwner(e.target.value)} placeholder="user@example.com" className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                ) : (
                  <dd className="mt-1 text-sm text-slate-200">{providerMeta.attributes['owner-email'] || 'None'}</dd>
                )}
              </div>
              <div>
                <dt className="text-xs text-slate-500 mb-1">Labels</dt>
                {editingMeta ? (
                  <LabelsEditor labels={editLabels} onChange={setEditLabels} />
                ) : (
                  <dd className="mt-1"><LabelsEditor labels={providerMeta.attributes.labels || {}} readOnly /></dd>
                )}
              </div>
            </dl>
            {lockoutWarning && (
              <div className="mt-4 p-3 bg-amber-900/30 border border-amber-700/50 rounded-lg">
                <p className="text-sm text-amber-300 mb-2">{lockoutWarning}</p>
                <div className="flex gap-2">
                  <button
                    onClick={() => { setLockoutWarning(''); setEditLabels(providerMeta.attributes.labels || {}); }}
                    className="px-3 py-1 rounded text-xs text-slate-300 hover:text-white bg-slate-700 hover:bg-slate-600"
                  >
                    Revert Labels
                  </button>
                  <button
                    onClick={() => handleSaveMeta(true)}
                    disabled={savingMeta}
                    className="px-3 py-1 rounded text-xs text-amber-200 hover:text-white bg-amber-700 hover:bg-amber-600"
                  >
                    {savingMeta ? 'Saving...' : 'Save Anyway'}
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : versions.length === 0 ? (
          <EmptyState message="No versions yet. Publish with the terrapod-publish CLI to get started." />
        ) : (
          <div className="space-y-3">
            {versions.map((v) => {
              const isExpanded = expandedVersion === v.attributes.version
              return (
                <div key={v.id} className="bg-slate-800/50 rounded-lg border border-slate-700/50">
                  <div
                    role="button"
                    tabIndex={0}
                    onClick={() => setExpandedVersion(isExpanded ? null : v.attributes.version)}
                    onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpandedVersion(isExpanded ? null : v.attributes.version) } }}
                    className="w-full flex items-center justify-between px-4 py-3 text-left cursor-pointer"
                  >
                    <div className="flex items-center gap-3">
                      {isExpanded ? <ChevronDown size={16} className="text-slate-500" /> : <ChevronRight size={16} className="text-slate-500" />}
                      <span className="font-mono text-slate-200">{v.attributes.version}</span>
                      <span className="text-xs text-slate-500">{v.attributes.platforms?.length || 0} platform(s)</span>
                      {v.attributes['shasums-uploaded'] && (
                        <span className="text-xs px-1.5 py-0.5 rounded bg-slate-700 text-slate-400">SHASUMS</span>
                      )}
                      {v.attributes['shasums-sig-uploaded'] && (
                        <span className="text-xs px-1.5 py-0.5 rounded bg-slate-700 text-slate-400">SHASUMS.sig</span>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      {provPerms['can-destroy'] && (
                        <button
                          onClick={(e) => { e.stopPropagation(); setDeleteTarget({ type: 'version', version: v.attributes.version }) }}
                          className="text-xs text-red-400 hover:text-red-300 transition-colors"
                        >
                          Delete
                        </button>
                      )}
                    </div>
                  </div>
                  {isExpanded && v.attributes.platforms && v.attributes.platforms.length > 0 && (
                    <div className="border-t border-slate-700/30 px-4 py-2">
                      <table className="w-full text-sm">
                        <thead>
                          <tr className="text-slate-500 text-xs">
                            <th className="text-left py-1 font-medium">OS</th>
                            <th className="text-left py-1 font-medium">Arch</th>
                            <th className="text-left py-1 font-medium">Filename</th>
                            <th className="text-right py-1 font-medium">Actions</th>
                          </tr>
                        </thead>
                        <tbody>
                          {v.attributes.platforms.map((p) => (
                            <tr key={`${p.os}-${p.arch}`} className="border-t border-slate-700/20">
                              <td className="py-1.5 text-slate-300">{p.os}</td>
                              <td className="py-1.5 text-slate-300">{p.arch}</td>
                              <td className="py-1.5 text-slate-400 font-mono text-xs">{p.filename}</td>
                              {provPerms['can-destroy'] && (
                                <td className="py-1.5 text-right">
                                  <button
                                    onClick={() => setDeleteTarget({ type: 'platform', version: v.attributes.version, os: p.os, arch: p.arch })}
                                    className="text-xs text-red-400 hover:text-red-300 transition-colors"
                                  >
                                    Delete
                                  </button>
                                </td>
                              )}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}

        {/* Delete confirmation dialog */}
        <Dialog.Root open={deleteTarget !== null} onOpenChange={(open) => { if (!open) setDeleteTarget(null) }}>
          <Dialog.Portal>
            <Dialog.Overlay className="fixed inset-0 bg-black/60" />
            <Dialog.Content className="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 bg-slate-800 rounded-lg border border-slate-700 p-6 w-full max-w-md shadow-xl">
              <Dialog.Title className="text-lg font-semibold text-slate-100">
                Confirm Delete
              </Dialog.Title>
              <Dialog.Description className="text-sm text-slate-400 mt-2">
                {deleteTarget?.type === 'provider' && 'This will permanently delete this provider and all its versions.'}
                {deleteTarget?.type === 'version' && `This will permanently delete version ${deleteTarget.version} and its platforms.`}
                {deleteTarget?.type === 'platform' && `This will permanently delete the ${deleteTarget.os}/${deleteTarget.arch} platform.`}
              </Dialog.Description>
              <div className="flex justify-end gap-3 mt-6">
                <button onClick={() => setDeleteTarget(null)}
                  className="px-4 py-2 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-700 transition-colors">
                  Cancel
                </button>
                <button onClick={handleDelete}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-500 text-white transition-colors">
                  Delete
                </button>
              </div>
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
      </main>
    </>
  )
}
