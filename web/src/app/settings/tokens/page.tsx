'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { getAuthState, getUserId, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { useSortable } from '@/lib/use-sortable'
import { useFormat } from '@/lib/format'

interface Token {
  id: string
  attributes: {
    description: string
    kind: string
    'bound-to': string | null
    'created-by': string
    'pinned-roles': string[] | null
    'created-at': string | null
    'last-used-at': string | null
    'expires-at': string | null
    'lifespan-hours': number | null
    token: string | null
  }
}

// Lifespan option hour values (labels are localised in the component via `t`).
const LIFESPAN_HOURS = [720, 2160, 4320, 8760]

// Token kinds (#495). interactive = a person's CLI/login token; service_bound =
// a service token whose effective permissions are the AND of its pinned roles
// and its owner's live roles (dies when the owner is offboarded); detached =
// an admin-managed service token with an absolute pinned scope, bound to no
// user (survives any single person leaving). Badge classes are presentational;
// the label + help prose is localised via the `settings.tokens.kinds.*` keys.
const KIND_BADGE: Record<string, string> = {
  interactive: 'bg-slate-700 text-slate-300',
  service_bound: 'bg-sky-900/50 text-sky-300 border border-sky-800/50',
  service_detached: 'bg-amber-900/50 text-amber-300 border border-amber-800/50',
}

