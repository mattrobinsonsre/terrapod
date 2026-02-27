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
import { getAuthState } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

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
  const params = useParams<{ org: string; namespace: string; name: string }>()
  const { org, namespace, name } = params

  const [versions, setVersions] = useState<Version[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [expandedVersion, setExpandedVersion] = useState<string | null>(null)

  // Create version form
  const [showCreateVersion, setShowCreateVersion] = useState(false)
  const [newVersion, setNewVersion] = useState('')
  const [newKeyId, setNewKeyId] = useState('')
  const [newProtocols, setNewProtocols] = useState('5.0')
  const [creatingVersion, setCreatingVersion] = useState(false)
  const [uploadLinks, setUploadLinks] = useState<{ shasums: string; sig: string } | null>(null)

  // Create platform form
  const [platformForVersion, setPlatformForVersion] = useState<string | null>(null)
  const [newOs, setNewOs] = useState('linux')
  const [newArch, setNewArch] = useState('amd64')
  const [newFilename, setNewFilename] = useState('')
  const [creatingPlatform, setCreatingPlatform] = useState(false)
  const [platformUploadUrl, setPlatformUploadUrl] = useState('')

  // Delete confirmation
  const [deleteTarget, setDeleteTarget] = useState<{ type: string; version?: string; os?: string; arch?: string } | null>(null)

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadVersions()
  }, [router, org, namespace, name])

  const basePath = `/api/v2/organizations/${org}/registry-providers/private/${namespace}/${name}`

  async function loadVersions() {
    setLoading(true)
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

  async function handleCreateVersion(e: React.FormEvent) {
    e.preventDefault()
    setCreatingVersion(true)
    setError('')
    setUploadLinks(null)
    try {
      const protocols = newProtocols.split(',').map(p => p.trim()).filter(Boolean)
      const res = await apiFetch(`${basePath}/versions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'registry-provider-versions',
            attributes: { version: newVersion, key_id: newKeyId, protocols },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to create version (${res.status})`)
      }
      const data = await res.json()
      const links = data.data?.links || {}
      setUploadLinks({
        shasums: links['shasums-upload'] || '',
        sig: links['shasums-sig-upload'] || '',
      })
      setNewVersion('')
      setNewKeyId('')
      setShowCreateVersion(false)
      await loadVersions()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create version')
    } finally {
      setCreatingVersion(false)
    }
  }

  async function handleCreatePlatform(e: React.FormEvent) {
    e.preventDefault()
    if (!platformForVersion) return
    setCreatingPlatform(true)
    setError('')
    setPlatformUploadUrl('')
    try {
      const res = await apiFetch(`${basePath}/versions/${platformForVersion}/platforms`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'registry-provider-platforms',
            attributes: { os: newOs, arch: newArch, filename: newFilename },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to create platform (${res.status})`)
      }
      const data = await res.json()
      setPlatformUploadUrl(data.data?.links?.['provider-binary-upload'] || '')
      setNewFilename('')
      setPlatformForVersion(null)
      await loadVersions()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create platform')
    } finally {
      setCreatingPlatform(false)
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

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        {error && <ErrorBanner message={error} />}

        <PageHeader
          title={`${namespace}/${name}`}
          description="Provider registry"
          actions={
            <div className="flex gap-2">
              <button
                onClick={() => setShowCreateVersion(!showCreateVersion)}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors"
              >
                {showCreateVersion ? 'Cancel' : 'Add Version'}
              </button>
              <button
                onClick={() => setDeleteTarget({ type: 'provider' })}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-red-900/50 hover:bg-red-800/50 text-red-300 border border-red-800/50 transition-colors"
              >
                Delete Provider
              </button>
            </div>
          }
        />

        {uploadLinks && (
          <div className="mb-6 p-4 bg-green-900/30 rounded-lg border border-green-800/50 space-y-2">
            <p className="text-sm text-green-300 font-medium">Upload SHA256SUMS files:</p>
            {[{ label: 'SHA256SUMS', url: uploadLinks.shasums }, { label: 'SHA256SUMS.sig', url: uploadLinks.sig }].map((item) => (
              <div key={item.label} className="flex items-center gap-2">
                <span className="text-xs text-green-400 w-32 flex-shrink-0">{item.label}:</span>
                <code className="flex-1 text-xs text-green-200 bg-green-900/30 p-1.5 rounded overflow-x-auto">{item.url}</code>
                <button
                  onClick={() => navigator.clipboard.writeText(item.url)}
                  className="px-2 py-1 rounded text-xs font-medium bg-green-800/50 hover:bg-green-700/50 text-green-200 transition-colors flex-shrink-0"
                >
                  Copy
                </button>
              </div>
            ))}
          </div>
        )}

        {platformUploadUrl && (
          <div className="mb-6 p-4 bg-green-900/30 rounded-lg border border-green-800/50">
            <p className="text-sm text-green-300 font-medium mb-2">Upload provider binary to:</p>
            <div className="flex items-center gap-2">
              <code className="flex-1 text-xs text-green-200 bg-green-900/30 p-2 rounded overflow-x-auto">{platformUploadUrl}</code>
              <button
                onClick={() => navigator.clipboard.writeText(platformUploadUrl)}
                className="px-3 py-1 rounded text-xs font-medium bg-green-800/50 hover:bg-green-700/50 text-green-200 transition-colors flex-shrink-0"
              >
                Copy
              </button>
            </div>
          </div>
        )}

        {showCreateVersion && (
          <form onSubmit={handleCreateVersion} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div>
                <label htmlFor="pv-ver" className="block text-sm font-medium text-slate-300 mb-1">Version</label>
                <input id="pv-ver" type="text" value={newVersion} onChange={(e) => setNewVersion(e.target.value)} required placeholder="1.0.0"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
              <div>
                <label htmlFor="pv-key" className="block text-sm font-medium text-slate-300 mb-1">GPG Key ID (optional)</label>
                <input id="pv-key" type="text" value={newKeyId} onChange={(e) => setNewKeyId(e.target.value)} placeholder="A1B2C3D4"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
              <div>
                <label htmlFor="pv-proto" className="block text-sm font-medium text-slate-300 mb-1">Protocols</label>
                <input id="pv-proto" type="text" value={newProtocols} onChange={(e) => setNewProtocols(e.target.value)} placeholder="5.0"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
            </div>
            <button type="submit" disabled={creatingVersion}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
              {creatingVersion ? 'Creating...' : 'Create Version'}
            </button>
          </form>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : versions.length === 0 ? (
          <EmptyState message="No versions yet. Add one to get started." />
        ) : (
          <div className="space-y-3">
            {versions.map((v) => {
              const isExpanded = expandedVersion === v.attributes.version
              return (
                <div key={v.id} className="bg-slate-800/50 rounded-lg border border-slate-700/50">
                  <button
                    onClick={() => setExpandedVersion(isExpanded ? null : v.attributes.version)}
                    className="w-full flex items-center justify-between px-4 py-3 text-left"
                  >
                    <div className="flex items-center gap-3">
                      {isExpanded ? <ChevronDown size={16} className="text-slate-500" /> : <ChevronRight size={16} className="text-slate-500" />}
                      <span className="font-mono text-slate-200">{v.attributes.version}</span>
                      <span className="text-xs text-slate-500">{v.attributes.platforms?.length || 0} platform(s)</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={(e) => { e.stopPropagation(); setPlatformForVersion(v.attributes.version) }}
                        className="text-xs text-brand-400 hover:text-brand-300 transition-colors"
                      >
                        Add Platform
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); setDeleteTarget({ type: 'version', version: v.attributes.version }) }}
                        className="text-xs text-red-400 hover:text-red-300 transition-colors"
                      >
                        Delete
                      </button>
                    </div>
                  </button>
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
                              <td className="py-1.5 text-right">
                                <button
                                  onClick={() => setDeleteTarget({ type: 'platform', version: v.attributes.version, os: p.os, arch: p.arch })}
                                  className="text-xs text-red-400 hover:text-red-300 transition-colors"
                                >
                                  Delete
                                </button>
                              </td>
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

        {/* Add platform form dialog */}
        <Dialog.Root open={platformForVersion !== null} onOpenChange={(open) => { if (!open) setPlatformForVersion(null) }}>
          <Dialog.Portal>
            <Dialog.Overlay className="fixed inset-0 bg-black/60" />
            <Dialog.Content className="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 bg-slate-800 rounded-lg border border-slate-700 p-6 w-full max-w-md shadow-xl">
              <Dialog.Title className="text-lg font-semibold text-slate-100">
                Add Platform — {platformForVersion}
              </Dialog.Title>
              <form onSubmit={handleCreatePlatform} className="mt-4 space-y-3">
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="plt-os" className="block text-sm font-medium text-slate-300 mb-1">OS</label>
                    <input id="plt-os" type="text" value={newOs} onChange={(e) => setNewOs(e.target.value)} required
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="plt-arch" className="block text-sm font-medium text-slate-300 mb-1">Arch</label>
                    <input id="plt-arch" type="text" value={newArch} onChange={(e) => setNewArch(e.target.value)} required
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                </div>
                <div>
                  <label htmlFor="plt-file" className="block text-sm font-medium text-slate-300 mb-1">Filename</label>
                  <input id="plt-file" type="text" value={newFilename} onChange={(e) => setNewFilename(e.target.value)} required
                    placeholder="terraform-provider-aws_1.0.0_linux_amd64.zip"
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                </div>
                <div className="flex justify-end gap-3 mt-4">
                  <button type="button" onClick={() => setPlatformForVersion(null)}
                    className="px-4 py-2 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-700 transition-colors">
                    Cancel
                  </button>
                  <button type="submit" disabled={creatingPlatform}
                    className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                    {creatingPlatform ? 'Creating...' : 'Create Platform'}
                  </button>
                </div>
              </form>
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>

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
