'use client'

import { useEffect, useState, useCallback } from 'react'
import { useParams, useRouter } from 'next/navigation'
import { Upload, FolderOpen, GitBranch, Link2, Plus, Trash2, Search } from 'lucide-react'
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

interface VersionStatus {
  version: string
  status: string
  'vcs-commit-sha'?: string
  'vcs-tag'?: string
}

interface VCSConnection {
  id: string
  attributes: {
    name: string
    provider: string
  }
}

interface ModulePermissions {
  'can-update': boolean
  'can-destroy': boolean
  'can-create-version': boolean
}

interface ModuleDetail {
  id: string
  attributes: {
    name: string
    namespace: string
    provider: string
    status: string
    source: string
    labels: Record<string, string>
    'owner-email': string
    'vcs-connection-id': string | null
    'vcs-repo-url': string
    'vcs-branch': string
    'vcs-tag-pattern': string
    'vcs-last-tag': string
    'version-statuses': VersionStatus[]
    'created-at': string | null
    'updated-at': string | null
    permissions: ModulePermissions
  }
}

async function buildTarGz(files: File[]): Promise<Uint8Array> {
  const { gzipSync } = await import('fflate')

  // Build tar archive manually (512-byte header + file data per entry)
  const entries: Uint8Array[] = []

  for (const file of files) {
    const data = new Uint8Array(await file.arrayBuffer())
    // Use webkitRelativePath to get relative path within selected directory
    const relativePath = file.webkitRelativePath
      ? file.webkitRelativePath.split('/').slice(1).join('/')
      : file.name

    // Build tar header (512 bytes)
    const header = new Uint8Array(512)
    const encoder = new TextEncoder()

    // name (0-99)
    const nameBytes = encoder.encode(relativePath)
    header.set(nameBytes.subarray(0, 100), 0)

    // mode (100-107) — 0644
    header.set(encoder.encode('0000644\0'), 100)

    // uid (108-115)
    header.set(encoder.encode('0000000\0'), 108)

    // gid (116-123)
    header.set(encoder.encode('0000000\0'), 116)

    // size (124-135) — octal
    const sizeStr = data.length.toString(8).padStart(11, '0') + '\0'
    header.set(encoder.encode(sizeStr), 124)

    // mtime (136-147) — current time in octal
    const mtime = Math.floor(Date.now() / 1000).toString(8).padStart(11, '0') + '\0'
    header.set(encoder.encode(mtime), 136)

    // checksum placeholder (148-155) — spaces
    header.set(encoder.encode('        '), 148)

    // typeflag (156) — '0' for regular file
    header[156] = 0x30

    // magic (257-262) — "ustar\0"
    header.set(encoder.encode('ustar\0'), 257)

    // version (263-264)
    header.set(encoder.encode('00'), 263)

    // Calculate checksum
    let checksum = 0
    for (let i = 0; i < 512; i++) {
      checksum += header[i]
    }
    const checksumStr = checksum.toString(8).padStart(6, '0') + '\0 '
    header.set(encoder.encode(checksumStr), 148)

    entries.push(header)
    entries.push(data)

    // Pad to 512-byte boundary
    const padding = (512 - (data.length % 512)) % 512
    if (padding > 0) {
      entries.push(new Uint8Array(padding))
    }
  }

  // Two 512-byte zero blocks to mark end of archive
  entries.push(new Uint8Array(1024))

  // Concatenate all entries
  const totalSize = entries.reduce((s, e) => s + e.length, 0)
  const tar = new Uint8Array(totalSize)
  let offset = 0
  for (const entry of entries) {
    tar.set(entry, offset)
    offset += entry.length
  }

  // Gzip compress
  return gzipSync(tar)
}

