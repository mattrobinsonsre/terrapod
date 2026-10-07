'use client'

// Ansible inventory tab (#1967, #1968) — the declared hosts, what they resolve
// to, and when that was taken.
//
// ## This tab is DATA-GATED, and the gate is the design
//
// The parent page renders it only when `GET …/inventories` came back non-empty.
// An inventory is created lazily on the first declared host, so a
// terraform/tofu-only workspace has no rows, no tab and no configuration to
// turn any of this off — runtime adaptation rather than a flag, the same
// mechanism the Cost, Security and Architecture surfaces already use (#1986).
//
// ## Read-only, deliberately
//
// Hosts are declared by the workspace's own Terraform (`terrapod_inventory_item`,
// #1968), so this offers no create / edit / delete. Terraform owns the lifecycle
// and would revert an out-of-band edit on the next apply, so a button here would
// be an affordance for losing work. The tab says so in as many words, because an
// operator who is not told goes looking for the button.
//
// ## Freshness is load-bearing, not decoration
//
// A reader sees the LAST SNAPSHOT, never a live resolution: `taken-at` and
// `produced-by` are stated next to the numbers they qualify, and the refresh
// action is offered beside them. A stale snapshot presented as if it were live
// is the wrong answer dressed as an answer, which is exactly what the limit
// preview below would then be built on.
//
// ## The limit preview is the safety surface
//
// Auto-configure is deliberately broad, so **visibility is the control rather
// than prevention**: "what would this target" is the question an operator needs
// answered before anything runs. Both caveats are on screen whenever a result
// is — it is ADVISORY (the authoritative expansion is `ansible-inventory
// --list --limit` in the runner) and it is only as fresh as the snapshot. A
// `~regex` term is refused by the API with a 422, and that message is shown
// rather than an empty list, because an empty target set for a pattern ansible
// would have expanded reads as "nothing matches".

import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { apiFetch, fetchAllPages, parseApiError } from '@/lib/api'
import { useIsTouch } from '@/lib/use-media-query'
import { LoadingSpinner } from '@/components/loading-spinner'
import { EmptyState } from '@/components/empty-state'
import { MobileCard, MobileCardList } from '@/components/mobile-card-list'
import { StatChip } from '@/components/stat-chip'

export interface InventorySource {
  id: string
  position: number
  kind: string
  config: Record<string, unknown>
  'api-resolvable': boolean
  'created-at': string
}

export interface Inventory {
  id: string
  attributes: {
    name: string
    description: string
    'api-resolvable': boolean
    // In `-i` order: a lower position resolves first, so a higher one wins a
    // conflicting host variable. The API sends them sorted.
    sources: InventorySource[]
    'created-at': string
    'updated-at': string
  }
}

interface InventoryItem {
  id: string
  attributes: {
    name: string
    address: string
    groups: string[]
    vars: Record<string, unknown>
    'created-at': string
    'updated-at': string
  }
}

interface ResolvedAttrs {
  'host-count': number
  'group-count': number
  'produced-by': string
  'produced-by-ref': string
  'taken-at': string
  hosts: Record<string, Record<string, unknown>>
  groups: Record<string, string[]>
}

interface LimitPreviewAttrs {
  limit: string
  hosts: string[]
  'host-count': number
  'of-host-count': number
  'taken-at': string
}

/** Host vars as one readable `key=value` run. Values are not prose: no translation. */
function formatVars(vars: Record<string, unknown>): string {
  return Object.entries(vars)
    .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(' ')
}