export default function TokensPage() {
  const router = useRouter()
  const t = useTranslations('settings')
  const fmt = useFormat()
  const kindLabel = (kind: string) =>
    t(`tokens.kinds.${KIND_BADGE[kind] ? kind : 'interactive'}.label`)
  const kindHelp = (kind: string) =>
    t(`tokens.kinds.${KIND_BADGE[kind] ? kind : 'interactive'}.help`)
  const [tokens, setTokens] = useState<Token[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [showCreate, setShowCreate] = useState(false)
  const [description, setDescription] = useState('')
  const [lifespanHours, setLifespanHours] = useState<number>(8760)
  const [kind, setKind] = useState<string>('interactive')
  const [pinnedRoles, setPinnedRoles] = useState<Set<string>>(new Set())
  const [creating, setCreating] = useState(false)
  const [createdToken, setCreatedToken] = useState<string | null>(null)
  const [showToken, setShowToken] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const [kindFilter, setKindFilter] = useState<string>('all')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [revoking, setRevoking] = useState(false)
  const [rotatingId, setRotatingId] = useState<string | null>(null)
  const [admin, setAdmin] = useState(false)
  const [userId, setUserId] = useState('')
  const [ownRoles, setOwnRoles] = useState<string[]>([])
  const [allRoles, setAllRoles] = useState<string[]>([])

  type TokenSortKey = 'description' | 'kind' | 'bound-to' | 'created-at' | 'last-used-at' | 'expires-at'
  const { sortedItems: sortedTokens, sortState, toggleSort } = useSortable<Token, TokenSortKey>(
    tokens, 'created-at', 'desc',
    useCallback((item: Token, key: TokenSortKey) => {
      switch (key) {
        case 'description': return item.attributes.description
        case 'kind': return item.attributes.kind
        case 'bound-to': return item.attributes['bound-to'] ?? ''
        case 'created-at': return item.attributes['created-at']
        case 'last-used-at': return item.attributes['last-used-at']
        case 'expires-at': return item.attributes['expires-at']
      }
    }, []),
  )

  useEffect(() => {
    const auth = getAuthState()
    if (!auth) { router.push('/login'); return }
    setAdmin(isAdmin())
    setUserId(getUserId())
    // A bound token can only be scoped to a subset of the creator's own roles
    // (the AND caps it), so offer exactly those — minus the implicit everyone.
    setOwnRoles((auth.roles || []).filter((r) => r !== 'everyone'))
  }, [router])

  useEffect(() => {
    if (!userId) return
    loadTokens()
  }, [userId, showAll, kindFilter])

  // Detached tokens pin an absolute scope from any role; only admins create
  // them, and only admins can list all roles for the picker.
  useEffect(() => {
    if (!admin) return
    apiFetch('/api/terrapod/v1/roles')
      .then((r) => (r.ok ? r.json() : { data: [] }))
      .then((d) => setAllRoles((d.data || []).map((role: { name: string }) => role.name).filter(Boolean)))
      .catch(() => {})
  }, [admin])

  async function loadTokens() {
    try {
      let url: string
      if (showAll) {
        url = '/api/terrapod/v1/admin/authentication-tokens'
        if (kindFilter !== 'all') url += `?kind=${kindFilter}`
      } else {
        url = `/api/terrapod/v1/users/${userId}/authentication-tokens`
      }
      const res = await apiFetch(url)
      if (!res.ok) throw new Error(t('tokens.errors.load'))
      const data = await res.json()
      setTokens(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : t('tokens.errors.load'))
    } finally {
      setLoading(false)
    }
  }

  function resetCreateForm() {
    setDescription('')
    setLifespanHours(8760)
    setKind('interactive')
    setPinnedRoles(new Set())
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    setCreatedToken(null)
    try {
      const attributes: Record<string, unknown> = {
        description,
        lifespan_hours: lifespanHours,
        kind,
      }
      if (kind !== 'interactive') {
        attributes.pinned_roles = [...pinnedRoles]
      }
      const res = await apiFetch(`/api/terrapod/v1/users/${userId}/authentication-tokens`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'authentication-tokens', attributes } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('tokens.errors.create', { status: res.status }))
      }
      const data = await res.json()
      setCreatedToken(data.data?.attributes?.token || null)
      setShowToken(false)
      resetCreateForm()
      setShowCreate(false)
      await loadTokens()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('tokens.errors.createGeneric'))
    } finally {
      setCreating(false)
    }
  }

  async function handleRotate(tokenId: string) {
    if (!confirm(t('tokens.confirmRotate'))) return
    setRotatingId(tokenId)
    setError('')
    setCreatedToken(null)
    try {
      const res = await apiFetch(`/api/terrapod/v1/authentication-tokens/${tokenId}/actions/rotate`, {
        method: 'POST',
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('tokens.errors.rotate', { status: res.status }))
      }
      const data = await res.json()
      setCreatedToken(data.data?.attributes?.token || null)
      setShowToken(false)
      await loadTokens()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('tokens.errors.rotateGeneric'))
    } finally {
      setRotatingId(null)
    }
  }

  async function handleRevoke(tokenId: string) {
    setError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/authentication-tokens/${tokenId}`, {
        method: 'DELETE',
      })
      if (!res.ok && res.status !== 204) throw new Error(t('tokens.errors.revoke', { status: res.status }))
      setSelected((prev) => { const next = new Set(prev); next.delete(tokenId); return next })
      await loadTokens()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('tokens.errors.revokeGeneric'))
    }
  }

  async function handleBulkRevoke() {
    if (selected.size === 0) return
    if (!confirm(t('tokens.confirmBulkRevoke', { count: selected.size }))) return
    setRevoking(true)
    setError('')
    try {
      const ids = [...selected]
      await Promise.all(ids.map((id) =>
        apiFetch(`/api/terrapod/v1/authentication-tokens/${id}`, { method: 'DELETE' })
      ))
      setSelected(new Set())
      await loadTokens()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('tokens.errors.bulkRevoke'))
    } finally {
      setRevoking(false)
    }
  }

  function toggleSelect(id: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  function toggleSelectAll() {
    if (selected.size === sortedTokens.length) {
      setSelected(new Set())
    } else {
      setSelected(new Set(sortedTokens.map((t) => t.id)))
    }
  }

  function togglePinnedRole(name: string) {
    setPinnedRoles((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name); else next.add(name)
      return next
    })
  }

  function formatDate(iso: string | null): string {
    if (!iso) return t('tokens.never')
    return fmt.dateTime(iso)
  }

  // A token minted by `terraform login` / `tofu login` — the server always
  // sets this exact description prefix on the OAuth token-exchange (#502).
  // These are intentionally short-lived (login_token_ttl_hours, default 12h),
  // so an imminent expiry is by design, not a foot-gun worth warning about.
  // Every other token — including manually-created personal tokens — keeps the
  // amber "nearing expiry" warning.
  function isLoginToken(tok: Token): boolean {
    return (
      tok.attributes.kind === 'interactive' &&
      (tok.attributes.description || '').startsWith('terraform login')
    )
  }

  function expiryColor(iso: string | null, isLogin: boolean): string {
    if (!iso) return 'text-slate-400'
    const now = Date.now()
    const expires = new Date(iso).getTime()
    if (expires <= now) return 'text-red-400'
    if (!isLogin && expires - now < 30 * 24 * 60 * 60 * 1000) return 'text-amber-400'
    return 'text-slate-400'
  }

  // Detached is admin-only (the server enforces 403); only offer it to admins.
  const kindOptions = admin
    ? ['interactive', 'service_bound', 'service_detached']
    : ['interactive', 'service_bound']
  const rolePickerSource = kind === 'service_detached' ? allRoles : ownRoles

  const KindBadge = ({ kind }: { kind: string }) => (
    <span
      className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${KIND_BADGE[kind] ?? KIND_BADGE.interactive}`}
      title={kindHelp(kind)}
    >
      {kindLabel(kind)}
    </span>
  )

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={t('tokens.title')}
          description={showAll ? t('tokens.descriptionAll') : t('tokens.description')}
          actions={
            <div className="flex items-center gap-3">
              {admin && (
                <button
                  onClick={() => { setShowAll(!showAll); setSelected(new Set()) }}
                  className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                    showAll
                      ? 'bg-amber-600 hover:bg-amber-500 text-white'
                      : 'bg-slate-700 hover:bg-slate-600 text-slate-300'
                  }`}
                >
                  {showAll ? t('tokens.myTokens') : t('tokens.allTokens')}
                </button>
              )}
              <button
                onClick={() => { setShowCreate(!showCreate); setCreatedToken(null) }}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
              >
                {showCreate ? t('tokens.cancel') : t('tokens.createToken')}
              </button>
            </div>
          }
        />

        {error && <ErrorBanner message={error} />}

        {createdToken && (
          <div className="relative mb-6 p-4 pr-10 bg-green-900/30 rounded-lg border border-green-800/50">
            <button
              onClick={() => { setCreatedToken(null); setShowToken(false) }}
              aria-label={t('tokens.dismissToken')}
              className="absolute top-2 right-2 text-green-400 hover:text-green-200 transition-colors"
            >
              ✕
            </button>
            <p className="text-sm text-green-300 font-medium mb-1">{t('tokens.tokenReady')}</p>
            <p className="text-xs text-green-400 mb-2">{t('tokens.tokenReadyHelp')}</p>
            <div className="flex items-center gap-2">
              <code
                className={`flex-1 text-sm text-green-200 bg-green-900/30 p-2 rounded font-mono overflow-x-auto ${
                  showToken ? '' : 'select-none'
                }`}
              >
                {showToken ? createdToken : '•'.repeat(48)}
              </code>
              <button
                onClick={() => setShowToken((v) => !v)}
                aria-pressed={showToken}
                className="px-3 py-1 rounded text-xs font-medium bg-green-800/50 hover:bg-green-700/50 text-green-200 transition-colors flex-shrink-0 w-14"
              >
                {showToken ? t('tokens.hide') : t('tokens.show')}
              </button>
              <button
                onClick={() => navigator.clipboard.writeText(createdToken)}
                className="px-3 py-1 rounded text-xs font-medium bg-green-800/50 hover:bg-green-700/50 text-green-200 transition-colors flex-shrink-0"
              >
                {t('tokens.copy')}
              </button>
            </div>
          </div>
        )}

        {showCreate && (
          <form onSubmit={handleCreate} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-4">
            <div className="flex flex-wrap items-end gap-3">
              <div className="flex-1 min-w-[200px]">
                <label htmlFor="tok-desc" className="block text-sm font-medium text-slate-300 mb-1">{t('tokens.form.description')}</label>
                <input
                  id="tok-desc"
                  type="text"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder={t('tokens.form.descriptionPlaceholder')}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
              <div className="w-48">
                <label htmlFor="tok-kind" className="block text-sm font-medium text-slate-300 mb-1">{t('tokens.form.kind')}</label>
                <select
                  id="tok-kind"
                  value={kind}
                  onChange={(e) => { setKind(e.target.value); setPinnedRoles(new Set()) }}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                >
                  {kindOptions.map((k) => (
                    <option key={k} value={k}>{kindLabel(k)}</option>
                  ))}
                </select>
              </div>
              <div className="w-40">
                <label htmlFor="tok-lifespan" className="block text-sm font-medium text-slate-300 mb-1">{t('tokens.form.lifespan')}</label>
                <select
                  id="tok-lifespan"
                  value={lifespanHours}
                  onChange={(e) => setLifespanHours(Number(e.target.value))}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                >
                  {LIFESPAN_HOURS.map((hours) => (
                    <option key={hours} value={hours}>{t(`tokens.lifespans.${hours}`)}</option>
                  ))}
                </select>
              </div>
              <button
                type="submit"
                disabled={creating}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
              >
                {creating ? t('tokens.form.creating') : t('tokens.form.create')}
              </button>
            </div>

            <p className="text-xs text-slate-400">{kindHelp(kind)}</p>

            {kind !== 'interactive' && (
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-2">
                  {t('tokens.form.pinnedRoles')}
                  <span className="text-slate-500 font-normal">
                    {' '}{t('tokens.form.pinnedRolesHelp')}
                    {kind === 'service_bound' ? ' ' + t('tokens.form.pinnedRolesCapped') : ''}
                  </span>
                </label>
                {rolePickerSource.length === 0 ? (
                  <p className="text-xs text-slate-500 italic">
                    {kind === 'service_detached'
                      ? t('tokens.form.noCustomRoles')
                      : t('tokens.form.noScopedRoles')}
                  </p>
                ) : (
                  <div className="flex flex-wrap gap-2">
                    {rolePickerSource.map((name) => (
                      <label
                        key={name}
                        className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border text-sm cursor-pointer transition-colors ${
                          pinnedRoles.has(name)
                            ? 'bg-brand-900/30 border-brand-700/50 text-brand-200'
                            : 'bg-slate-700/50 border-slate-600/50 text-slate-300 hover:bg-slate-700'
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={pinnedRoles.has(name)}
                          onChange={() => togglePinnedRole(name)}
                          className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
                        />
                        {name}
                      </label>
                    ))}
                  </div>
                )}
              </div>
            )}
          </form>
        )}

        {admin && showAll && (
          <div className="mb-4 flex items-center gap-2">
            <label htmlFor="kind-filter" className="text-sm text-slate-400">{t('tokens.form.kind')}</label>
            <select
              id="kind-filter"
              value={kindFilter}
              onChange={(e) => setKindFilter(e.target.value)}
              className="px-3 py-1.5 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500"
            >
              <option value="all">{t('tokens.filter.all')}</option>
              <option value="interactive">{t('tokens.kinds.interactive.label')}</option>
              <option value="service_bound">{t('tokens.kinds.service_bound.label')}</option>
              <option value="service_detached">{t('tokens.kinds.service_detached.label')}</option>
            </select>
          </div>
        )}

        {selected.size > 0 && (
          <div className="mb-4 flex items-center gap-3 p-3 bg-red-900/20 rounded-lg border border-red-800/30">
            <span className="text-sm text-slate-300">{t('tokens.selected', { count: selected.size })}</span>
            <button
              onClick={handleBulkRevoke}
              disabled={revoking}
              className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-500 disabled:bg-red-800 disabled:text-red-400 text-white transition-colors"
            >
              {revoking ? t('tokens.revoking') : t('tokens.revokeSelected')}
            </button>
            <button
              onClick={() => setSelected(new Set())}
              className="text-sm text-slate-400 hover:text-slate-300 transition-colors"
            >
              {t('tokens.clearSelection')}
            </button>
          </div>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : tokens.length === 0 ? (
          <EmptyState message={t('tokens.empty')} />
        ) : (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <th className="w-10 px-4 py-3">
                    <input
                      type="checkbox"
                      checked={sortedTokens.length > 0 && selected.size === sortedTokens.length}
                      onChange={toggleSelectAll}
                      className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
                    />
                  </th>
                  <SortableHeader label={t('tokens.columns.description')} sortKey="description" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label={t('tokens.columns.kind')} sortKey="kind" sortState={sortState} onSort={toggleSort} />
                  {showAll && <SortableHeader label={t('tokens.columns.boundTo')} sortKey="bound-to" sortState={sortState} onSort={toggleSort} />}
                  <SortableHeader label={t('tokens.columns.created')} sortKey="created-at" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label={t('tokens.columns.lastUsed')} sortKey="last-used-at" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label={t('tokens.columns.expires')} sortKey="expires-at" sortState={sortState} onSort={toggleSort} />
                  <th className="text-right px-4 py-3 text-slate-400 font-medium">{t('tokens.columns.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {sortedTokens.map((tok) => {
                  const isService = tok.attributes.kind !== 'interactive'
                  const pinned = tok.attributes['pinned-roles'] || []
                  return (
                    <tr key={tok.id} className={`border-b border-slate-700/30 last:border-0 ${selected.has(tok.id) ? 'bg-brand-900/10' : ''}`}>
                      <td className="w-10 px-4 py-3">
                        <input
                          type="checkbox"
                          checked={selected.has(tok.id)}
                          onChange={() => toggleSelect(tok.id)}
                          className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
                        />
                      </td>
                      <td className="px-4 py-3 text-slate-200">
                        {tok.attributes.description || <span className="text-slate-500 italic">{t('tokens.noDescription')}</span>}
                        {isService && pinned.length > 0 && (
                          <div className="mt-1 flex flex-wrap gap-1">
                            {pinned.map((r) => (
                              <span key={r} className="inline-block px-1.5 py-0.5 rounded bg-slate-700/60 text-slate-400 text-[10px] font-mono">
                                {r}
                              </span>
                            ))}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <KindBadge kind={tok.attributes.kind} />
                      </td>
                      {showAll && (
                        <td className="px-4 py-3 text-slate-400 text-xs">
                          {tok.attributes['bound-to'] || <span className="text-amber-400/80 italic">{t('tokens.detached')}</span>}
                        </td>
                      )}
                      <td className="px-4 py-3 text-slate-400 text-xs">
                        {formatDate(tok.attributes['created-at'])}
                      </td>
                      <td className="px-4 py-3 text-slate-400 text-xs">
                        {formatDate(tok.attributes['last-used-at'])}
                      </td>
                      <td className={`px-4 py-3 text-xs ${expiryColor(tok.attributes['expires-at'], isLoginToken(tok))}`}>
                        {formatDate(tok.attributes['expires-at'])}
                      </td>
                      <td className="px-4 py-3 text-right whitespace-nowrap">
                        {isService && (
                          <button
                            onClick={() => handleRotate(tok.id)}
                            disabled={rotatingId === tok.id}
                            className="text-xs text-brand-400 hover:text-brand-300 disabled:text-slate-500 transition-colors mr-3"
                          >
                            {rotatingId === tok.id ? t('tokens.rotating') : t('tokens.rotate')}
                          </button>
                        )}
                        <button
                          onClick={() => handleRevoke(tok.id)}
                          className="text-xs text-red-400 hover:text-red-300 transition-colors"
                        >
                          {t('tokens.revoke')}
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </>
  )
}
