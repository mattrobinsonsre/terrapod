'use client'

// Ansible inventory tab (#1967, #1968) — the declared hosts and what they
// resolve to.
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
// ## The resolved view is LIVE, and there is nothing to refresh
//
// Dynamic inventory is banned (#1970, closed `NOT_PLANNED`), so every source
// Terrapod implements is static — declared rows the managing Terraform owns —
// and resolving one is a database query. The read resolves the rows to answer
// itself, so the panel states what the numbers ARE, with no date beside them
// and no refresh action: both would invite an operator to wonder whether what
// they are looking at is current, which is a question the read has already
// answered.
//
// The screen carrying a timestamp was the earlier shape, when a resolution was
// a stored snapshot. Nothing stores one now, so there is no second answer a
// button could fetch.
//
// ## The limit preview is the safety surface
//
// Auto-configure is deliberately broad, so **visibility is the control rather
// than prevention**: "what would this target" is the question an operator needs
// answered before anything runs. It expands against the same live resolution,
// so there is no freshness caveat; the one that remains is on screen whenever a
// result is, and still matters — the expansion here is ADVISORY, because the
// authoritative one is `ansible-inventory --list --limit` taken in the runner. A
// `~regex` term is refused by the API with a 422, and that message is shown
// rather than an empty list, because an empty target set for a pattern ansible
// would have expanded reads as "nothing matches".

import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { apiFetch, fetchAllPages, parseApiError } from '@/lib/api'
import { LoadingSpinner } from '@/components/loading-spinner'
import { EmptyState } from '@/components/empty-state'
import { MobileCard, MobileCardList } from '@/components/mobile-card-list'
import { StatChip } from '@/components/stat-chip'

export interface InventorySource {
  id: string
  position: number
  kind: string
  config: Record<string, unknown>
  'created-at': string
}

export interface Inventory {
  id: string
  attributes: {
    name: string
    description: string
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
  hosts: Record<string, Record<string, unknown>>
  groups: Record<string, string[]>
}

interface LimitPreviewAttrs {
  limit: string
  hosts: string[]
  'host-count': number
  'of-host-count': number
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
}: {
  workspaceId: string
  // Fetched by the parent, which needs them for the tab gate anyway — one
  // request serves both rather than the panel re-asking the same question.
  inventories: Inventory[]
}) {
  const t = useTranslations('workspaceDetail.inventory')

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
        // 422 for a `~regex` term, 409 when a source needs ansible and no
        // runner has posted a resolution to limit against. Both messages name
        // the reason, so both are shown as-is.
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

      {/* ── Resolution ─────────────────────────────────────────────────────── */}
      <section className="rounded-lg border border-slate-700/50 bg-slate-800/50 p-4">
        <div className="mb-3 min-w-0">
          <h3 className="text-sm font-semibold text-slate-200">{t('resolved.heading')}</h3>
          <p className="text-xs text-slate-500 break-words">
            {t('resolved.subheading', { name: attrs.name })}
          </p>
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
            {/* No date, deliberately: the read resolved these rows to answer
                itself, so there is no other resolution this could be and
                nothing for a timestamp to distinguish it from. */}
            <p className="mt-3 text-xs text-slate-500">{t('resolved.liveNote')}</p>

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
            {/* Rendered with the result, never under a disclosure: a target
                list read without it is a target list an operator will act on.
                The freshness caveat that used to sit beside it is gone — the
                expansion is taken against a live resolution now, so there is no
                snapshot age for it to be bounded by. */}
            <p className="mt-3 text-xs text-amber-400/90">{t('limit.advisory')}</p>
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
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
