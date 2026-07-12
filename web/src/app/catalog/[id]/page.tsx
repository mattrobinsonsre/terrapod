'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter, useParams } from 'next/navigation'
import { useTranslations } from 'next-intl'
import Link from 'next/link'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { LabelsEditor } from '@/components/labels-editor'
import { Modal } from '@/components/modal'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

interface CatalogItem {
  id: string
  attributes: {
    name: string
    'display-name': string
    description: string
    enabled: boolean
    'module-id': string
    'module-name': string
    'module-provider': string
    'default-version-pin': string | null
    'allowed-agent-pool-ids': string[] | null
  }
}

interface FormField {
  name: string
  type: string
  description: string
  required: boolean
  sensitive: boolean
  default: unknown
  options: string[] | null
  source: string
}

interface ProvisionForm {
  'resolved-version': string | null
  fields: FormField[]
}

interface Instance {
  id: string
  attributes: {
    name: string
    'catalog-item-id': string | null
    'catalog-version-pin': string | null
    'agent-pool-id': string | null
    'owner-email': string
    labels: Record<string, string>
    'input-values'?: Record<string, unknown>
  }
}

interface AgentPool {
  id: string
  attributes: { name: string }
}

// Coerce an unknown field default to the string the <input>/<select> needs.
function defaultStr(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'string') return v
  return String(v)
}

