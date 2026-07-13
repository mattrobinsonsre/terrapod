'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { LabelsEditor } from '@/components/labels-editor'
import { getAuthState, isAdmin } from '@/lib/auth'
import { useConfirm } from '@/lib/use-confirm'
import { apiFetch } from '@/lib/api'

interface ProviderTemplate {
  id: string
  attributes: {
    name: string
    'provider-type': string
    body: string
    parameters: unknown[]
    labels: Record<string, string>
    'owner-email': string
  }
}

const EMPTY_FORM = {
  name: '',
  providerType: '',
  body: '',
  parametersJson: '[]',
  labels: {} as Record<string, string>,
}

export default function ProviderTemplatesPage() {
  const tr = useTranslations('adminProviderTemplates')
  const router = useRouter()
  const { confirmDelete } = useConfirm()
  const [templates, setTemplates] = useState<ProviderTemplate[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [disabled, setDisabled] = useState(false)

  const [showForm, setShowForm] = useState(false)
  const [editId, setEditId] = useState<string | null>(null)
  const [f, setF] = useState({ ...EMPTY_FORM })
  const [saving, setSaving] = useState(false)


  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadTemplates()
  }, [router])

  async function loadTemplates() {
    try {
      const res = await apiFetch('/api/terrapod/v1/provider-templates')
      if (res.status === 404) {
        setDisabled(true)
        setTemplates([])
        return
      }
      if (!res.ok) throw new Error(tr('errors.load'))
      const data = await res.json()
      setDisabled(false)
      setTemplates(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : tr('errors.load'))
    } finally {
      setLoading(false)
    }
  }

  function resetForm() {
    setF({ ...EMPTY_FORM })
    setEditId(null)
  }

  function startCreate() {
    resetForm()
    setShowForm(true)
    setError(''); setSuccess('')
  }

  function startEdit(t: ProviderTemplate) {
    const a = t.attributes
    setEditId(t.id)
    setF({
      name: a.name,
      providerType: a['provider-type'],
      body: a.body || '',
      parametersJson: JSON.stringify(a.parameters || [], null, 2),
      labels: a.labels || {},
    })
    setShowForm(true)
    setError(''); setSuccess('')
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError(''); setSuccess('')
    try {
      const editing = editId !== null

      let parameters: unknown
      try {
        parameters = JSON.parse(f.parametersJson || '[]')
      } catch {
        throw new Error(tr('errors.paramsInvalidJson'))
      }
      if (!Array.isArray(parameters)) {
        throw new Error(tr('errors.paramsNotArray'))
      }

      const attrs: Record<string, unknown> = {
        name: f.name,
        'provider-type': f.providerType,
        body: f.body,
        parameters,
        labels: f.labels,
      }

      const url = editing
        ? `/api/terrapod/v1/provider-templates/${editId}`
        : '/api/terrapod/v1/provider-templates'
      const res = await apiFetch(url, {
        method: editing ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'provider-templates', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || (editing
          ? tr('errors.updateStatus', { status: res.status })
          : tr('errors.createStatus', { status: res.status })))
      }
      setSuccess(editing
        ? tr('success.updated', { name: f.name })
        : tr('success.created', { name: f.name }))
      resetForm()
      setShowForm(false)
      await loadTemplates()
    } catch (err) {
      setError(err instanceof Error ? err.message : tr('errors.save'))
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete(id: string) {
    if (!confirmDelete(tr('confirm.delete'))) return
    setError(''); setSuccess('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/provider-templates/${id}`, { method: 'DELETE' })
      if (res.status === 409) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || tr('errors.deleteConflict'))
      }
      if (!res.ok) throw new Error(tr('errors.delete'))
      setSuccess(tr('success.deleted'))
      await loadTemplates()
    } catch (err) {
      setError(err instanceof Error ? err.message : tr('errors.delete'))
    }
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={tr('title')}
          description={tr('description')}
          actions={!disabled ? (
            <button
              onClick={() => { if (showForm) { setShowForm(false); resetForm() } else startCreate() }}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showForm ? tr('actions.cancel') : tr('actions.new')}
            </button>
          ) : undefined}
        />

        {error && <ErrorBanner message={error} />}
        {success && (
          <div className="mb-4 p-3 bg-green-900/30 text-green-400 rounded-lg text-sm border border-green-800/50">{success}</div>
        )}

        {disabled ? (
          <div className="p-4 bg-slate-800/50 text-slate-400 rounded-lg text-sm border border-slate-700/50">
            {tr('notEnabled')}
          </div>
        ) : (
          <>
            {showForm && (
              <form onSubmit={handleSubmit} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-4">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="pt-name" className="block text-sm font-medium text-slate-300 mb-1">{tr('form.name')}</label>
                    <input id="pt-name" type="text" value={f.name}
                      onChange={(e) => setF({ ...f, name: e.target.value })} required
                      placeholder="aws-default"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="pt-type" className="block text-sm font-medium text-slate-300 mb-1">{tr('form.providerType')}</label>
                    <input id="pt-type" type="text" value={f.providerType}
                      onChange={(e) => setF({ ...f, providerType: e.target.value })} required
                      placeholder="aws"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                </div>

                <div>
                  <label htmlFor="pt-body" className="block text-sm font-medium text-slate-300 mb-1">{tr('form.body')}</label>
                  <textarea id="pt-body" value={f.body} rows={8}
                    onChange={(e) => setF({ ...f, body: e.target.value })} required
                    placeholder={'provider "aws" {\n  region = var.region\n}'}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 font-mono text-xs focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                </div>

                <div>
                  <label htmlFor="pt-params" className="block text-sm font-medium text-slate-300 mb-1">{tr('form.parameters')}</label>
                  <textarea id="pt-params" value={f.parametersJson} rows={5}
                    onChange={(e) => setF({ ...f, parametersJson: e.target.value })}
                    placeholder='[{"name": "region", "type": "string", "required": true}]'
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 font-mono text-xs focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  <p className="mt-1 text-xs text-slate-500">{tr.rich('form.parametersHint', { code: (chunks) => <code>{chunks}</code> })}</p>
                </div>

                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">{tr('form.labels')}</label>
                  <LabelsEditor labels={f.labels} onChange={(labels) => setF({ ...f, labels })} />
                </div>

                <button type="submit" disabled={saving}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {saving ? (editId !== null ? tr('actions.saving') : tr('actions.creating')) : (editId !== null ? tr('actions.saveChanges') : tr('actions.create'))}
                </button>
              </form>
            )}

            {loading ? (
              <LoadingSpinner />
            ) : templates.length === 0 ? (
              <EmptyState message={tr('empty')} />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">{tr('table.name')}</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden sm:table-cell">{tr('table.providerType')}</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden md:table-cell">{tr('table.parameters')}</th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">{tr('table.actions')}</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {templates.map((t) => (
                      <tr key={t.id} className="hover:bg-slate-700/20 transition-colors">
                        <td className="px-4 py-3 text-sm text-slate-200">{t.attributes.name}</td>
                        <td className="px-4 py-3 hidden sm:table-cell">
                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-700 text-slate-300">
                            {t.attributes['provider-type']}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-400 hidden md:table-cell">
                          {(t.attributes.parameters || []).length}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <div className="flex justify-end gap-2">
                            <button onClick={() => startEdit(t)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors">{tr('actions.edit')}</button>
                            <button onClick={() => handleDelete(t.id)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors">{tr('actions.delete')}</button>
                          </div>
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
