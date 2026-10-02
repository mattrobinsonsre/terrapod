'use client'

import { useEffect, useState, useCallback, useRef } from 'react'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { VCSConsumption, type ConnectionConsumption } from '@/components/vcs-consumption'
import { LabelsEditor } from '@/components/labels-editor'
import { StringListEditor } from '@/components/template-editors'
import { getAuthState, isAdmin } from '@/lib/auth'
import { useConfirm } from '@/lib/use-confirm'
import { apiFetch, fetchAllPages } from '@/lib/api'
import { useSortable } from '@/lib/use-sortable'
import { usePollingInterval } from '@/lib/use-polling-interval'
import { useFormat } from '@/lib/format'

interface VCSConnection {
  id: string
  attributes: {
    name: string
    provider: string
    'server-url': string
    status: string
    'github-app-id': string | null
    'github-installation-id': string | null
    'github-account-login': string | null
    'has-token': boolean
    'has-webhook-secret'?: boolean
    'created-at': string
    // GHSA-v8g7-pqrj-8mcm. Who may point a workspace at this connection, and
    // where it may be pointed. None of the three is a secret, so all three are
    // returned on read — unlike the credential, which stays write-only. Typed
    // optional because a lagging server omits them entirely.
    'owner-email'?: string
    labels?: Record<string, string>
    'allowed-repositories'?: string[]
  } & ConnectionConsumption
}

// An empty allowlist means "any repository", so the count that decides which of
// the two states to show has to ignore the blank row StringListEditor adds when
// Add is pressed — the server trims those away, and a UI that counted them
// would claim a restriction that does not exist.
function nonBlank(values: string[]): string[] {
  return values.map((v) => v.trim()).filter(Boolean)
}

type VCSSortKey = 'name' | 'provider' | 'server-url' | 'status' | 'created'

