'use client'

import { useEffect, useState, useCallback } from 'react'
import { useParams, useRouter } from 'next/navigation'
import { ChevronDown, ChevronRight, Upload, FolderOpen } from 'lucide-react'
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

interface DetectedFile {
  file: File
  name: string
  version: string
  os: string
  arch: string
}

function parseProviderFilename(filename: string): { name: string; version: string; os: string; arch: string } | null {
  const match = filename.match(
    /^terraform-provider-(.+?)_(\d+\.\d+\.\d+(?:-.+?)?)_([a-z]+)_([a-z0-9]+)\.zip$/
  )
  if (!match) return null
  return { name: match[1], version: match[2], os: match[3], arch: match[4] }
}

export default function ProviderDetailPage() {
  const router = useRouter()
  const params = useParams<{ name: string }>()
  const { name } = params

  const [versions, setVersions] = useState<Version[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [expandedVersion, setExpandedVersion] = useState<string | null>(null)

  // Upload state
  const [showUpload, setShowUpload] = useState(false)
  const [detectedFiles, setDetectedFiles] = useState<DetectedFile[]>([])
  const [uploading, setUploading] = useState(false)
  const [uploadProgress, setUploadProgress] = useState<Record<string, number>>({})

  // Advanced: create version/platform forms
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [showCreateVersion, setShowCreateVersion] = useState(false)
  const [newVersion, setNewVersion] = useState('')
  const [newKeyId, setNewKeyId] = useState('')
  const [newProtocols, setNewProtocols] = useState('5.0')
  const [creatingVersion, setCreatingVersion] = useState(false)

  const [platformForVersion, setPlatformForVersion] = useState<string | null>(null)
  const [newOs, setNewOs] = useState('linux')
  const [newArch, setNewArch] = useState('amd64')
  const [newFilename, setNewFilename] = useState('')
  const [creatingPlatform, setCreatingPlatform] = useState(false)

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

  const basePath = `/api/v2/organizations/default/registry-providers/private/default/${name}`

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

  const handleDirectorySelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (!files) return

    const detected: DetectedFile[] = []
    for (let i = 0; i < files.length; i++) {
      const file = files[i]
      const parsed = parseProviderFilename(file.name)
      if (parsed) {
        detected.push({ file, ...parsed })
      }
    }
    detected.sort((a, b) => `${a.version}-${a.os}-${a.arch}`.localeCompare(`${b.version}-${b.os}-${b.arch}`))
    setDetectedFiles(detected)
  }, [])

  async function handleUploadAll() {
    if (detectedFiles.length === 0) return
    setUploading(true)
    setError('')
    const progress: Record<string, number> = {}

    try {
      for (const df of detectedFiles) {
        const key = `${df.version}-${df.os}-${df.arch}`
        progress[key] = 0
        setUploadProgress({ ...progress })

        const arrayBuffer = await df.file.arrayBuffer()
        const res = await apiFetch(
          `${basePath}/versions/${df.version}/platforms/${df.os}/${df.arch}/upload`,
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/zip' },
            body: arrayBuffer,
          }
        )
        if (!res.ok) {
          const data = await res.json().catch(() => ({}))
          throw new Error(data.detail || `Upload failed for ${df.file.name} (${res.status})`)
        }
        progress[key] = 100
        setUploadProgress({ ...progress })
      }

      setDetectedFiles([])
      setShowUpload(false)
      setUploadProgress({})
      await loadVersions()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed')
    } finally {
      setUploading(false)
    }
  }

  async function handleCreateVersion(e: React.FormEvent) {
    e.preventDefault()
    setCreatingVersion(true)
    setError('')
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

  // Group detected files by version for preview
  const groupedFiles = detectedFiles.reduce<Record<string, DetectedFile[]>>((acc, df) => {
    if (!acc[df.version]) acc[df.version] = []
    acc[df.version].push(df)
    return acc
  }, {})

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
              {provPerms['can-create-version'] && (
                <button
                  onClick={() => { setShowUpload(!showUpload); setShowAdvanced(false) }}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors flex items-center gap-2"
                >
                  <Upload size={16} />
                  {showUpload ? 'Cancel' : 'Upload Binaries'}
                </button>
              )}
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

        {/* Upload section */}
        {showUpload && (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-5 mb-6 space-y-4">
            <div>
              <p className="text-sm text-slate-300 mb-3">
                Select a directory containing provider binaries. Files matching the pattern{' '}
                <code className="text-xs bg-slate-700 px-1.5 py-0.5 rounded text-brand-300">
                  terraform-provider-*_VERSION_OS_ARCH.zip
                </code>{' '}
                will be detected automatically.
              </p>
              <label className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 cursor-pointer transition-colors border border-slate-600">
                <FolderOpen size={16} />
                Select Directory
                {/* @ts-expect-error webkitdirectory is not in standard types */}
                <input type="file" webkitdirectory="" multiple className="hidden" onChange={handleDirectorySelect} />
              </label>
            </div>

            {detectedFiles.length > 0 && (
              <>
                <div className="space-y-3">
                  {Object.entries(groupedFiles).map(([ver, files]) => (
                    <div key={ver}>
                      <h4 className="text-sm font-medium text-slate-300 mb-1">Version {ver}</h4>
                      <div className="bg-slate-900/50 rounded-lg overflow-hidden">
                        <table className="w-full text-sm">
                          <thead>
                            <tr className="text-slate-500 text-xs border-b border-slate-700/30">
                              <th className="text-left px-3 py-1.5 font-medium">OS</th>
                              <th className="text-left px-3 py-1.5 font-medium">Arch</th>
                              <th className="text-left px-3 py-1.5 font-medium">Filename</th>
                              <th className="text-right px-3 py-1.5 font-medium">Size</th>
                              <th className="text-right px-3 py-1.5 font-medium w-24">Status</th>
                            </tr>
                          </thead>
                          <tbody>
                            {files.map((df) => {
                              const key = `${df.version}-${df.os}-${df.arch}`
                              const pct = uploadProgress[key]
                              return (
                                <tr key={key} className="border-t border-slate-700/20">
                                  <td className="px-3 py-1.5 text-slate-300">{df.os}</td>
                                  <td className="px-3 py-1.5 text-slate-300">{df.arch}</td>
                                  <td className="px-3 py-1.5 text-slate-400 font-mono text-xs">{df.file.name}</td>
                                  <td className="px-3 py-1.5 text-slate-400 text-right text-xs">
                                    {(df.file.size / 1024 / 1024).toFixed(1)} MB
                                  </td>
                                  <td className="px-3 py-1.5 text-right">
                                    {pct === undefined ? (
                                      <span className="text-xs text-slate-500">Pending</span>
                                    ) : pct === 100 ? (
                                      <span className="text-xs text-green-400">Done</span>
                                    ) : (
                                      <span className="text-xs text-brand-400">Uploading...</span>
                                    )}
                                  </td>
                                </tr>
                              )
                            })}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  ))}
                </div>

                <button
                  onClick={handleUploadAll}
                  disabled={uploading}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors flex items-center gap-2"
                >
                  <Upload size={16} />
                  {uploading ? 'Uploading...' : `Upload ${detectedFiles.length} file(s)`}
                </button>
              </>
            )}

            {/* Advanced toggle */}
            <div className="pt-2 border-t border-slate-700/30">
              <button
                onClick={() => setShowAdvanced(!showAdvanced)}
                className="text-xs text-slate-500 hover:text-slate-400 transition-colors flex items-center gap-1"
              >
                {showAdvanced ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                Advanced (manual version/platform creation)
              </button>
            </div>

            {showAdvanced && (
              <div className="space-y-3 pt-2">
                <button
                  onClick={() => setShowCreateVersion(!showCreateVersion)}
                  className="px-3 py-1.5 rounded text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-300 transition-colors"
                >
                  {showCreateVersion ? 'Cancel' : 'Create Version Manually'}
                </button>

                {showCreateVersion && (
                  <form onSubmit={handleCreateVersion} className="bg-slate-900/50 rounded-lg p-3 space-y-3">
                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                      <div>
                        <label htmlFor="pv-ver" className="block text-xs font-medium text-slate-400 mb-1">Version</label>
                        <input id="pv-ver" type="text" value={newVersion} onChange={(e) => setNewVersion(e.target.value)} required placeholder="1.0.0"
                          className="w-full px-2.5 py-1.5 text-sm border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                      </div>
                      <div>
                        <label htmlFor="pv-key" className="block text-xs font-medium text-slate-400 mb-1">GPG Key ID</label>
                        <input id="pv-key" type="text" value={newKeyId} onChange={(e) => setNewKeyId(e.target.value)} placeholder="optional"
                          className="w-full px-2.5 py-1.5 text-sm border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                      </div>
                      <div>
                        <label htmlFor="pv-proto" className="block text-xs font-medium text-slate-400 mb-1">Protocols</label>
                        <input id="pv-proto" type="text" value={newProtocols} onChange={(e) => setNewProtocols(e.target.value)} placeholder="5.0"
                          className="w-full px-2.5 py-1.5 text-sm border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                      </div>
                    </div>
                    <button type="submit" disabled={creatingVersion}
                      className="px-3 py-1.5 rounded text-xs font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                      {creatingVersion ? 'Creating...' : 'Create Version'}
                    </button>
                  </form>
                )}
              </div>
            )}
          </div>
        )}

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
          <EmptyState message="No versions yet. Upload binaries to get started." />
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
                    </div>
                    <div className="flex items-center gap-2">
                      {provPerms['can-create-version'] && (
                        <button
                          onClick={(e) => { e.stopPropagation(); setPlatformForVersion(v.attributes.version) }}
                          className="text-xs text-brand-400 hover:text-brand-300 transition-colors"
                        >
                          Add Platform
                        </button>
                      )}
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
