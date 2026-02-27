'use client'

import { useEffect, useState } from 'react'
import { useParams, useRouter } from 'next/navigation'
import * as Dialog from '@radix-ui/react-dialog'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { getAuthState } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

interface VersionStatus {
  version: string
  status: string
}

interface ModuleDetail {
  id: string
  attributes: {
    name: string
    namespace: string
    provider: string
    status: string
    'version-statuses': VersionStatus[]
    'created-at': string | null
    'updated-at': string | null
  }
}

export default function ModuleDetailPage() {
  const router = useRouter()
  const params = useParams<{ org: string; namespace: string; name: string; provider: string }>()
  const { org, namespace, name, provider } = params

  const [module, setModule] = useState<ModuleDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Create version form
  const [showCreateVersion, setShowCreateVersion] = useState(false)
  const [newVersion, setNewVersion] = useState('')
  const [creating, setCreating] = useState(false)
  const [uploadUrl, setUploadUrl] = useState('')

  // Delete confirmation
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null) // 'module' or version string

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadModule()
  }, [router, org, namespace, name, provider])

  async function loadModule() {
    setLoading(true)
    try {
      const res = await apiFetch(
        `/api/v2/organizations/${org}/registry-modules/private/${namespace}/${name}/${provider}`
      )
      if (!res.ok) throw new Error('Module not found')
      const data = await res.json()
      setModule(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load module')
    } finally {
      setLoading(false)
    }
  }

  async function handleCreateVersion(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    setUploadUrl('')
    try {
      const res = await apiFetch(
        `/api/v2/organizations/${org}/registry-modules/private/${namespace}/${name}/${provider}/versions`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/vnd.api+json' },
          body: JSON.stringify({
            data: {
              type: 'registry-module-versions',
              attributes: { version: newVersion },
            },
          }),
        }
      )
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to create version (${res.status})`)
      }
      const data = await res.json()
      setUploadUrl(data.data?.links?.upload || '')
      setNewVersion('')
      setShowCreateVersion(false)
      await loadModule()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create version')
    } finally {
      setCreating(false)
    }
  }

  async function handleDelete() {
    if (!deleteTarget) return
    setError('')
    try {
      const path = deleteTarget === 'module'
        ? `/api/v2/organizations/${org}/registry-modules/private/${namespace}/${name}/${provider}`
        : `/api/v2/organizations/${org}/registry-modules/private/${namespace}/${name}/${provider}/${deleteTarget}`

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
              title={`${module.attributes.namespace}/${module.attributes.name}`}
              description={`Provider: ${module.attributes.provider}`}
              actions={
                <div className="flex gap-2">
                  <button
                    onClick={() => setShowCreateVersion(!showCreateVersion)}
                    className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors"
                  >
                    {showCreateVersion ? 'Cancel' : 'Add Version'}
                  </button>
                  <button
                    onClick={() => setDeleteTarget('module')}
                    className="px-4 py-2 rounded-lg text-sm font-medium bg-red-900/50 hover:bg-red-800/50 text-red-300 border border-red-800/50 transition-colors"
                  >
                    Delete Module
                  </button>
                </div>
              }
            />

            {uploadUrl && (
              <div className="mb-6 p-4 bg-green-900/30 rounded-lg border border-green-800/50">
                <p className="text-sm text-green-300 font-medium mb-2">Upload your module tarball to:</p>
                <div className="flex items-center gap-2">
                  <code className="flex-1 text-xs text-green-200 bg-green-900/30 p-2 rounded overflow-x-auto">
                    {uploadUrl}
                  </code>
                  <button
                    onClick={() => navigator.clipboard.writeText(uploadUrl)}
                    className="px-3 py-1 rounded text-xs font-medium bg-green-800/50 hover:bg-green-700/50 text-green-200 transition-colors flex-shrink-0"
                  >
                    Copy
                  </button>
                </div>
              </div>
            )}

            {showCreateVersion && (
              <form onSubmit={handleCreateVersion} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 flex items-end gap-3">
                <div className="flex-1">
                  <label htmlFor="ver" className="block text-sm font-medium text-slate-300 mb-1">Version</label>
                  <input
                    id="ver"
                    type="text"
                    value={newVersion}
                    onChange={(e) => setNewVersion(e.target.value)}
                    required
                    placeholder="1.0.0"
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                  />
                </div>
                <button
                  type="submit"
                  disabled={creating}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
                >
                  {creating ? 'Creating...' : 'Create Version'}
                </button>
              </form>
            )}

            <h2 className="text-lg font-semibold text-slate-200 mb-3">Versions</h2>
            {(module.attributes['version-statuses'] || []).length === 0 ? (
              <EmptyState message="No versions yet. Add one to get started." />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="text-left px-4 py-3 text-slate-400 font-medium">Version</th>
                      <th className="text-left px-4 py-3 text-slate-400 font-medium">Status</th>
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
                        <td className="px-4 py-3 text-right">
                          <button
                            onClick={() => setDeleteTarget(v.version)}
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