export default function VCSConnectionsPage() {
  const router = useRouter()
  const t = useTranslations('adminVcs')
  const fmt = useFormat()
  const { confirmDelete } = useConfirm()
  const [connections, setConnections] = useState<VCSConnection[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  // Create form
  const [showCreate, setShowCreate] = useState(false)
  const [provider, setProvider] = useState<'github' | 'gitlab'>('github')
  const [name, setName] = useState('')
  const [serverUrl, setServerUrl] = useState('')
  // GitHub fields
  const [appId, setAppId] = useState('')
  const [installationId, setInstallationId] = useState('')
  const [privateKey, setPrivateKey] = useState('')
  const [webhookSecret, setWebhookSecret] = useState('')
  const [pemDragOver, setPemDragOver] = useState(false)
  const pemFileRef = useRef<HTMLInputElement>(null)
  // GitLab fields
  const [token, setToken] = useState('')
  // GHSA-v8g7-pqrj-8mcm. Access control, shared by both providers.
  const [ownerEmail, setOwnerEmail] = useState('')
  const [connLabels, setConnLabels] = useState<Record<string, string>>({})
  const [allowedRepos, setAllowedRepos] = useState<string[]>([])
  const [creating, setCreating] = useState(false)
  // When set, the form is editing this connection (PATCH) rather than creating.
  const [editId, setEditId] = useState<string | null>(null)

  // Delete confirmation

  function resetForm() {
    setName(''); setServerUrl(''); setAppId(''); setInstallationId('')
    setPrivateKey(''); setToken(''); setWebhookSecret(''); setProvider('github'); setEditId(null)
    setOwnerEmail(''); setConnLabels({}); setAllowedRepos([])
  }

  function startEdit(conn: VCSConnection) {
    setEditId(conn.id)
    setName(conn.attributes.name)
    setProvider(conn.attributes.provider as 'github' | 'gitlab')
    setServerUrl(conn.attributes['server-url'] || '')
    setAppId(conn.attributes['github-app-id'] ? String(conn.attributes['github-app-id']) : '')
    setInstallationId(
      conn.attributes['github-installation-id']
        ? String(conn.attributes['github-installation-id'])
        : '',
    )
    // Credentials are write-only — never returned. Leave blank = keep.
    setPrivateKey('')
    setToken('')
    setWebhookSecret('')
    // Access control is readable, so the form opens on the stored values rather
    // than on blanks — otherwise saving an unrelated field (a rotated key, a new
    // server URL) would silently clear the owner, the labels and the allowlist,
    // widening who may use the connection.
    setOwnerEmail(conn.attributes['owner-email'] || '')
    setConnLabels({ ...(conn.attributes.labels || {}) })
    setAllowedRepos([...(conn.attributes['allowed-repositories'] || [])])
    setShowCreate(true)
    setError(''); setSuccess('')
  }

  const vcsAccessor = useCallback((item: VCSConnection, key: VCSSortKey) => {
    switch (key) {
      case 'name': return item.attributes.name
      case 'provider': return item.attributes.provider
      case 'server-url': return item.attributes['server-url']
      case 'status': return item.attributes.status
      case 'created': return item.attributes['created-at']
    }
  }, [])

  const { sortedItems, sortState, toggleSort } = useSortable<VCSConnection, VCSSortKey>(
    connections, 'name', 'asc', vcsAccessor,
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadConnections()
  // eslint-disable-next-line react-hooks/exhaustive-deps -- initial mount load; the loader is a hoisted function declaration recreated each render, so depending on it would re-fetch on every render
  }, [router])

  usePollingInterval(!loading, 60_000, loadConnections)

  async function loadConnections() {
    try {
      setConnections(await fetchAllPages<VCSConnection>('/api/terrapod/v1/vcs-connections'))
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.load'))
    } finally {
      setLoading(false)
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    setSuccess('')
    try {
      const editing = editId !== null
      const attrs: Record<string, unknown> = { name }
      if (!editing) attrs.provider = provider
      if (serverUrl || editing) attrs['server-url'] = serverUrl
      if (provider === 'github') {
        attrs['github-app-id'] = appId
        attrs['github-installation-id'] = installationId
        // On edit, credentials are optional — only send when rotating.
        if (privateKey) attrs['private-key'] = privateKey
        else if (!editing) attrs['private-key'] = privateKey
        // Optional per-connection webhook secret — only send when set/rotating.
        if (webhookSecret) attrs['webhook-secret'] = webhookSecret
      } else {
        if (token) attrs.token = token
        else if (!editing) attrs.token = token
      }
      // GHSA-v8g7-pqrj-8mcm. Sent on create and on every edit. PATCH applies a
      // key only when present, so sending all three is what makes the form
      // authoritative: the fields were loaded from the server in startEdit, so
      // this writes back what the operator sees. An explicitly empty value
      // clears the field — a cleared allowlist has to mean "any repository
      // again", or an allowlist could never be undone.
      attrs['owner-email'] = ownerEmail.trim()
      attrs.labels = connLabels
      attrs['allowed-repositories'] = nonBlank(allowedRepos)
      const url = editing
        ? `/api/terrapod/v1/vcs-connections/${editId}`
        : '/api/terrapod/v1/vcs-connections'
      const res = await apiFetch(url, {
        method: editing ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'vcs-connections', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(
          data.detail ||
            (editing
              ? t('errors.updateStatus', { status: res.status })
              : t('errors.createStatus', { status: res.status })),
        )
      }
      setSuccess(editing ? t('success.updated', { name }) : t('success.created', { name }))
      resetForm()
      setShowCreate(false)
      await loadConnections()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.save'))
    } finally {
      setCreating(false)
    }
  }

  async function handleDelete(id: string) {
    if (!confirmDelete(t('confirmDelete'))) return
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/vcs-connections/${id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(t('errors.delete'))
      setSuccess(t('success.deleted'))
      await loadConnections()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.delete'))
    }
  }

  function providerBadge(p: string) {
    return p === 'github'
      ? 'bg-slate-700 text-slate-200'
      : 'bg-orange-900/50 text-orange-300'
  }

  function statusBadge(s: string) {
    return s === 'active'
      ? 'bg-green-900/50 text-green-300'
      : 'bg-slate-700 text-slate-400'
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={t('title')}
          description={t('description')}
          actions={
            <button
              onClick={() => {
                if (showCreate) { setShowCreate(false); resetForm() }
                else { resetForm(); setShowCreate(true) }
              }}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showCreate ? t('actions.cancel') : t('actions.new')}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}
        {success && (
          <div className="mb-4 p-3 bg-green-900/30 text-green-400 rounded-lg text-sm border border-green-800/50">{success}</div>
        )}

        {showCreate && (
          <form onSubmit={handleSubmit} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div>
                <label htmlFor="vcs-name" className="block text-sm font-medium text-slate-300 mb-1">{t('form.name')}</label>
                <input id="vcs-name" type="text" value={name} onChange={(e) => setName(e.target.value)} required
                  pattern="[a-zA-Z0-9][a-zA-Z0-9_\-]*"
                  title={t('form.namePattern')}
                  placeholder="my-github-app"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
              <div>
                <label htmlFor="vcs-provider" className="block text-sm font-medium text-slate-300 mb-1">{t('form.provider')}</label>
                <select id="vcs-provider" value={provider} disabled={editId !== null}
                  onChange={(e) => setProvider(e.target.value as 'github' | 'gitlab')}
                  title={editId !== null ? t('form.providerImmutable') : undefined}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 disabled:opacity-60 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                  <option value="github">GitHub</option>
                  <option value="gitlab">GitLab</option>
                </select>
              </div>
              <div>
                <label htmlFor="vcs-url" className="block text-sm font-medium text-slate-300 mb-1">{t('form.serverUrl')}</label>
                <input id="vcs-url" type="text" value={serverUrl} onChange={(e) => setServerUrl(e.target.value)}
                  pattern="https?://.+"
                  title={t('form.serverUrlHint')}
                  placeholder={provider === 'github' ? 'https://api.github.com' : 'https://gitlab.com'}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
            </div>

            {provider === 'github' ? (
              <div className="space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="gh-app-id" className="block text-sm font-medium text-slate-300 mb-1">{t('form.appId')}</label>
                    <input id="gh-app-id" type="text" value={appId} onChange={(e) => setAppId(e.target.value)} required
                      pattern="[0-9]+"
                      title={t('form.appIdHint')}
                      placeholder="123456"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="gh-install-id" className="block text-sm font-medium text-slate-300 mb-1">{t('form.installationId')}</label>
                    <input id="gh-install-id" type="text" value={installationId} onChange={(e) => setInstallationId(e.target.value)} required
                      pattern="[0-9]+"
                      title={t('form.installationIdHint')}
                      placeholder="789012"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                </div>
                <div>
                  <div className="flex items-center gap-2 mb-1">
                    <label htmlFor="gh-key" className="block text-sm font-medium text-slate-300">{t('form.privateKey')}</label>
                    <button type="button" onClick={() => pemFileRef.current?.click()}
                      className="text-xs text-brand-400 hover:text-brand-300 transition-colors">{t('form.browse')}</button>
                    <input ref={pemFileRef} type="file" accept=".pem,.key" className="hidden"
                      onChange={(e) => { const f = e.target.files?.[0]; if (f) f.text().then(t => setPrivateKey(t)) }} />
                  </div>
                  <textarea id="gh-key" value={privateKey} onChange={(e) => setPrivateKey(e.target.value)} required={editId === null} rows={4}
                    placeholder={editId !== null
                      ? t('form.privateKeyKeep')
                      : t('form.privateKeyPlaceholder')}
                    className={`w-full px-3 py-2 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent font-mono text-xs transition-colors ${pemDragOver ? 'border-2 border-dashed border-brand-400 bg-brand-900/20' : 'border border-slate-600'}`}
                    onDragOver={(e) => { e.preventDefault(); setPemDragOver(true) }}
                    onDragLeave={() => setPemDragOver(false)}
                    onDrop={(e) => {
                      e.preventDefault()
                      setPemDragOver(false)
                      const f = e.dataTransfer.files[0]
                      if (f) f.text().then(t => setPrivateKey(t))
                    }}
                  />
                </div>
                <div>
                  <label htmlFor="gh-webhook-secret" className="block text-sm font-medium text-slate-300 mb-1">
                    {t('form.webhookSecret')} <span className="text-slate-500 font-normal">{t('form.optional')}</span>
                  </label>
                  <input id="gh-webhook-secret" type="password" value={webhookSecret}
                    onChange={(e) => setWebhookSecret(e.target.value)}
                    placeholder={editId !== null
                      ? (connections.find((c) => c.id === editId)?.attributes['has-webhook-secret']
                          ? t('form.webhookSecretSet')
                          : t('form.webhookSecretGlobal'))
                      : t('form.webhookSecretGlobal')}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  <p className="mt-1 text-xs text-slate-500">
                    {t('form.webhookSecretHelp')}
                  </p>
                </div>
              </div>
            ) : (
              <div>
                <label htmlFor="gl-token" className="block text-sm font-medium text-slate-300 mb-1">{t('form.accessToken')}</label>
                <input id="gl-token" type="password" value={token} onChange={(e) => setToken(e.target.value)} required={editId === null}
                  placeholder={editId !== null ? t('form.accessTokenKeep') : 'glpat-...'}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
            )}

            {/* GHSA-v8g7-pqrj-8mcm. The three settings that decide who may use
                this connection and where it may be pointed. Grouped and
                labelled as access control rather than scattered among the
                credential fields, because an operator setting a glob here is
                making a security decision, not filling in a detail. */}
            {/* role=group + aria-labelledby rather than fieldset/legend: a
                legend notches whatever border the fieldset carries, and a
                fieldset's `min-width: min-content` does not shrink, which is how
                a grouped form pushes a phone sideways. The grouping an assistive
                technology announces is the same. */}
            <div role="group" aria-labelledby="vcs-access-heading"
              className="pt-3 border-t border-slate-700/50 space-y-3">
              <h3 id="vcs-access-heading" className="text-sm font-semibold text-slate-200">{t('access.heading')}</h3>
              <p className="text-xs text-slate-500">{t('access.intro')}</p>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label htmlFor="vcs-owner" className="block text-sm font-medium text-slate-300 mb-1">
                    {t('access.ownerEmail')} <span className="text-slate-500 font-normal">{t('form.optional')}</span>
                  </label>
                  <input id="vcs-owner" type="email" value={ownerEmail}
                    onChange={(e) => setOwnerEmail(e.target.value)}
                    placeholder="owner@example.com" /* i18n-ignore — an address shape, not prose */
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  <p className="mt-1 text-xs text-slate-500">{t('access.ownerEmailHint')}</p>
                </div>
                <div>
                  <span className="block text-sm font-medium text-slate-300 mb-1">{t('access.labels')}</span>
                  <LabelsEditor labels={connLabels} onChange={setConnLabels} />
                  <p className="mt-1 text-xs text-slate-500">{t('access.labelsHint')}</p>
                </div>
              </div>

              <div>
                <span className="block text-sm font-medium text-slate-300 mb-1">{t('access.allowedRepositories')}</span>
                <StringListEditor
                  values={allowedRepos}
                  onChange={setAllowedRepos}
                  placeholder="org/repo-*" /* i18n-ignore — a glob example, not prose */
                  addLabel={t('access.addPattern')}
                />
                <p className="mt-1 text-xs text-slate-500">{t('access.allowedRepositoriesHint')}</p>
                {/* The empty state is stated outright, because "no entries" here
                    means the OPPOSITE of what a blank list usually implies: an
                    empty allowlist permits every repository. Leaving the list
                    simply blank would read as "nothing is permitted". */}
                {nonBlank(allowedRepos).length === 0 ? (
                  <p className="mt-2 p-2 rounded-lg text-xs bg-amber-900/30 text-amber-300 border border-amber-800/50">
                    {t('access.anyRepositoryWarning')}
                  </p>
                ) : (
                  <p className="mt-2 p-2 rounded-lg text-xs bg-slate-700/50 text-slate-300 border border-slate-600/50">
                    {t('access.restricted', { count: nonBlank(allowedRepos).length })}
                  </p>
                )}
              </div>
            </div>

            <button type="submit" disabled={creating}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
              {creating
                ? (editId !== null ? t('form.saving') : t('form.creating'))
                : (editId !== null ? t('form.saveChanges') : t('form.createConnection'))}
            </button>
          </form>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : connections.length === 0 ? (
          <EmptyState message={t('empty')} />
        ) : (
          /* Panels rather than a table (#1339). A connection now carries a
             saturation verdict, a consumption rate, a countdown and an
             expandable per-consumer breakdown — far more than a table cell can
             hold without becoming a cramped stack of text. Cards give each
             connection room, and they reflow to one column on a phone for free,
             so the dual desktop/mobile render the table needed is gone. */
          <>
            <div className="flex items-center justify-end gap-2 mb-3">
              <label htmlFor="vcs-sort" className="text-xs text-slate-500">{t('sortBy')}</label>
              <select
                id="vcs-sort"
                value={`${sortState.key ?? 'name'}:${sortState.direction ?? 'asc'}`}
                onChange={(e) => {
                  const [key, dir] = e.target.value.split(':')
                  // toggleSort flips direction, so call it until both match —
                  // at most twice, and it keeps one source of sort truth.
                  if (sortState.key !== key || sortState.direction !== dir) {
                    toggleSort(key as VCSSortKey)
                    if (sortState.key === key && sortState.direction !== dir) return
                    if (sortState.key !== key && dir === 'desc') toggleSort(key as VCSSortKey)
                  }
                }}
                className="px-2 py-1 rounded-lg text-xs bg-slate-700 border border-slate-600 text-slate-200 focus:outline-none focus:ring-2 focus:ring-brand-500"
              >
                <option value="name:asc">{t('table.name')}</option>
                <option value="provider:asc">{t('table.provider')}</option>
                <option value="status:asc">{t('table.status')}</option>
                <option value="created:desc">{t('table.created')}</option>
              </select>
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {sortedItems.map((conn) => (
                <div
                  key={conn.id}
                  /* A card is the unit an assertion needs to scope to: every
                     verdict here is per connection, so a page-wide check would
                     pass on a neighbouring card's text. Class names are styling
                     and nesting is not a contract, so the handle is explicit. */
                  data-testid="vcs-connection-card"
                  className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 flex flex-col gap-3 h-full"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="font-semibold text-slate-200 me-auto break-all">{conn.attributes.name}</h3>
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${providerBadge(conn.attributes.provider)}`}>
                      {conn.attributes.provider}
                    </span>
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${statusBadge(conn.attributes.status)}`}>
                      {conn.attributes.status}
                    </span>
                  </div>

                  {conn.attributes['server-url'] ? (
                    <p className="text-xs text-slate-500 break-all" dir="ltr">{conn.attributes['server-url']}</p>
                  ) : null}

                  {/* GHSA-v8g7-pqrj-8mcm. Readable from the list, not only from
                      the edit form: "which repositories can this credential
                      reach" is the question an operator comes here to answer,
                      and it should not require opening a form to see. */}
                  <div className="pt-3 border-t border-slate-700/50 space-y-2">
                    <p className="text-xs text-slate-400 break-all">
                      {conn.attributes['owner-email']
                        ? t('access.ownedBy', { email: conn.attributes['owner-email'] })
                        : t('access.unowned')}
                    </p>
                    <LabelsEditor labels={conn.attributes.labels || {}} readOnly />
                    {nonBlank(conn.attributes['allowed-repositories'] || []).length === 0 ? (
                      <p className="text-xs text-amber-300">{t('access.anyRepositorySummary')}</p>
                    ) : (
                      <div className="space-y-1">
                        <p className="text-xs text-slate-400">
                          {t('access.repositoryCount', {
                            count: nonBlank(conn.attributes['allowed-repositories'] || []).length,
                          })}
                        </p>
                        <div className="flex flex-wrap gap-1.5">
                          {nonBlank(conn.attributes['allowed-repositories'] || []).map((pattern) => (
                            <span key={pattern}
                              className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-mono bg-slate-700 text-slate-200 border border-slate-600 break-all"
                              dir="ltr">
                              {pattern}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>

                  <div className="pt-3 border-t border-slate-700/50">
                    <VCSConsumption attrs={conn.attributes} />
                  </div>

                  <div className="mt-auto pt-3 border-t border-slate-700/50 flex flex-wrap items-center gap-2">
                    <span className="text-xs text-slate-500 me-auto">
                      {t('createdAt', { date: fmt.date(conn.attributes['created-at']) })}
                    </span>
                    <button onClick={() => startEdit(conn)} className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors">{t('actions.edit')}</button>
                    <button onClick={() => handleDelete(conn.id)} className="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors">{t('actions.delete')}</button>
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
      </main>
    </>
  )
}