export function InventoryPanel({
  workspaceId,
  inventories,
  canWrite,
}: {
  workspaceId: string
  // Fetched by the parent, which needs them for the tab gate anyway — one
  // request serves both rather than the panel re-asking the same question.
  inventories: Inventory[]
  canWrite: boolean
}) {
  const t = useTranslations('workspaceDetail.inventory')
  const isTouch = useIsTouch()

  const [selectedId, setSelectedId] = useState(inventories[0]?.id ?? '')
  const inventory = inventories.find((i) => i.id === selectedId) ?? inventories[0]

  const [items, setItems] = useState<InventoryItem[] | null>(null)
  const [itemsError, setItemsError] = useState('')

  const [resolved, setResolved] = useState<ResolvedAttrs | null>(null)
  // The resolve refusal (409) names WHICH source kinds need ansible, so it is
  // the actionable part of the response and is rendered verbatim rather than
  // replaced with a generic "unavailable".
  const [resolvedError, setResolvedError] = useState('')
  const [resolvedLoading, setResolvedLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)

  const [limit, setLimit] = useState('')
  const [preview, setPreview] = useState<LimitPreviewAttrs | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [previewing, setPreviewing] = useState(false)

  const loadItems = useCallback(async () => {
    try {
      setItems(
        await fetchAllPages<InventoryItem>(`/api/v1/workspaces/${workspaceId}/inventory-items`),
      )
    } catch (err) {
      setItemsError(err instanceof Error ? err.message : t('errors.loadItems'))
      setItems([])
    }
  }, [workspaceId, t])

  const loadResolved = useCallback(async () => {
    if (!inventory) return
    setResolvedLoading(true)
    setResolvedError('')
    try {
      const res = await apiFetch(`/api/v1/inventories/${inventory.id}/resolved`)
      if (!res.ok) {
        setResolved(null)
        setResolvedError(await parseApiError(res, t('errors.loadResolved')))
        return
      }
      const body = await res.json()
      setResolved(body.data?.attributes ?? null)
    } catch (err) {
      setResolved(null)
      setResolvedError(err instanceof Error ? err.message : t('errors.loadResolved'))
    } finally {
      setResolvedLoading(false)
    }
  }, [inventory, t])

  useEffect(() => {
    loadItems()
  }, [loadItems])

  useEffect(() => {
    loadResolved()
    // A different inventory resolves to a different host set, so a preview held
    // against the old one would be a target list for the wrong thing.
    setPreview(null)
    setPreviewError('')
  }, [loadResolved])

  async function refresh() {
    // Replaces what every other reader of this inventory is shown, so it is a
    // mutation: guarded on touch, where a mis-tap is easy (#719).
    if (isTouch && !window.confirm(t('refreshConfirm'))) return
    setRefreshing(true)
    setResolvedError('')
    try {
      const res = await apiFetch(`/api/v1/inventories/${inventory!.id}/actions/resolve`, {
        method: 'POST',
      })
      if (!res.ok) {
        setResolvedError(await parseApiError(res, t('errors.refresh')))
        return
      }
      const body = await res.json()
      setResolved(body.data?.attributes ?? null)
      // The preview was taken against the previous snapshot, so it is now a
      // statement about a host set that no longer exists.
      setPreview(null)
    } catch (err) {
      setResolvedError(err instanceof Error ? err.message : t('errors.refresh'))
    } finally {
      setRefreshing(false)
    }
  }

  async function runPreview() {
    setPreviewing(true)
    setPreviewError('')
    try {
      const res = await apiFetch(`/api/v1/inventories/${inventory!.id}/actions/preview-limit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data: { attributes: { limit } } }),
      })
      if (!res.ok) {
        // 422 for a `~regex` term, 409 when there is no snapshot to limit
        // against. Both messages name the reason, so both are shown as-is.
        setPreview(null)
        setPreviewError(await parseApiError(res, t('errors.preview')))
        return
      }
      const body = await res.json()
      setPreview(body.data?.attributes ?? null)
    } catch (err) {
      setPreview(null)
      setPreviewError(err instanceof Error ? err.message : t('errors.preview'))
    } finally {
      setPreviewing(false)
    }
  }

  if (!inventory) return null

  const attrs = inventory.attributes
  const sources = attrs.sources || []
  const stale = sources.filter((s) => !s['api-resolvable'])
  const groups = resolved?.groups ?? {}
  const groupNames = Object.keys(groups).sort()

  return (
    // Named so a test can assert the tab rendered without depending on a
    // locale's heading text -- the RTL and novelty catalogues legitimately
    // make every visible string different.
    <div data-testid="inventory-tab" className="space-y-6">
      {/* What this release does and does not deliver. #1967 makes the inventory
          observable; configure operations (#1971, #1972) do not exist yet, and
          saying so is cheaper than letting an operator hunt for a Run button. */}
      <div className="rounded-lg border border-slate-700/50 bg-slate-800/40 p-4">
        <h3 className="text-sm font-semibold text-slate-200">{t('scope.heading')}</h3>
        <p className="mt-1 text-sm text-slate-400">{t('scope.body')}</p>
      </div>

      {inventories.length > 1 && (
        <div className="min-w-0">
          <label htmlFor="inventory-select" className="block text-xs text-slate-500">
            {t('selectLabel')}
          </label>
          <select
            id="inventory-select"
            value={inventory.id}
            onChange={(e) => setSelectedId(e.target.value)}
            className="mt-1 w-full min-w-0 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 sm:w-auto"
          >
            {inventories.map((i) => (
              <option key={i.id} value={i.id}>
                {i.attributes.name}
              </option>
            ))}
          </select>
        </div>
      )}

      {/* ── Resolution + freshness ─────────────────────────────────────────── */}
      <section className="rounded-lg border border-slate-700/50 bg-slate-800/50 p-4">
        <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-slate-200">{t('resolved.heading')}</h3>
            <p className="text-xs text-slate-500 break-words">
              {t('resolved.subheading', { name: attrs.name })}
            </p>
          </div>
          {canWrite && attrs['api-resolvable'] && (
            <button
              type="button"
              onClick={refresh}
              disabled={refreshing}
              className="rounded-lg bg-slate-700 px-3 py-2 text-sm font-medium text-slate-100 transition-colors hover:bg-slate-600 disabled:opacity-50"
            >
              {refreshing ? t('resolved.refreshing') : t('resolved.refresh')}
            </button>
          )}
        </div>

        {resolvedLoading && <LoadingSpinner />}

        {!resolvedLoading && resolvedError && (
          <p className="rounded-lg border border-amber-800/50 bg-amber-900/20 p-3 text-sm break-words text-amber-300">
            {resolvedError}
          </p>
        )}

        {!resolvedLoading && resolved && (
          <>
            <div className="flex flex-wrap gap-2">
              <StatChip label={t('resolved.hosts')} value={resolved['host-count']} />
              <StatChip label={t('resolved.groups')} value={resolved['group-count']} />
            </div>
            {/* The honest statement of what the numbers above are: a snapshot,
                with its age and its author. */}
            <p className="mt-3 text-xs text-slate-500 break-words">
              {t('resolved.takenAt', {
                when: resolved['taken-at']
                  ? new Date(resolved['taken-at']).toLocaleString()
                  : t('resolved.never'),
              })}
              {' · '}
              {t.has(`resolved.producedBy.${resolved['produced-by']}`)
                ? t(`resolved.producedBy.${resolved['produced-by']}`)
                : t('resolved.producedByOther', { by: resolved['produced-by'] })}
              {resolved['produced-by-ref'] ? ` (${resolved['produced-by-ref']})` : ''}
            </p>
            <p className="mt-1 text-xs text-slate-500">{t('resolved.snapshotNote')}</p>

            {groupNames.length > 0 && (
              <dl className="mt-4 space-y-2">
                {groupNames.map((g) => (
                  <div key={g} className="flex flex-wrap items-baseline gap-2">
                    <dt className="font-mono text-xs text-slate-400" dir="ltr">
                      {g}
                    </dt>
                    <dd className="min-w-0 font-mono text-xs break-words text-slate-300" dir="ltr">
                      {groups[g]!.join(', ')}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </>
        )}
      </section>

      {/* ── Limit preview ──────────────────────────────────────────────────── */}
      <section className="rounded-lg border border-slate-700/50 bg-slate-800/50 p-4">
        <h3 className="text-sm font-semibold text-slate-200">{t('limit.heading')}</h3>
        <p className="mt-1 text-sm text-slate-400">{t('limit.body')}</p>

        <div className="mt-3 flex flex-col gap-2 sm:flex-row">
          <input
            id="inventory-limit"
            type="text"
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
            placeholder={t('limit.placeholder')}
            aria-label={t('limit.inputLabel')}
            dir="ltr"
            className="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 font-mono text-base text-slate-200 sm:text-sm"
          />
          <button
            type="button"
            onClick={runPreview}
            disabled={previewing || !resolved}
            className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-brand-500 disabled:opacity-50"
          >
            {previewing ? t('limit.previewing') : t('limit.preview')}
          </button>
        </div>

        {previewError && (
          <p className="mt-3 rounded-lg border border-red-800/50 bg-red-900/20 p-3 text-sm break-words text-red-300">
            {previewError}
          </p>
        )}

        {preview && (
          <div className="mt-3">
            <p className="text-sm text-slate-300">
              {t('limit.matched', {
                count: preview['host-count'],
                total: preview['of-host-count'],
              })}
            </p>
            {preview.hosts.length > 0 && (
              <ul className="mt-2 flex flex-wrap gap-1.5">
                {preview.hosts.map((h) => (
                  <li
                    key={h}
                    className="rounded-full bg-slate-700 px-2 py-0.5 font-mono text-xs break-all text-slate-200"
                    dir="ltr"
                  >
                    {h}
                  </li>
                ))}
              </ul>
            )}
            {/* Both caveats are rendered with the result, never under a
                disclosure: a target list read without them is a target list an
                operator will act on. */}
            <p className="mt-3 text-xs text-amber-400/90">{t('limit.advisory')}</p>
            <p className="mt-1 text-xs text-slate-500">
              {t('limit.asFreshAs', {
                when: preview['taken-at']
                  ? new Date(preview['taken-at']).toLocaleString()
                  : t('resolved.never'),
              })}
            </p>
          </div>
        )}
      </section>

      {/* ── Declared hosts ─────────────────────────────────────────────────── */}
      <section className="rounded-lg border border-slate-700/50 bg-slate-800/50 p-4">
        <h3 className="text-sm font-semibold text-slate-200">{t('items.heading')}</h3>
        <p className="mt-1 text-sm text-slate-400">{t('items.managedNote')}</p>

        {itemsError && <p className="mt-3 text-sm break-words text-red-400">{itemsError}</p>}
        {items === null && <LoadingSpinner />}
        {items !== null && items.length === 0 && !itemsError && (
          <div className="mt-3">
            <EmptyState message={t('items.empty')} />
          </div>
        )}

        {items !== null && items.length > 0 && (
          <>
            {/* Desktop keeps the table; the mobile half is the card list below,
                so nothing is HIDDEN at phone width — the host name, its address
                and its groups are the whole point of the row (#719). */}
            <div className="mt-3 hidden overflow-hidden md:block">
              <table className="w-full table-fixed text-sm">
                <thead>
                  <tr className="border-b border-slate-700/50 text-start text-xs text-slate-500">
                    <th className="w-1/5 py-2 pe-3 text-start font-medium">{t('items.name')}</th>
                    <th className="w-1/5 py-2 pe-3 text-start font-medium">{t('items.address')}</th>
                    <th className="w-1/4 py-2 pe-3 text-start font-medium">{t('items.groups')}</th>
                    <th className="py-2 text-start font-medium">{t('items.vars')}</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((it) => (
                    <tr key={it.id} className="border-b border-slate-700/30 align-top">
                      <td
                        className="py-2 pe-3 font-mono text-xs break-words text-slate-200"
                        dir="ltr"
                      >
                        {it.attributes.name}
                      </td>
                      <td
                        className="py-2 pe-3 font-mono text-xs break-words text-slate-400"
                        dir="ltr"
                      >
                        {it.attributes.address || '—'}
                      </td>
                      <td className="py-2 pe-3">
                        {(it.attributes.groups || []).length === 0 ? (
                          <span className="text-xs text-slate-500">{t('items.noGroups')}</span>
                        ) : (
                          <span className="flex flex-wrap gap-1">
                            {it.attributes.groups.map((g) => (
                              <span
                                key={g}
                                className="rounded-full bg-slate-700 px-2 py-0.5 font-mono text-xs break-all text-slate-300"
                                dir="ltr"
                              >
                                {g}
                              </span>
                            ))}
                          </span>
                        )}
                      </td>
                      <td className="py-2 font-mono text-xs break-words text-slate-400" dir="ltr">
                        {Object.keys(it.attributes.vars || {}).length === 0
                          ? '—'
                          : formatVars(it.attributes.vars)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="mt-3">
              <MobileCardList>
                {items.map((it) => (
                  <MobileCard
                    key={it.id}
                    title={
                      <span className="font-mono text-sm break-all text-slate-200" dir="ltr">
                        {it.attributes.name}
                      </span>
                    }
                    fields={[
                      {
                        label: t('items.address'),
                        value: it.attributes.address || '—',
                        valueClassName: 'text-slate-300 font-mono',
                      },
                      {
                        label: t('items.groups'),
                        value:
                          (it.attributes.groups || []).length === 0
                            ? t('items.noGroups')
                            : it.attributes.groups.join(', '),
                        valueClassName: 'text-slate-300 font-mono',
                      },
                      {
                        label: t('items.vars'),
                        value:
                          Object.keys(it.attributes.vars || {}).length === 0
                            ? '—'
                            : formatVars(it.attributes.vars),
                        valueClassName: 'text-slate-400 font-mono',
                      },
                    ]}
                  />
                ))}
              </MobileCardList>
            </div>
          </>
        )}
      </section>

      {/* ── Sources ────────────────────────────────────────────────────────── */}
      <section className="rounded-lg border border-slate-700/50 bg-slate-800/50 p-4">
        <h3 className="text-sm font-semibold text-slate-200">{t('sources.heading')}</h3>
        <p className="mt-1 text-sm text-slate-400">{t('sources.body')}</p>
        {/* Per-source `api-resolvable` is what lets a reader say WHICH source is
            why a snapshot is as old as it is, rather than inferring it from the
            inventory's rolled-up flag. */}
        {stale.length > 0 && (
          <p className="mt-2 text-xs text-amber-400/90">
            {t('sources.needsRunner', { kinds: stale.map((s) => s.kind).join(', ') })}
          </p>
        )}
        <ul className="mt-3 space-y-2">
          {sources.map((s) => (
            <li
              key={s.id}
              className="flex flex-wrap items-center gap-2 border-b border-slate-700/30 pb-2 last:border-0"
            >
              <span className="rounded bg-slate-700 px-2 py-0.5 text-xs text-slate-300">
                {t('sources.position', { position: s.position })}
              </span>
              <span className="font-mono text-xs text-slate-200" dir="ltr">
                {s.kind}
              </span>
              <span
                className={`rounded-full px-2 py-0.5 text-xs ${
                  s['api-resolvable']
                    ? 'bg-emerald-900/40 text-emerald-300'
                    : 'bg-amber-900/40 text-amber-300'
                }`}
              >
                {s['api-resolvable'] ? t('sources.apiResolvable') : t('sources.runnerOnly')}
              </span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