export default function CatalogItemPage() {
  const t = useTranslations('catalogDetail')
  const router = useRouter()
  const params = useParams()
  const itemId = params.id as string

  const [item, setItem] = useState<CatalogItem | null>(null)
  const [form, setForm] = useState<ProvisionForm | null>(null)
  const [instances, setInstances] = useState<Instance[]>([])
  const [pools, setPools] = useState<AgentPool[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notFound, setNotFound] = useState(false)

  // Provision form state
  const [provName, setProvName] = useState('')
  const [provPoolId, setProvPoolId] = useState('')
  const [provVersion, setProvVersion] = useState('') // '' => latest (float)
  const [provInputs, setProvInputs] = useState<Record<string, string>>({})
  const [provAutoApply, setProvAutoApply] = useState(false)
  const [provLabels, setProvLabels] = useState<Record<string, string>>({})
  const [provisioning, setProvisioning] = useState(false)
  const [provError, setProvError] = useState('')

  // Reconfigure modal state
  const [reconfigInstance, setReconfigInstance] = useState<Instance | null>(null)
  const [reconfigInputs, setReconfigInputs] = useState<Record<string, string>>({})
  const [reconfigVersion, setReconfigVersion] = useState('')
  const [reconfigAutoApply, setReconfigAutoApply] = useState(false)
  const [reconfigBusy, setReconfigBusy] = useState(false)
  const [reconfigError, setReconfigError] = useState('')

  // Destroy modal state
  const [destroyInstance, setDestroyInstance] = useState<Instance | null>(null)
  const [destroyAutoApply, setDestroyAutoApply] = useState(false)
  const [destroyBusy, setDestroyBusy] = useState(false)
  const [destroyError, setDestroyError] = useState('')

  // Orphan modal state (discouraged escape hatch — abandons infra)
  const [orphanInstance, setOrphanInstance] = useState<Instance | null>(null)
  const [orphanConfirm, setOrphanConfirm] = useState('')
  const [orphanBusy, setOrphanBusy] = useState(false)
  const [orphanError, setOrphanError] = useState('')
  const canOrphan = isAdmin()

  // Run-result banner after a lifecycle action.
  const [actionResult, setActionResult] = useState('')
  // A planned (non-auto-applied) run awaiting confirmation. The workspace clamp
  // gives the provisioner read-only on the workspace, so confirm/discard happen
  // here on the catalog surface rather than the workspace run API.
  const [pendingRun, setPendingRun] = useState<{ id: string; name: string } | null>(null)
  const [pendingBusy, setPendingBusy] = useState(false)

  async function runPending(action: 'confirm' | 'discard') {
    if (!pendingRun) return
    setPendingBusy(true)
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/catalog-instances/${pendingRun.id}/${action}`,
        { method: 'POST' },
      )
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(
          data.detail ||
            (action === 'confirm'
              ? t('errors.confirmStatus', { status: res.status })
              : t('errors.discardStatus', { status: res.status })),
        )
      }
      setActionResult(action === 'confirm' ? t('actions.confirmed') : t('actions.discarded'))
      setPendingRun(null)
      await loadInstances()
    } catch (err) {
      setActionResult(
        err instanceof Error
          ? err.message
          : action === 'confirm'
            ? t('errors.confirm')
            : t('errors.discard'),
      )
    } finally {
      setPendingBusy(false)
    }
  }

  const loadInstances = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/terrapod/v1/catalog-items/${itemId}/instances`)
      if (res.ok) {
        const data = await res.json()
        setInstances(data.data || [])
      }
    } catch {
      // instances are non-fatal for the page
    }
  }, [itemId])

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    let cancelled = false

    async function load() {
      try {
        const [itemRes, formRes, poolsRes] = await Promise.all([
          apiFetch(`/api/terrapod/v1/catalog-items/${itemId}`),
          apiFetch(`/api/terrapod/v1/catalog-items/${itemId}/form`),
          apiFetch('/api/terrapod/v1/agent-pools'),
        ])
        if (cancelled) return
        if (itemRes.status === 404) { setNotFound(true); return }
        if (!itemRes.ok) throw new Error(t('errors.loadItem'))
        const itemData = await itemRes.json()
        setItem(itemData.data)

        if (formRes.ok) {
          const formData = await formRes.json()
          const f: ProvisionForm = formData.data.attributes
          setForm(f)
          // Pre-fill provision inputs with field defaults.
          const init: Record<string, string> = {}
          for (const fld of f.fields) init[fld.name] = defaultStr(fld.default)
          setProvInputs(init)
        }

        if (poolsRes.ok) {
          const poolsData = await poolsRes.json()
          setPools(poolsData.data || [])
        }

        await loadInstances()
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : t('errors.loadItem'))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    load()
    return () => { cancelled = true }
  }, [itemId, router, loadInstances])

  // Pools the user may pick: when the item restricts pools, intersect with its
  // allow-list; otherwise any pool the API returned.
  const allowedPools = item?.attributes['allowed-agent-pool-ids'] ?? null
  const selectablePools = allowedPools === null
    ? pools
    : pools.filter((p) => allowedPools.includes(p.id))

  function renderField(
    field: FormField,
    value: string,
    onChange: (v: string) => void,
    idPrefix: string,
  ) {
    const id = `${idPrefix}-${field.name}`
    return (
      <div key={field.name}>
        <label htmlFor={id} className="block text-sm font-medium text-slate-300 mb-1">
          {field.name}
          {field.required && <span className="text-red-400 ml-0.5">*</span>}
          <span className="ml-2 text-[10px] uppercase tracking-wide text-slate-500">{field.source}</span>
        </label>
        {field.description && (
          <p className="text-xs text-slate-500 mb-1">{field.description}</p>
        )}
        {field.options && field.options.length > 0 ? (
          <select
            id={id}
            value={value}
            required={field.required}
            onChange={(e) => onChange(e.target.value)}
            className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
          >
            <option value="">{t('field.select')}</option>
            {field.options.map((opt) => (
              <option key={opt} value={opt}>{opt}</option>
            ))}
          </select>
        ) : (
          <input
            id={id}
            type={field.sensitive ? 'password' : 'text'}
            value={value}
            required={field.required}
            onChange={(e) => onChange(e.target.value)}
            className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
          />
        )}
      </div>
    )
  }

  async function handleProvision(e: React.FormEvent) {
    e.preventDefault()
    setProvisioning(true)
    setProvError('')
    try {
      const attrs: Record<string, unknown> = {
        name: provName,
        'agent-pool-id': provPoolId,
        'input-values': provInputs,
        'auto-apply': provAutoApply,
      }
      if (provVersion) attrs['version-pin'] = provVersion
      if (Object.keys(provLabels).length > 0) attrs.labels = provLabels

      const res = await apiFetch(`/api/terrapod/v1/catalog-items/${itemId}/provision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'catalog-instances', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.provisionStatus', { status: res.status }))
      }
      const data = await res.json()
      const newId = data.data?.id
      const provisionedName = data.data?.attributes?.name || provName
      if (newId && provAutoApply) {
        // Auto-apply: navigate to the workspace to watch the run.
        router.push(`/workspaces/${newId}`)
        return
      }
      // Plan-only provision: the run is planned and the provisioner can't
      // confirm it via the workspace API (clamp), so offer confirm/discard here.
      setProvName('')
      setProvLabels({})
      await loadInstances()
      if (newId) {
        setPendingRun({ id: newId, name: provisionedName })
        setActionResult(t('actions.provisioned', { name: provisionedName }))
      }
    } catch (err) {
      setProvError(err instanceof Error ? err.message : t('errors.provision'))
    } finally {
      setProvisioning(false)
    }
  }

  function openReconfigure(inst: Instance) {
    setReconfigInstance(inst)
    setReconfigError('')
    setReconfigAutoApply(false)
    setReconfigVersion(inst.attributes['catalog-version-pin'] || '')
    // Pre-fill from the instance's stored input-values, falling back to form
    // defaults for any field not yet set.
    const init: Record<string, string> = {}
    for (const fld of form?.fields || []) init[fld.name] = defaultStr(fld.default)
    const stored = inst.attributes['input-values'] || {}
    for (const [k, v] of Object.entries(stored)) init[k] = defaultStr(v)
    setReconfigInputs(init)
  }

  // The instances list endpoint omits input-values; fetch the full instance so
  // the reconfigure modal can pre-fill from current inputs.
  async function startReconfigure(inst: Instance) {
    try {
      const res = await apiFetch(`/api/terrapod/v1/catalog-instances/${inst.id}`)
      if (res.ok) {
        const data = await res.json()
        openReconfigure({ ...inst, attributes: { ...inst.attributes, ...data.data.attributes } })
        return
      }
    } catch {
      // fall through to opening with what we have
    }
    openReconfigure(inst)
  }

  async function handleReconfigure(e: React.FormEvent) {
    e.preventDefault()
    if (!reconfigInstance) return
    setReconfigBusy(true)
    setReconfigError('')
    try {
      const attrs: Record<string, unknown> = {
        'input-values': reconfigInputs,
        'auto-apply': reconfigAutoApply,
        // null version-pin => float to latest; value => pin.
        'version-pin': reconfigVersion || null,
      }
      const res = await apiFetch(`/api/terrapod/v1/catalog-instances/${reconfigInstance.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'catalog-instances', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.reconfigureStatus', { status: res.status }))
      }
      const data = await res.json()
      const status = data.data?.attributes?.status
      setActionResult(
        status
          ? t('actions.reconfigureQueuedStatus', { status })
          : t('actions.reconfigureQueued'),
      )
      if (status === 'planned') {
        setPendingRun({ id: reconfigInstance.id, name: reconfigInstance.attributes.name })
      }
      setReconfigInstance(null)
      await loadInstances()
    } catch (err) {
      setReconfigError(err instanceof Error ? err.message : t('errors.reconfigure'))
    } finally {
      setReconfigBusy(false)
    }
  }

  async function handleDestroy() {
    if (!destroyInstance) return
    setDestroyBusy(true)
    setDestroyError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/catalog-instances/${destroyInstance.id}/destroy`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'catalog-instances', attributes: { 'auto-apply': destroyAutoApply } } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.destroyStatus', { status: res.status }))
      }
      const data = await res.json()
      const status = data.data?.attributes?.status
      setActionResult(
        status
          ? t('actions.destroyQueuedStatus', { status })
          : t('actions.destroyQueued'),
      )
      if (status === 'planned') {
        setPendingRun({ id: destroyInstance.id, name: destroyInstance.attributes.name })
      }
      setDestroyInstance(null)
      setDestroyAutoApply(false)
      await loadInstances()
    } catch (err) {
      setDestroyError(err instanceof Error ? err.message : t('errors.destroy'))
    } finally {
      setDestroyBusy(false)
    }
  }

  async function handleOrphan() {
    if (!orphanInstance) return
    setOrphanBusy(true)
    setOrphanError('')
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/catalog-instances/${orphanInstance.id}?orphan=true`,
        { method: 'DELETE' },
      )
      if (!res.ok && res.status !== 204) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.orphanStatus', { status: res.status }))
      }
      setActionResult(t('actions.orphaned', { name: orphanInstance.attributes.name }))
      setOrphanInstance(null)
      setOrphanConfirm('')
      await loadInstances()
    } catch (err) {
      setOrphanError(err instanceof Error ? err.message : t('errors.orphan'))
    } finally {
      setOrphanBusy(false)
    }
  }

  if (loading) {
    return (
      <>
        <NavBar />
        <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main>
      </>
    )
  }

  if (notFound) {
    return (
      <>
        <NavBar />
        <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
          <div className="p-4 bg-slate-800/50 text-slate-400 rounded-lg text-sm border border-slate-700/50">
            {t('notFound')}{' '}
            <Link href="/catalog" className="text-brand-400 hover:text-brand-300">{t('backToCatalog')}</Link>
          </div>
        </main>
      </>
    )
  }

  const a = item?.attributes

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <div className="mb-2">
          <Link href="/catalog" className="text-sm text-brand-400 hover:text-brand-300">{t('backLink')}</Link>
        </div>
        <PageHeader
          title={a?.['display-name'] || a?.name || t('fallbackTitle')}
          description={a?.description || undefined}
        />

        {error && <ErrorBanner message={error} />}
        {actionResult && (
          <div className="mb-4 p-3 bg-green-900/30 text-green-400 rounded-lg text-sm border border-green-800/50">{actionResult}</div>
        )}
        {pendingRun && (
          <div className="mb-4 p-3 bg-amber-900/20 text-amber-200 rounded-lg text-sm border border-amber-800/50 flex items-center justify-between gap-3">
            <span>
              {t.rich('pending.banner', {
                name: pendingRun.name,
                strong: (chunks) => <span className="font-medium">{chunks}</span>,
              })}
            </span>
            <span className="flex gap-2 shrink-0">
              <button
                type="button"
                onClick={() => runPending('confirm')}
                disabled={pendingBusy}
                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-slate-700 disabled:text-slate-500 text-white transition-colors"
              >
                {pendingBusy ? t('pending.working') : t('pending.confirmApply')}
              </button>
              <button
                type="button"
                onClick={() => runPending('discard')}
                disabled={pendingBusy}
                className="px-3 py-1.5 rounded-lg text-xs font-medium text-slate-300 hover:text-slate-100 transition-colors"
              >
                {t('pending.discard')}
              </button>
            </span>
          </div>
        )}

        {a && (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 flex flex-wrap gap-x-6 gap-y-2 text-sm">
            <div>
              <span className="text-slate-500">{t('info.module')} </span>
              <span className="text-slate-200">{a['module-name']}{a['module-provider'] ? `/${a['module-provider']}` : ''}</span>
            </div>
            <div>
              <span className="text-slate-500">{t('info.resolvedVersion')} </span>
              <span className="text-slate-200">{form?.['resolved-version'] || t('info.latest')}</span>
            </div>
            {!a.enabled && (
              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900/50 text-amber-300">{t('info.disabled')}</span>
            )}
          </div>
        )}

        {/* Provision panel */}
        <section className="mb-8">
          <h2 className="text-lg font-semibold text-slate-100 mb-3">{t('provision.title')}</h2>
          <form onSubmit={handleProvision} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 space-y-3">
            {provError && <ErrorBanner message={provError} />}
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div>
                <label htmlFor="prov-name" className="block text-sm font-medium text-slate-300 mb-1">{t('provision.workspaceName')}<span className="text-red-400 ml-0.5">*</span></label>
                <input
                  id="prov-name"
                  type="text"
                  value={provName}
                  onChange={(e) => setProvName(e.target.value)}
                  required
                  pattern="[a-zA-Z0-9][a-zA-Z0-9_\-]*"
                  title={t('provision.workspaceNameTitle')}
                  placeholder={t('provision.workspaceNamePlaceholder')}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
              <div>
                <label htmlFor="prov-pool" className="block text-sm font-medium text-slate-300 mb-1">{t('provision.agentPool')}<span className="text-red-400 ml-0.5">*</span></label>
                <select
                  id="prov-pool"
                  value={provPoolId}
                  required
                  onChange={(e) => setProvPoolId(e.target.value)}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                >
                  <option value="">{t('provision.selectPool')}</option>
                  {selectablePools.map((p) => (
                    <option key={p.id} value={p.id}>{p.attributes.name}</option>
                  ))}
                </select>
                {selectablePools.length === 0 && (
                  <p className="mt-1 text-xs text-amber-400">
                    {pools.length === 0
                      ? t('provision.noPoolsAvailable')
                      : t('provision.noPoolsAllowed')}
                  </p>
                )}
              </div>
              <div>
                <label htmlFor="prov-version" className="block text-sm font-medium text-slate-300 mb-1">{t('provision.version')}</label>
                <select
                  id="prov-version"
                  value={provVersion}
                  onChange={(e) => setProvVersion(e.target.value)}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                >
                  <option value="">{t('provision.latestFloat')}</option>
                  {a?.['default-version-pin'] && (
                    <option value={a['default-version-pin']}>{t('provision.defaultPinOption', { version: a['default-version-pin'] })}</option>
                  )}
                </select>
              </div>
            </div>

            {/* Dynamic form fields from /form */}
            {form && form.fields.length > 0 && (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-2 border-t border-slate-700/50">
                {form.fields.map((field) =>
                  renderField(
                    field,
                    provInputs[field.name] ?? '',
                    (v) => setProvInputs((prev) => ({ ...prev, [field.name]: v })),
                    'prov',
                  ),
                )}
              </div>
            )}

            <div className="pt-2">
              <label className="block text-sm font-medium text-slate-300 mb-1">{t('provision.labelsOptional')}</label>
              <LabelsEditor labels={provLabels} onChange={setProvLabels} />
            </div>

            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={provAutoApply}
                onChange={(e) => setProvAutoApply(e.target.checked)}
                className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
              />
              <span className="text-sm text-amber-300">{t('provision.autoApplyHint')}</span>
            </label>

            <button
              type="submit"
              disabled={provisioning}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors btn-smoke"
            >
              {provisioning ? t('provision.provisioning') : t('provision.submit')}
            </button>
          </form>
        </section>

        {/* Instances */}
        <section>
          <h2 className="text-lg font-semibold text-slate-100 mb-3">{t('instances.title')}</h2>
          {instances.length === 0 ? (
            <EmptyState message={t('instances.empty')} />
          ) : (
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-slate-700/50">
                    <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">{t('instances.colName')}</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden sm:table-cell">{t('instances.colVersion')}</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden md:table-cell">{t('instances.colPool')}</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">{t('instances.colActions')}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-700/30">
                  {instances.map((inst) => {
                    const poolName = pools.find((p) => p.id === inst.attributes['agent-pool-id'])?.attributes.name
                    return (
                      <tr key={inst.id} className="hover:bg-slate-700/20 transition-colors">
                        <td className="px-4 py-3">
                          <Link href={`/workspaces/${inst.id}`} className="text-sm font-medium text-brand-400 hover:text-brand-300">
                            {inst.attributes.name}
                          </Link>
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-400 hidden sm:table-cell">
                          {inst.attributes['catalog-version-pin'] || t('info.latest')}
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-400 hidden md:table-cell">
                          {poolName || inst.attributes['agent-pool-id'] || '—'}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <div className="flex justify-end gap-2">
                            <button onClick={() => startReconfigure(inst)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200">{t('instances.reconfigure')}</button>
                            <button onClick={() => { setDestroyInstance(inst); setDestroyError(''); setDestroyAutoApply(false) }} className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300">{t('instances.destroy')}</button>
                            {canOrphan && (
                              <button onClick={() => { setOrphanInstance(inst); setOrphanError(''); setOrphanConfirm('') }} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-400" title={t('instances.orphanTitle')}>{t('instances.orphan')}</button>
                            )}
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </main>

      {/* Reconfigure modal */}
      {reconfigInstance && (
        <Modal
          open
          onClose={() => setReconfigInstance(null)}
          title={t('reconfigure.title', { name: reconfigInstance.attributes.name })}
          panelClassName="bg-slate-800 rounded-lg border border-slate-700 w-full max-w-2xl max-h-[90vh] overflow-y-auto p-5"
        >
          <div>
            <h3 className="text-lg font-semibold text-slate-100 mb-1">{t('reconfigure.title', { name: reconfigInstance.attributes.name })}</h3>
            <p className="text-sm text-slate-400 mb-4">{t('reconfigure.description')}</p>
            {reconfigError && <ErrorBanner message={reconfigError} />}
            <form onSubmit={handleReconfigure} className="space-y-3">
              <div>
                <label htmlFor="recfg-version" className="block text-sm font-medium text-slate-300 mb-1">{t('provision.version')}</label>
                <select
                  id="recfg-version"
                  value={reconfigVersion}
                  onChange={(e) => setReconfigVersion(e.target.value)}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                >
                  <option value="">{t('provision.latestFloat')}</option>
                  {a?.['default-version-pin'] && (
                    <option value={a['default-version-pin']}>{t('provision.defaultPinOption', { version: a['default-version-pin'] })}</option>
                  )}
                  {reconfigInstance.attributes['catalog-version-pin'] &&
                    reconfigInstance.attributes['catalog-version-pin'] !== a?.['default-version-pin'] && (
                    <option value={reconfigInstance.attributes['catalog-version-pin'] as string}>
                      {t('reconfigure.currentOption', { version: reconfigInstance.attributes['catalog-version-pin'] as string })}
                    </option>
                  )}
                </select>
              </div>
              {form && form.fields.length > 0 && (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {form.fields.map((field) =>
                    renderField(
                      field,
                      reconfigInputs[field.name] ?? '',
                      (v) => setReconfigInputs((prev) => ({ ...prev, [field.name]: v })),
                      'recfg',
                    ),
                  )}
                </div>
              )}
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={reconfigAutoApply}
                  onChange={(e) => setReconfigAutoApply(e.target.checked)}
                  className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
                />
                <span className="text-sm text-amber-300">{t('provision.autoApplyHint')}</span>
              </label>
              <div className="flex justify-end gap-2 pt-2">
                <button type="button" onClick={() => setReconfigInstance(null)} className="px-4 py-2 rounded-lg text-sm font-medium text-slate-400 hover:text-slate-200 transition-colors">{t('common.cancel')}</button>
                <button type="submit" disabled={reconfigBusy} className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {reconfigBusy ? t('reconfigure.queuing') : t('reconfigure.queueRun')}
                </button>
              </div>
            </form>
          </div>
        </Modal>
      )}

      {/* Destroy confirm modal */}
      {destroyInstance && (
        <Modal
          open
          onClose={() => setDestroyInstance(null)}
          title={t('destroy.title', { name: destroyInstance.attributes.name })}
          panelClassName="bg-slate-800 rounded-lg border border-slate-700 w-full max-w-md p-5"
        >
          <div>
            <h3 className="text-lg font-semibold text-slate-100 mb-1">{t('destroy.heading', { name: destroyInstance.attributes.name })}</h3>
            <p className="text-sm text-slate-400 mb-4">
              {t('destroy.description')}
            </p>
            {destroyError && <ErrorBanner message={destroyError} />}
            <label className="flex items-center gap-2 cursor-pointer mb-4">
              <input
                type="checkbox"
                checked={destroyAutoApply}
                onChange={(e) => setDestroyAutoApply(e.target.checked)}
                className="rounded border-slate-600 bg-slate-700 text-red-600 focus:ring-red-500"
              />
              <span className="text-sm text-amber-300">{t('destroy.autoApplyHint')}</span>
            </label>
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => setDestroyInstance(null)} className="px-4 py-2 rounded-lg text-sm font-medium text-slate-400 hover:text-slate-200 transition-colors">{t('common.cancel')}</button>
              <button type="button" onClick={handleDestroy} disabled={destroyBusy} className="px-4 py-2 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-500 disabled:bg-red-900 disabled:text-red-400 text-white transition-colors">
                {destroyBusy ? t('reconfigure.queuing') : t('instances.destroy')}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {orphanInstance && (
        <Modal
          open
          onClose={() => { setOrphanInstance(null); setOrphanConfirm('') }}
          title={t('orphan.title', { name: orphanInstance.attributes.name })}
          panelClassName="bg-slate-800 rounded-lg border border-red-900/60 w-full max-w-md p-5"
        >
          <div>
            <h3 className="text-lg font-semibold text-slate-100 mb-1">{t('orphan.heading', { name: orphanInstance.attributes.name })}</h3>
            <p className="text-sm text-slate-400 mb-3">
              {t.rich('orphan.description', {
                warn: (chunks) => <span className="text-amber-300 font-medium">{chunks}</span>,
                emph: (chunks) => <span className="text-slate-200 font-medium">{chunks}</span>,
              })}
            </p>
            {orphanError && <ErrorBanner message={orphanError} />}
            <label className="block text-xs text-slate-400 mb-1">
              {t.rich('orphan.confirmPrompt', {
                name: orphanInstance.attributes.name,
                mono: (chunks) => <span className="text-slate-200 font-mono">{chunks}</span>,
              })}
            </label>
            <input
              type="text"
              value={orphanConfirm}
              onChange={(e) => setOrphanConfirm(e.target.value)}
              className="w-full mb-4 rounded-lg bg-slate-900 border border-slate-700 px-3 py-2 text-sm text-slate-100 focus:ring-1 focus:ring-red-500 focus:border-red-500"
              placeholder={orphanInstance.attributes.name}
            />
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => { setOrphanInstance(null); setOrphanConfirm('') }} className="px-4 py-2 rounded-lg text-sm font-medium text-slate-400 hover:text-slate-200 transition-colors">{t('common.cancel')}</button>
              <button
                type="button"
                onClick={handleOrphan}
                disabled={orphanBusy || orphanConfirm !== orphanInstance.attributes.name}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-red-700 hover:bg-red-600 disabled:bg-slate-700 disabled:text-slate-500 text-white transition-colors"
              >
                {orphanBusy ? t('orphan.orphaning') : t('orphan.submit')}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </>
  )
}