export default function ModuleDetailPage() {
  const router = useRouter()
  const params = useParams<{ name: string; provider: string }>()
  const { name, provider } = params

  const [module, setModule] = useState<ModuleDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Upload state
  const [showUpload, setShowUpload] = useState(false)
  const [uploadVersion, setUploadVersion] = useState('')
  const [selectedFiles, setSelectedFiles] = useState<File[]>([])
  const [uploading, setUploading] = useState(false)

  // VCS state
  const [showVcs, setShowVcs] = useState(false)
  const [vcsConnections, setVcsConnections] = useState<VCSConnection[]>([])
  const [vcsConnectionId, setVcsConnectionId] = useState('')
  const [vcsRepoUrl, setVcsRepoUrl] = useState('')
  const [vcsBranch, setVcsBranch] = useState('')
  const [vcsTagPattern, setVcsTagPattern] = useState('v*')
  const [savingVcs, setSavingVcs] = useState(false)

  // Metadata editing (labels/owner)
  const [editingMeta, setEditingMeta] = useState(false)
  const [editLabels, setEditLabels] = useState<Record<string, string>>({})
  const [editOwner, setEditOwner] = useState('')
  const [savingMeta, setSavingMeta] = useState(false)

  // Delete confirmation
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null)

  // Label lockout warning
  const [lockoutWarning, setLockoutWarning] = useState('')

  // Workspace links (impact analysis)
  interface WorkspaceLink {
    id: string
    attributes: {
      'workspace-id': string
      'workspace-name': string
      'created-at': string | null
      'created-by': string
    }
  }
  const [workspaceLinks, setWorkspaceLinks] = useState<WorkspaceLink[]>([])
  const [linksLoading, setLinksLoading] = useState(false)
  const [showLinkPicker, setShowLinkPicker] = useState(false)
  const [wsSearchQuery, setWsSearchQuery] = useState('')
  const [wsSearchResults, setWsSearchResults] = useState<{ id: string; name: string }[]>([])
  const [linkingWs, setLinkingWs] = useState('')

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadModule()
    loadWorkspaceLinks()
  }, [router, name, provider])

  usePollingInterval(!loading, 60_000, loadModule)

  async function loadModule() {
    setLoading(true)
    try {
      const res = await apiFetch(
        `/api/v2/organizations/default/registry-modules/private/default/${name}/${provider}`
      )
      if (!res.ok) throw new Error('Module not found')
      const data = await res.json()
      setModule(data.data)

      // Pre-fill VCS fields from module data
      const attrs = data.data?.attributes
      if (attrs) {
        setVcsConnectionId(attrs['vcs-connection-id'] || '')
        setVcsRepoUrl(attrs['vcs-repo-url'] || '')
        setVcsBranch(attrs['vcs-branch'] || '')
        setVcsTagPattern(attrs['vcs-tag-pattern'] || 'v*')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load module')
    } finally {
      setLoading(false)
    }
  }

  async function loadVcsConnections() {
    try {
      const res = await apiFetch('/api/v2/organizations/default/vcs-connections')
      if (res.ok) {
        const data = await res.json()
        setVcsConnections(data.data || [])
      }
    } catch {
      // VCS connections are optional — ignore errors
    }
  }

  async function loadWorkspaceLinks() {
    setLinksLoading(true)
    try {
      const res = await apiFetch(
        `/api/v2/organizations/default/registry-modules/private/default/${name}/${provider}/workspace-links`
      )
      if (res.ok) {
        const data = await res.json()
        setWorkspaceLinks(data.data || [])
      }
    } catch {
      // Non-critical
    } finally {
      setLinksLoading(false)
    }
  }

  async function searchWorkspaces(query: string) {
    try {
      const res = await apiFetch('/api/v2/organizations/default/workspaces')
      if (res.ok) {
        const data = await res.json()
        const all = (data.data || []).map((ws: { id: string; attributes: { name: string } }) => ({
          id: ws.id,
          name: ws.attributes.name,
        }))
        const linked = new Set(workspaceLinks.map(l => l.attributes['workspace-id']))
        const filtered = all.filter(
          (ws: { id: string; name: string }) =>
            !linked.has(ws.id) &&
            (!query || ws.name.toLowerCase().includes(query.toLowerCase()))
        )
        setWsSearchResults(filtered.slice(0, 20))
      }
    } catch {
      // ignore
    }
  }

  async function handleLinkWorkspace(wsId: string) {
    setLinkingWs(wsId)
    setError('')
    try {
      const res = await apiFetch(
        `/api/v2/organizations/default/registry-modules/private/default/${name}/${provider}/workspace-links`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/vnd.api+json' },
          body: JSON.stringify({
            data: { type: 'workspace-links', attributes: { workspace_id: wsId } },
          }),
        }
      )
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Link failed (${res.status})`)
      }
      setShowLinkPicker(false)
      setWsSearchQuery('')
      await loadWorkspaceLinks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to link workspace')
    } finally {
      setLinkingWs('')
    }
  }

  async function handleUnlinkWorkspace(linkId: string) {
    setError('')
    try {
      const res = await apiFetch(
        `/api/v2/organizations/default/registry-modules/private/default/${name}/${provider}/workspace-links/${linkId}`,
        { method: 'DELETE' }
      )
      if (!res.ok && res.status !== 204) {
        throw new Error(`Unlink failed (${res.status})`)
      }
      await loadWorkspaceLinks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to unlink workspace')
    }
  }

  const handleDirectorySelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (!files) return
    const fileList: File[] = []
    for (let i = 0; i < files.length; i++) {
      // Skip hidden files and directories
      if (!files[i].name.startsWith('.')) {
        fileList.push(files[i])
      }
    }
    setSelectedFiles(fileList)
  }, [])

  async function handleUpload() {
    if (!uploadVersion || selectedFiles.length === 0) return
    setUploading(true)
    setError('')
    try {
      const tarGz = await buildTarGz(selectedFiles)
      const res = await apiFetch(
        `/api/v2/organizations/default/registry-modules/private/default/${name}/${provider}/versions/${uploadVersion}/upload`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/gzip' },
          body: tarGz as unknown as BodyInit,
        }
      )
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Upload failed (${res.status})`)
      }

      setSelectedFiles([])
      setUploadVersion('')
      setShowUpload(false)
      await loadModule()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed')
    } finally {
      setUploading(false)
    }
  }

  async function handleSaveVcs() {
    setSavingVcs(true)
    setError('')
    try {
      const res = await apiFetch(
        `/api/v2/organizations/default/registry-modules/private/default/${name}/${provider}/vcs`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/vnd.api+json' },
          body: JSON.stringify({
            data: {
              type: 'registry-modules',
              attributes: {
                source: 'vcs',
                vcs_connection_id: vcsConnectionId,
                vcs_repo_url: vcsRepoUrl,
                vcs_branch: vcsBranch,
                vcs_tag_pattern: vcsTagPattern,
              },
            },
          }),
        }
      )
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to save VCS config (${res.status})`)
      }
      await loadModule()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save VCS config')
    } finally {
      setSavingVcs(false)
    }
  }

  async function handleDisableVcs() {
    setSavingVcs(true)
    setError('')
    try {
      const res = await apiFetch(
        `/api/v2/organizations/default/registry-modules/private/default/${name}/${provider}/vcs`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/vnd.api+json' },
          body: JSON.stringify({
            data: {
              type: 'registry-modules',
              attributes: {
                source: 'upload',
                vcs_connection_id: '',
                vcs_repo_url: '',
                vcs_branch: '',
                vcs_tag_pattern: 'v*',
              },
            },
          }),
        }
      )
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to disable VCS (${res.status})`)
      }
      await loadModule()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to disable VCS')
    } finally {
      setSavingVcs(false)
    }
  }

  async function handleDelete() {
    if (!deleteTarget) return
    setError('')
    try {
      const path = deleteTarget === 'module'
        ? `/api/v2/organizations/default/registry-modules/private/default/${name}/${provider}`
        : `/api/v2/organizations/default/registry-modules/private/default/${name}/${provider}/${deleteTarget}`

      const res = await apiFetch(path, { method: 'DELETE' })
      if (!res.ok && res.status !== 204) {
        throw new Error(`Delete failed (${res.status})`)
      }

      setDeleteTarget(null)
      if (deleteTarget === 'module') {
        router.push('/registry/modules')
      } else {
        await loadModule()
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Delete failed')
      setDeleteTarget(null)
    }
  }

  function startEditingMeta() {
    if (!module) return
    setEditLabels(module.attributes.labels || {})
    setEditOwner(module.attributes['owner-email'] || '')
    setEditingMeta(true)
  }

  async function handleSaveMeta(force = false) {
    if (!module) return
    setSavingMeta(true)
    setError('')
    setLockoutWarning('')
    try {
      const res = await apiFetch(`/api/v2/organizations/default/registry-modules/private/default/${name}/${provider}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'registry-modules', attributes: { labels: editLabels, ...(isAdmin() ? { 'owner-email': editOwner } : {}), ...(force ? { force: true } : {}) } },
        }),
      })
      if (res.status === 409) {
        const errData = await res.json()
        const detail = errData.errors?.[0]?.detail || 'This label change would reduce your access.'
        setLockoutWarning(detail)
        return
      }
      if (!res.ok) throw new Error('Failed to update module')
      const data = await res.json()
      setModule(data.data)
      setEditingMeta(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update module')
    } finally {
      setSavingMeta(false)
    }
  }

  const isVcsSource = module?.attributes.source === 'vcs'
  const modPerms = module?.attributes.permissions || {} as ModulePermissions

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        {error && <ErrorBanner message={error} />}

        {loading ? (
          <LoadingSpinner />
        ) : module ? (
          <>
            <PageHeader
              title={module.attributes.name}
              description={`Provider: ${module.attributes.provider}`}
              actions={
                <div className="flex gap-2">
                  {modPerms['can-create-version'] && (
                    <button
                      onClick={() => { setShowUpload(!showUpload); setShowVcs(false) }}
                      className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors flex items-center gap-2"
                    >
                      <Upload size={16} />
                      {showUpload ? 'Cancel' : 'Upload Version'}
                    </button>
                  )}
                  {modPerms['can-update'] && (
                    <button
                      onClick={() => { setShowVcs(!showVcs); setShowUpload(false); if (!showVcs) loadVcsConnections() }}
                      className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors flex items-center gap-2 ${
                        isVcsSource
                          ? 'bg-green-900/50 text-green-300 border border-green-800/50 hover:bg-green-800/50'
                          : 'bg-slate-700 hover:bg-slate-600 text-slate-300'
                      }`}
                    >
                      <GitBranch size={16} />
                      {showVcs ? 'Cancel' : (isVcsSource ? 'VCS Connected' : 'Connect VCS')}
                    </button>
                  )}
                  {modPerms['can-destroy'] && (
                    <button
                      onClick={() => setDeleteTarget('module')}
                      className="px-4 py-2 rounded-lg text-sm font-medium bg-red-900/50 hover:bg-red-800/50 text-red-300 border border-red-800/50 transition-colors"
                    >
                      Delete Module
                    </button>
                  )}
                </div>
              }
            />

            {/* Upload section */}
            {showUpload && (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-5 mb-6 space-y-4">
                <p className="text-sm text-slate-300">
                  Select a directory containing your Terraform module files. They will be packaged into a tarball and uploaded.
                </p>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <div>
                    <label htmlFor="upload-ver" className="block text-sm font-medium text-slate-300 mb-1">Version</label>
                    <input
                      id="upload-ver"
                      type="text"
                      value={uploadVersion}
                      onChange={(e) => setUploadVersion(e.target.value)}
                      required
                      placeholder="1.0.0"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-slate-300 mb-1">Module Directory</label>
                    <label className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 cursor-pointer transition-colors border border-slate-600">
                      <FolderOpen size={16} />
                      {selectedFiles.length > 0 ? `${selectedFiles.length} file(s)` : 'Select Directory'}
                      {/* @ts-expect-error webkitdirectory is not in standard types */}
                      <input type="file" webkitdirectory="" multiple className="hidden" onChange={handleDirectorySelect} />
                    </label>
                  </div>
                </div>

                {selectedFiles.length > 0 && (
                  <div className="bg-slate-900/50 rounded-lg p-3 max-h-48 overflow-y-auto">
                    <p className="text-xs text-slate-500 mb-2">{selectedFiles.length} files to include:</p>
                    <div className="space-y-0.5">
                      {selectedFiles.slice(0, 50).map((f, i) => (
                        <div key={i} className="text-xs text-slate-400 font-mono">
                          {f.webkitRelativePath ? f.webkitRelativePath.split('/').slice(1).join('/') : f.name}
                        </div>
                      ))}
                      {selectedFiles.length > 50 && (
                        <div className="text-xs text-slate-500">...and {selectedFiles.length - 50} more</div>
                      )}
                    </div>
                  </div>
                )}

                <button
                  onClick={handleUpload}
                  disabled={uploading || !uploadVersion || selectedFiles.length === 0}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors flex items-center gap-2"
                >
                  <Upload size={16} />
                  {uploading ? 'Uploading...' : 'Upload'}
                </button>
              </div>
            )}

            {/* VCS configuration section */}
            {showVcs && (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-5 mb-6 space-y-4">
                <p className="text-sm text-slate-300">
                  Connect this module to a VCS repository. New versions are published automatically when semver tags matching the tag pattern are pushed. Branch commits alone do not create versions.
                </p>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <div>
                    <label htmlFor="vcs-conn" className="block text-sm font-medium text-slate-300 mb-1">VCS Connection</label>
                    <select
                      id="vcs-conn"
                      value={vcsConnectionId}
                      onChange={(e) => setVcsConnectionId(e.target.value)}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                    >
                      <option value="">Select a connection...</option>
                      {vcsConnections.map((conn) => (
                        <option key={conn.id} value={conn.id}>
                          {conn.attributes.name} ({conn.attributes.provider})
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label htmlFor="vcs-repo" className="block text-sm font-medium text-slate-300 mb-1">Repository URL</label>
                    <input
                      id="vcs-repo"
                      type="text"
                      value={vcsRepoUrl}
                      onChange={(e) => setVcsRepoUrl(e.target.value)}
                      placeholder="https://github.com/org/terraform-module-vpc"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                    />
                  </div>
                  <div>
                    <label htmlFor="vcs-branch" className="block text-sm font-medium text-slate-300 mb-1">Branch (optional)</label>
                    <input
                      id="vcs-branch"
                      type="text"
                      value={vcsBranch}
                      onChange={(e) => setVcsBranch(e.target.value)}
                      placeholder="main (leave empty for default)"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                    />
                  </div>
                  <div>
                    <label htmlFor="vcs-tag" className="block text-sm font-medium text-slate-300 mb-1">Tag Pattern</label>
                    <input
                      id="vcs-tag"
                      type="text"
                      value={vcsTagPattern}
                      onChange={(e) => setVcsTagPattern(e.target.value)}
                      placeholder="v*"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                    />
                    <p className="mt-1 text-xs text-slate-500">Only tags matching this pattern create versions (e.g. v1.0.0)</p>
                  </div>
                </div>

                <div className="flex gap-2">
                  <button
                    onClick={handleSaveVcs}
                    disabled={savingVcs || !vcsConnectionId || !vcsRepoUrl}
                    className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
                  >
                    {savingVcs ? 'Saving...' : 'Save VCS Configuration'}
                  </button>
                  {isVcsSource && (
                    <button
                      onClick={handleDisableVcs}
                      disabled={savingVcs}
                      className="px-4 py-2 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 text-slate-300 transition-colors"
                    >
                      Disconnect VCS
                    </button>
                  )}
                </div>
              </div>
            )}

            {/* VCS status info when connected but section not open */}
            {isVcsSource && !showVcs && module.attributes['vcs-repo-url'] && (
              <div className="bg-green-900/20 rounded-lg border border-green-800/30 px-4 py-3 mb-6">
                <div className="flex items-center gap-3">
                  <GitBranch size={16} className="text-green-400" />
                  <div className="text-sm">
                    <span className="text-green-300">VCS connected:</span>{' '}
                    <span className="text-slate-300 font-mono text-xs">{module.attributes['vcs-repo-url']}</span>
                    {module.attributes['vcs-last-tag'] && (
                      <span className="text-slate-400 ml-2">Last tag: {module.attributes['vcs-last-tag']}</span>
                    )}
                  </div>
                </div>
                {(module.attributes['version-statuses'] || []).length === 0 && (
                  <p className="text-xs text-green-400/70 mt-2 ml-7">
                    Push a semver tag (e.g. <code className="font-mono">v1.0.0</code>) matching the tag pattern to publish the first version.
                  </p>
                )}
              </div>
            )}

            {/* Linked Workspaces (Impact Analysis) */}
            {modPerms['can-update'] && (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-5 mb-6">
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <Link2 size={16} className="text-purple-400" />
                    <h3 className="text-sm font-medium text-slate-300">Linked Workspaces</h3>
                  </div>
                  <button
                    onClick={() => { setShowLinkPicker(!showLinkPicker); if (!showLinkPicker) searchWorkspaces('') }}
                    className="text-xs text-brand-400 hover:text-brand-300 flex items-center gap-1"
                  >
                    <Plus size={12} />
                    Link Workspace
                  </button>
                </div>

                <p className="text-xs text-slate-500 mb-3">
                  Linked workspaces receive speculative plans when PRs are opened against this module, and standard runs when new versions are published.
                </p>

                {showLinkPicker && (
                  <div className="mb-3 p-3 bg-slate-900/50 rounded-lg border border-slate-700/30">
                    <div className="flex items-center gap-2 mb-2">
                      <Search size={14} className="text-slate-400" />
                      <input
                        type="text"
                        value={wsSearchQuery}
                        onChange={(e) => { setWsSearchQuery(e.target.value); searchWorkspaces(e.target.value) }}
                        placeholder="Search workspaces..."
                        className="flex-1 px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                      />
                    </div>
                    <div className="max-h-40 overflow-y-auto space-y-1">
                      {wsSearchResults.length === 0 ? (
                        <p className="text-xs text-slate-500 py-2 text-center">No workspaces found</p>
                      ) : wsSearchResults.map(ws => (
                        <button
                          key={ws.id}
                          onClick={() => handleLinkWorkspace(ws.id)}
                          disabled={linkingWs === ws.id}
                          className="w-full text-left px-2 py-1.5 rounded text-sm text-slate-300 hover:bg-slate-700/50 transition-colors disabled:opacity-50"
                        >
                          {ws.name}
                          {linkingWs === ws.id && <span className="text-xs text-slate-500 ml-2">Linking...</span>}
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                {workspaceLinks.length === 0 ? (
                  <p className="text-xs text-slate-500 italic">No workspaces linked yet.</p>
                ) : (
                  <div className="space-y-1">
                    {workspaceLinks.map(link => (
                      <div key={link.id} className="flex items-center justify-between px-2 py-1.5 rounded hover:bg-slate-700/30">
                        <a
                          href={`/workspaces/${link.attributes['workspace-id']}`}
                          className="text-sm text-brand-400 hover:text-brand-300"
                        >
                          {link.attributes['workspace-name']}
                        </a>
                        <button
                          onClick={() => handleUnlinkWorkspace(link.id)}
                          className="text-slate-500 hover:text-red-400 transition-colors"
                          title="Unlink workspace"
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Metadata: Owner & Labels */}
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-5 mb-6">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-medium text-slate-300">Metadata</h3>
                {!editingMeta ? (
                  modPerms['can-update'] && <button onClick={startEditingMeta} className="text-xs text-brand-400 hover:text-brand-300">Edit</button>
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
                    <dd className="mt-1 text-sm text-slate-200">{module.attributes['owner-email'] || 'None'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500 mb-1">Labels</dt>
                  {editingMeta ? (
                    <LabelsEditor labels={editLabels} onChange={setEditLabels} />
                  ) : (
                    <dd className="mt-1"><LabelsEditor labels={module.attributes.labels || {}} readOnly /></dd>
                  )}
                </div>
              </dl>
              {lockoutWarning && (
                <div className="mt-4 p-3 bg-amber-900/30 border border-amber-700/50 rounded-lg">
                  <p className="text-sm text-amber-300 mb-2">{lockoutWarning}</p>
                  <div className="flex gap-2">
                    <button
                      onClick={() => { setLockoutWarning(''); setEditLabels(module.attributes.labels || {}); }}
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

            <h2 className="text-lg font-semibold text-slate-200 mb-3">Versions</h2>
            {(module.attributes['version-statuses'] || []).length === 0 ? (
              <EmptyState message={
                isVcsSource
                  ? `No versions yet. Push a semver tag matching "${module.attributes['vcs-tag-pattern'] || 'v*'}" to the connected repository to publish a version.`
                  : 'No versions yet. Upload a module or connect VCS to get started.'
              } />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="text-left px-4 py-3 text-slate-400 font-medium">Version</th>
                      <th className="text-left px-4 py-3 text-slate-400 font-medium">Status</th>
                      {isVcsSource && (
                        <th className="text-left px-4 py-3 text-slate-400 font-medium">Source</th>
                      )}
                      <th className="text-right px-4 py-3 text-slate-400 font-medium">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {module.attributes['version-statuses'].map((v) => (
                      <tr key={v.version} className="border-b border-slate-700/30 last:border-0">
                        <td className="px-4 py-3 text-slate-200 font-mono">{v.version}</td>
                        <td className="px-4 py-3">
                          <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                            v.status === 'uploaded'
                              ? 'bg-green-900/50 text-green-300'
                              : 'bg-amber-900/50 text-amber-300'
                          }`}>
                            {v.status}
                          </span>
                        </td>
                        {isVcsSource && (
                          <td className="px-4 py-3">
                            {v['vcs-tag'] ? (
                              <span className="text-slate-300 text-xs">
                                <span className="font-mono">{v['vcs-tag']}</span>
                                {v['vcs-commit-sha'] && (
                                  <span className="text-slate-500 ml-1.5" title={v['vcs-commit-sha']}>
                                    ({v['vcs-commit-sha'].slice(0, 7)})
                                  </span>
                                )}
                              </span>
                            ) : (
                              <span className="text-slate-500 text-xs">manual upload</span>
                            )}
                          </td>
                        )}
                        {modPerms['can-destroy'] && (
                          <td className="px-4 py-3 text-right">
                            <button
                              onClick={() => setDeleteTarget(v.version)}
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
          </>
        ) : null}

        {/* Delete confirmation dialog */}
        <Dialog.Root open={deleteTarget !== null} onOpenChange={(open) => { if (!open) setDeleteTarget(null) }}>
          <Dialog.Portal>
            <Dialog.Overlay className="fixed inset-0 bg-black/60" />
            <Dialog.Content className="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 bg-slate-800 rounded-lg border border-slate-700 p-6 w-full max-w-md shadow-xl">
              <Dialog.Title className="text-lg font-semibold text-slate-100">
                Confirm Delete
              </Dialog.Title>
              <Dialog.Description className="text-sm text-slate-400 mt-2">
                {deleteTarget === 'module'
                  ? 'This will permanently delete this module and all its versions.'
                  : `This will permanently delete version ${deleteTarget}.`}
              </Dialog.Description>
              <div className="flex justify-end gap-3 mt-6">
                <button
                  onClick={() => setDeleteTarget(null)}
                  className="px-4 py-2 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-700 transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleDelete}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-500 text-white transition-colors"
                >
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
