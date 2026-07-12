'use client'

import { useEffect, useState, useCallback } from 'react'
import { useParams, useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import { Upload, FolderOpen, GitBranch, Link2, Plus, Trash2 } from 'lucide-react'
import * as Dialog from '@radix-ui/react-dialog'
import NavBar from '@/components/nav-bar'
import { WorkspacePicker } from '@/components/workspace-picker'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { getAuthState, isAdmin } from '@/lib/auth'
import { useConfirm } from '@/lib/use-confirm'
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
  const t = useTranslations('registry')
  const { confirmDelete } = useConfirm()
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

  // Module interface (inputs/outputs)
  const [interfaceData, setInterfaceData] = useState<{
    inputs: { name: string; type: string; type_schema: object; description: string; default: string | null; required: boolean; sensitive: boolean }[] | null
    outputs: { name: string; description: string; sensitive: boolean }[] | null
  } | null>(null)
  const [interfaceLoading, setInterfaceLoading] = useState(false)
  const [interfaceExpanded, setInterfaceExpanded] = useState(false)
  const [interfaceVersion, setInterfaceVersion] = useState('')
  const [versionsExpanded, setVersionsExpanded] = useState(false)

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
  const [showLinkPicker, setShowLinkPicker] = useState(false)
  const [linkingWs, setLinkingWs] = useState('')

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadModule()
    loadWorkspaceLinks()
  }, [router, name, provider])

  usePollingInterval(!loading, 60_000, loadModule)

  async function loadModule() {
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/registry-modules/private/default/${name}/${provider}`
      )
      if (!res.ok) throw new Error(t('moduleDetail.notFound'))
      const data = await res.json()
      setModule(data.data)

      // Pre-fill VCS fields from module data
      const attrs = data.data?.attributes
      if (attrs) {
        setVcsConnectionId(attrs['vcs-connection-id'] || '')
        setVcsRepoUrl(attrs['vcs-repo-url'] || '')
        setVcsBranch(attrs['vcs-branch'] || '')
        setVcsTagPattern(attrs['vcs-tag-pattern'] || 'v*')

        // Load interface for latest uploaded version
        const versions = (attrs['version-statuses'] || []) as VersionStatus[]
        const latestUploaded = versions.find((v: VersionStatus) => v.status === 'uploaded')
        if (latestUploaded && latestUploaded.version !== interfaceVersion) {
          setInterfaceVersion(latestUploaded.version)
          loadInterface(latestUploaded.version)
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t('moduleDetail.loadFailed'))
    } finally {
      setLoading(false)
    }
  }

  async function loadVcsConnections() {
    try {
      const res = await apiFetch('/api/terrapod/v1/vcs-connections')
      if (res.ok) {
        const data = await res.json()
        setVcsConnections(data.data || [])
      }
    } catch {
      // VCS connections are optional — ignore errors
    }
  }

  async function loadWorkspaceLinks() {
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/registry-modules/private/default/${name}/${provider}/workspace-links`
      )
      if (res.ok) {
        const data = await res.json()
        setWorkspaceLinks(data.data || [])
      }
    } catch {
      // Non-critical
    }
  }

  async function loadInterface(ver: string) {
    if (!ver) return
    setInterfaceLoading(true)
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/registry-modules/private/default/${name}/${provider}/${ver}/interface`
      )
      if (res.ok) {
        const data = await res.json()
        setInterfaceData(data.data.attributes)
      } else {
        setInterfaceData(null)
      }
    } catch {
      setInterfaceData(null)
    } finally {
      setInterfaceLoading(false)
    }
  }

  async function handleLinkWorkspace(wsId: string) {
    setLinkingWs(wsId)
    setError('')
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/registry-modules/private/default/${name}/${provider}/workspace-links`,
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
        throw new Error(data.detail || t('moduleDetail.linkFailedStatus', { status: res.status }))
      }
      setShowLinkPicker(false)
      await loadWorkspaceLinks()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('moduleDetail.linkFailed'))
    } finally {
      setLinkingWs('')
    }
  }

  async function handleUnlinkWorkspace(linkId: string) {
    if (!confirmDelete(t('moduleDetail.unlinkConfirm'))) return
    setError('')
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/registry-modules/private/default/${name}/${provider}/workspace-links/${linkId}`,
        { method: 'DELETE' }
      )
      if (!res.ok && res.status !== 204) {
        throw new Error(t('moduleDetail.unlinkFailedStatus', { status: res.status }))
      }
      await loadWorkspaceLinks()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('moduleDetail.unlinkFailed'))
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
        `/api/terrapod/v1/registry-modules/private/default/${name}/${provider}/versions/${uploadVersion}/upload`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/gzip' },
          body: tarGz as unknown as BodyInit,
        }
      )
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('moduleDetail.uploadFailedStatus', { status: res.status }))
      }

      setSelectedFiles([])
      setUploadVersion('')
      setShowUpload(false)
      await loadModule()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('moduleDetail.uploadFailed'))
    } finally {
      setUploading(false)
    }
  }

  async function handleSaveVcs() {
    setSavingVcs(true)
    setError('')
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/registry-modules/private/default/${name}/${provider}/vcs`,
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
        throw new Error(data.detail || t('moduleDetail.saveVcsFailedStatus', { status: res.status }))
      }
      await loadModule()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('moduleDetail.saveVcsFailed'))
    } finally {
      setSavingVcs(false)
    }
  }

  async function handleDisableVcs() {
    setSavingVcs(true)
    setError('')
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/registry-modules/private/default/${name}/${provider}/vcs`,
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
        throw new Error(data.detail || t('moduleDetail.disableVcsFailedStatus', { status: res.status }))
      }
      await loadModule()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('moduleDetail.disableVcsFailed'))
    } finally {
      setSavingVcs(false)
    }
  }

  async function handleDelete() {
    if (!deleteTarget) return
    setError('')
    try {
      const path = deleteTarget === 'module'
        ? `/api/terrapod/v1/registry-modules/private/default/${name}/${provider}`
        : `/api/terrapod/v1/registry-modules/private/default/${name}/${provider}/${deleteTarget}`

      const res = await apiFetch(path, { method: 'DELETE' })
      if (!res.ok && res.status !== 204) {
        throw new Error(t('moduleDetail.deleteFailedStatus', { status: res.status }))
      }

      setDeleteTarget(null)
      if (deleteTarget === 'module') {
        router.push('/registry/modules')
      } else {
        await loadModule()
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t('moduleDetail.deleteFailed'))
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
      const res = await apiFetch(`/api/terrapod/v1/registry-modules/private/default/${name}/${provider}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'registry-modules', attributes: { labels: editLabels, ...(isAdmin() ? { 'owner-email': editOwner } : {}), ...(force ? { force: true } : {}) } },
        }),
      })
      if (res.status === 409) {
        const errData = await res.json()
        const detail = errData.errors?.[0]?.detail || t('moduleDetail.labelLockoutDefault')
        setLockoutWarning(detail)
        return
      }
      if (!res.ok) throw new Error(t('moduleDetail.updateFailed'))
      const data = await res.json()
      setModule(data.data)
      setEditingMeta(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('moduleDetail.updateFailed'))
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
              description={t('moduleDetail.providerLabel', { provider: module.attributes.provider })}
              actions={
                <div className="flex gap-2">
                  {modPerms['can-create-version'] && (
                    <button
                      onClick={() => { setShowUpload(!showUpload); setShowVcs(false) }}
                      className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors flex items-center gap-2"
                    >
                      <Upload size={16} />
                      {showUpload ? t('moduleDetail.cancel') : t('moduleDetail.uploadVersion')}
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
                      {showVcs ? t('moduleDetail.cancel') : (isVcsSource ? t('moduleDetail.vcsConnected') : t('moduleDetail.connectVcs'))}
                    </button>
                  )}
                  {modPerms['can-destroy'] && (
                    <button
                      onClick={() => setDeleteTarget('module')}
                      className="px-4 py-2 rounded-lg text-sm font-medium bg-red-900/50 hover:bg-red-800/50 text-red-300 border border-red-800/50 transition-colors"
                    >
                      {t('moduleDetail.deleteModule')}
                    </button>
                  )}
                </div>
              }
            />

            {/* Upload section */}
            {showUpload && (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-5 mb-6 space-y-4">
                <p className="text-sm text-slate-300">
                  {t('moduleDetail.upload.intro')}
                </p>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <div>
                    <label htmlFor="upload-ver" className="block text-sm font-medium text-slate-300 mb-1">{t('moduleDetail.upload.version')}</label>
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
                    <label className="block text-sm font-medium text-slate-300 mb-1">{t('moduleDetail.upload.moduleDirectory')}</label>
                    <label className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 cursor-pointer transition-colors border border-slate-600">
                      <FolderOpen size={16} />
                      {selectedFiles.length > 0 ? t('moduleDetail.upload.fileCount', { count: selectedFiles.length }) : t('moduleDetail.upload.selectDirectory')}
                      {/* @ts-expect-error webkitdirectory is not in standard types */}
                      <input type="file" webkitdirectory="" multiple className="hidden" onChange={handleDirectorySelect} />
                    </label>
                  </div>
                </div>

                {selectedFiles.length > 0 && (
                  <div className="bg-slate-900/50 rounded-lg p-3 max-h-48 overflow-y-auto">
                    <p className="text-xs text-slate-500 mb-2">{t('moduleDetail.upload.filesToInclude', { count: selectedFiles.length })}</p>
                    <div className="space-y-0.5">
                      {selectedFiles.slice(0, 50).map((f, i) => (
                        <div key={i} className="text-xs text-slate-400 font-mono">
                          {f.webkitRelativePath ? f.webkitRelativePath.split('/').slice(1).join('/') : f.name}
                        </div>
                      ))}
                      {selectedFiles.length > 50 && (
                        <div className="text-xs text-slate-500">{t('moduleDetail.upload.andMore', { count: selectedFiles.length - 50 })}</div>
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
                  {uploading ? t('moduleDetail.upload.uploading') : t('moduleDetail.upload.upload')}
                </button>
              </div>
            )}

            {/* VCS configuration section */}
            {showVcs && (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-5 mb-6 space-y-4">
                <p className="text-sm text-slate-300">
                  {t('moduleDetail.vcs.intro')}
                </p>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <div>
                    <label htmlFor="vcs-conn" className="block text-sm font-medium text-slate-300 mb-1">{t('moduleDetail.vcs.connection')}</label>
                    <select
                      id="vcs-conn"
                      value={vcsConnectionId}
                      onChange={(e) => setVcsConnectionId(e.target.value)}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                    >
                      <option value="">{t('moduleDetail.vcs.selectConnection')}</option>
                      {vcsConnections.map((conn) => (
                        <option key={conn.id} value={conn.id}>
                          {conn.attributes.name} ({conn.attributes.provider})
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label htmlFor="vcs-repo" className="block text-sm font-medium text-slate-300 mb-1">{t('moduleDetail.vcs.repositoryUrl')}</label>
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
                    <label htmlFor="vcs-branch" className="block text-sm font-medium text-slate-300 mb-1">{t('moduleDetail.vcs.branchOptional')}</label>
                    <input
                      id="vcs-branch"
                      type="text"
                      value={vcsBranch}
                      onChange={(e) => setVcsBranch(e.target.value)}
                      placeholder={t('moduleDetail.vcs.branchPlaceholder')}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                    />
                  </div>
                  <div>
                    <label htmlFor="vcs-tag" className="block text-sm font-medium text-slate-300 mb-1">{t('moduleDetail.vcs.tagPattern')}</label>
                    <input
                      id="vcs-tag"
                      type="text"
                      value={vcsTagPattern}
                      onChange={(e) => setVcsTagPattern(e.target.value)}
                      placeholder="v*"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                    />
                    <p className="mt-1 text-xs text-slate-500">{t('moduleDetail.vcs.tagPatternHint')}</p>
                  </div>
                </div>

                <div className="flex gap-2">
                  <button
                    onClick={handleSaveVcs}
                    disabled={savingVcs || !vcsConnectionId || !vcsRepoUrl}
                    className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
                  >
                    {savingVcs ? t('moduleDetail.vcs.saving') : t('moduleDetail.vcs.saveConfiguration')}
                  </button>
                  {isVcsSource && (
                    <button
                      onClick={handleDisableVcs}
                      disabled={savingVcs}
                      className="px-4 py-2 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 text-slate-300 transition-colors"
                    >
                      {t('moduleDetail.vcs.disconnect')}
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
                    <span className="text-green-300">{t('moduleDetail.vcsStatus.connected')}</span>{' '}
                    <span className="text-slate-300 font-mono text-xs">{module.attributes['vcs-repo-url']}</span>
                    {module.attributes['vcs-last-tag'] && (
                      <span className="text-slate-400 ml-2">{t('moduleDetail.vcsStatus.lastTag', { tag: module.attributes['vcs-last-tag'] })}</span>
                    )}
                  </div>
                </div>
                {(module.attributes['version-statuses'] || []).length === 0 && (
                  <p className="text-xs text-green-400/70 mt-2 ml-7">
                    {t.rich('moduleDetail.vcsStatus.firstVersionHint', {
                      code: (chunks) => <code className="font-mono">{chunks}</code>,
                    })}
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
                    <h3 className="text-sm font-medium text-slate-300">{t('moduleDetail.links.title')}</h3>
                  </div>
                  <button
                    onClick={() => setShowLinkPicker(!showLinkPicker)}
                    className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 flex items-center gap-1"
                  >
                    <Plus size={12} />
                    {t('moduleDetail.links.linkWorkspace')}
                  </button>
                </div>

                <p className="text-xs text-slate-500 mb-3">
                  {t('moduleDetail.links.description')}
                </p>

                {showLinkPicker && (
                  <div className="mb-3">
                    <WorkspacePicker
                      placeholder={t('moduleDetail.links.searchPlaceholder')}
                      excludeIds={workspaceLinks.map((l) => l.attributes['workspace-id'])}
                      busyId={linkingWs}
                      onSelect={(ws) => handleLinkWorkspace(ws.id)}
                    />
                  </div>
                )}

                {workspaceLinks.length === 0 ? (
                  <p className="text-xs text-slate-500 italic">{t('moduleDetail.links.empty')}</p>
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
                          className="p-1.5 rounded-md text-slate-500 hover:text-red-400 hover:bg-slate-700/50 transition-colors"
                          title={t('moduleDetail.links.unlinkTitle')}
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
                <h3 className="text-sm font-medium text-slate-300">{t('meta.title')}</h3>
                {!editingMeta ? (
                  modPerms['can-update'] && <button onClick={startEditingMeta} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200">{t('meta.edit')}</button>
                ) : (
                  <div className="flex gap-2">
                    <button onClick={() => { setEditingMeta(false); setLockoutWarning('') }} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200">{t('meta.cancel')}</button>
                    <button onClick={() => handleSaveMeta()} disabled={savingMeta} className="px-2.5 py-1 rounded-md text-xs font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white">{savingMeta ? t('meta.saving') : t('meta.save')}</button>
                  </div>
                )}
              </div>
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <dt className="text-xs text-slate-500">{t('meta.owner')}</dt>
                  {editingMeta && isAdmin() ? (
                    <input type="email" value={editOwner} onChange={(e) => setEditOwner(e.target.value)} placeholder={t('meta.ownerPlaceholder')} className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{module.attributes['owner-email'] || t('meta.ownerNone')}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500 mb-1">{t('meta.labels')}</dt>
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
                      {t('meta.revertLabels')}
                    </button>
                    <button
                      onClick={() => handleSaveMeta(true)}
                      disabled={savingMeta}
                      className="px-3 py-1 rounded text-xs text-amber-200 hover:text-white bg-amber-700 hover:bg-amber-600"
                    >
                      {savingMeta ? t('meta.saving') : t('meta.saveAnyway')}
                    </button>
                  </div>
                </div>
              )}
            </div>

            {/* Inputs & Outputs */}
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden mb-6">
              <button
                onClick={() => setInterfaceExpanded(!interfaceExpanded)}
                className="w-full flex items-center justify-between px-5 py-4 text-left"
              >
                <div className="flex items-center gap-3">
                  <h3 className="text-sm font-semibold text-slate-200">{t('moduleDetail.interface.title')}</h3>
                  {interfaceData && (
                    <span className="text-xs text-slate-500">
                      {t('moduleDetail.interface.counts', { inputs: interfaceData.inputs?.length ?? 0, outputs: interfaceData.outputs?.length ?? 0 })}
                    </span>
                  )}
                </div>
                <svg
                  className={`w-4 h-4 text-slate-400 transition-transform ${interfaceExpanded ? 'rotate-180' : ''}`}
                  fill="none" stroke="currentColor" viewBox="0 0 24 24"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </button>

              {interfaceExpanded && (
                <div className="px-5 pb-5 space-y-4 border-t border-slate-700/50 pt-4">
                  {/* Version selector */}
                  <div className="flex items-center gap-2">
                    <label className="text-xs text-slate-500">{t('moduleDetail.interface.versionLabel')}</label>
                    <select
                      value={interfaceVersion}
                      onChange={(e) => { setInterfaceVersion(e.target.value); loadInterface(e.target.value) }}
                      className="px-2 py-1 text-xs border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                    >
                      {(module?.attributes['version-statuses'] || [])
                        .filter((v: VersionStatus) => v.status === 'uploaded')
                        .map((v: VersionStatus) => (
                          <option key={v.version} value={v.version}>{v.version}</option>
                        ))}
                    </select>
                  </div>

                  {interfaceLoading ? (
                    <LoadingSpinner />
                  ) : !interfaceData || (interfaceData.inputs === null && interfaceData.outputs === null) ? (
                    <p className="text-sm text-slate-500">{t('moduleDetail.interface.noData')}</p>
                  ) : (
                    <>
                      {/* Inputs table */}
                      {interfaceData.inputs && interfaceData.inputs.length > 0 && (
                        <div>
                          <h4 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">{t('moduleDetail.interface.inputs')}</h4>
                          <div className="overflow-x-auto">
                            <table className="w-full text-sm">
                              <thead>
                                <tr className="text-xs text-slate-500 border-b border-slate-700/50">
                                  <th className="text-left py-2 pr-4">{t('moduleDetail.interface.name')}</th>
                                  <th className="text-left py-2 pr-4">{t('moduleDetail.interface.type')}</th>
                                  <th className="text-left py-2 pr-4">{t('moduleDetail.interface.description')}</th>
                                  <th className="text-left py-2 pr-4">{t('moduleDetail.interface.default')}</th>
                                  <th className="text-left py-2">{t('moduleDetail.interface.required')}</th>
                                </tr>
                              </thead>
                              <tbody>
                                {interfaceData.inputs.map((inp) => (
                                  <tr key={inp.name} className="border-b border-slate-700/30">
                                    <td className="py-2 pr-4 font-mono text-xs text-slate-200">{inp.name}</td>
                                    <td className="py-2 pr-4 font-mono text-xs text-slate-400">{inp.type}</td>
                                    <td className="py-2 pr-4 text-slate-300">{inp.description}</td>
                                    <td className="py-2 pr-4 font-mono text-xs text-slate-400">{inp.default ?? '—'}</td>
                                    <td className="py-2">
                                      {inp.required ? (
                                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900/50 text-amber-300">{t('moduleDetail.interface.requiredBadge')}</span>
                                      ) : (
                                        <span className="text-xs text-slate-500">{t('moduleDetail.interface.optionalBadge')}</span>
                                      )}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        </div>
                      )}

                      {/* Outputs table */}
                      {interfaceData.outputs && interfaceData.outputs.length > 0 && (
                        <div>
                          <h4 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">{t('moduleDetail.interface.outputs')}</h4>
                          <div className="overflow-x-auto">
                            <table className="w-full text-sm">
                              <thead>
                                <tr className="text-xs text-slate-500 border-b border-slate-700/50">
                                  <th className="text-left py-2 pr-4">{t('moduleDetail.interface.name')}</th>
                                  <th className="text-left py-2 pr-4">{t('moduleDetail.interface.description')}</th>
                                  <th className="text-left py-2">{t('moduleDetail.interface.sensitive')}</th>
                                </tr>
                              </thead>
                              <tbody>
                                {interfaceData.outputs.map((out) => (
                                  <tr key={out.name} className="border-b border-slate-700/30">
                                    <td className="py-2 pr-4 font-mono text-xs text-slate-200">{out.name}</td>
                                    <td className="py-2 pr-4 text-slate-300">{out.description}</td>
                                    <td className="py-2">
                                      {out.sensitive && (
                                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-900/50 text-red-300">{t('moduleDetail.interface.sensitiveBadge')}</span>
                                      )}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        </div>
                      )}

                      {interfaceData.inputs?.length === 0 && interfaceData.outputs?.length === 0 && (
                        <p className="text-sm text-slate-500">{t('moduleDetail.interface.noneDeclared')}</p>
                      )}
                    </>
                  )}
                </div>
              )}
            </div>

            {/* Versions */}
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
              <button
                onClick={() => setVersionsExpanded(!versionsExpanded)}
                className="w-full flex items-center justify-between px-5 py-4 text-left"
              >
                <div className="flex items-center gap-3">
                  <h3 className="text-sm font-semibold text-slate-200">{t('moduleDetail.versions.title')}</h3>
                  <span className="text-xs text-slate-500">
                    {t('moduleDetail.versions.count', { count: (module.attributes['version-statuses'] || []).length })}
                  </span>
                </div>
                <svg
                  className={`w-4 h-4 text-slate-400 transition-transform ${versionsExpanded ? 'rotate-180' : ''}`}
                  fill="none" stroke="currentColor" viewBox="0 0 24 24"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </button>

              {versionsExpanded && (
                <div className="border-t border-slate-700/50">
                  {(module.attributes['version-statuses'] || []).length === 0 ? (
                    <div className="p-5">
                      <EmptyState message={
                        isVcsSource
                          ? t('moduleDetail.versions.emptyVcs', { pattern: module.attributes['vcs-tag-pattern'] || 'v*' })
                          : t('moduleDetail.versions.emptyUpload')
                      } />
                    </div>
                  ) : (
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-slate-700/50">
                          <th className="text-left px-4 py-3 text-slate-400 font-medium">{t('moduleDetail.versions.version')}</th>
                          <th className="text-left px-4 py-3 text-slate-400 font-medium">{t('moduleDetail.versions.status')}</th>
                          {isVcsSource && (
                            <th className="text-left px-4 py-3 text-slate-400 font-medium">{t('moduleDetail.versions.source')}</th>
                          )}
                          <th className="text-right px-4 py-3 text-slate-400 font-medium">{t('moduleDetail.versions.actions')}</th>
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
                                  <span className="text-slate-500 text-xs">{t('moduleDetail.versions.manualUpload')}</span>
                                )}
                              </td>
                            )}
                            {modPerms['can-destroy'] && (
                              <td className="px-4 py-3 text-right">
                                <button
                                  onClick={() => setDeleteTarget(v.version)}
                                  className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors"
                                >
                                  {t('moduleDetail.versions.delete')}
                                </button>
                              </td>
                            )}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )}
            </div>
          </>
        ) : null}

        {/* Delete confirmation dialog */}
        <Dialog.Root open={deleteTarget !== null} onOpenChange={(open) => { if (!open) setDeleteTarget(null) }}>
          <Dialog.Portal>
            <Dialog.Overlay className="fixed inset-0 bg-black/60" />
            <Dialog.Content className="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 bg-slate-800 rounded-lg border border-slate-700 p-6 w-full max-w-md shadow-xl">
              <Dialog.Title className="text-lg font-semibold text-slate-100">
                {t('moduleDetail.deleteDialog.title')}
              </Dialog.Title>
              <Dialog.Description className="text-sm text-slate-400 mt-2">
                {deleteTarget === 'module'
                  ? t('moduleDetail.deleteDialog.moduleBody')
                  : t('moduleDetail.deleteDialog.versionBody', { version: deleteTarget ?? '' })}
              </Dialog.Description>
              <div className="flex justify-end gap-3 mt-6">
                <button
                  onClick={() => setDeleteTarget(null)}
                  className="px-4 py-2 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-700 transition-colors"
                >
                  {t('moduleDetail.deleteDialog.cancel')}
                </button>
                <button
                  onClick={handleDelete}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-500 text-white transition-colors"
                >
                  {t('moduleDetail.deleteDialog.confirm')}
                </button>
              </div>
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
      </main>
    </>
  )
}
