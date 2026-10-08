'use client'

// Ansible inventory tab (#1967, #1968, #1969) — eight structures, each its own
// addressable row, each editable here.
//
// ## Per-row editing is the premise, not a feature of it
//
// The inventory is eight tables — settings, hosts, groups, membership, nesting,
// and a variable table per scope — and every one of them is a resource with its
// own routes. So an operator edits ONE host, ONE membership, ONE variable; the
// earlier single read-only panel was the shape of a model that no longer
// exists, and its "declared by Terraform, so read-only here" copy is gone with
// it. Terraform is still *a* writer (`terrapod_inventory_*`), and it is no
// longer the only one.
//
// ## This tab is DATA-GATED, and the gate is the design
//
// No hosts, no groups and no settings ⇒ no tab. A terraform/tofu-only workspace
// therefore has nothing to see and nothing to turn off — runtime adaptation
// rather than a flag, the same mechanism the Cost, Security and Architecture
// surfaces already use (#1986).
//
// The consequence is worth saying out loud: because the gate reads rows, the
// FIRST row cannot be created from here. It arrives through the provider, the
// API or the MCP tools, and from then on this tab owns the editing. That is
// deliberate — a tab that appeared on every workspace so that it could offer an
// Add button would be exactly the configuration-shaped surface the gate exists
// to avoid.
//
// ## The resolution is ANSIBLE's, live, and there is nothing to refresh
//
// `ansible-inventory --list` does the merge, the precedence, the group DAG and
// the derivation of `all`/`ungrouped`; `?limit=` is expanded by ansible too, so
// `~regex` works. Every source is static (dynamic inventory is banned — #1970,
// closed `NOT_PLANNED`), so resolving is a function of the rows and the read
// resolves them to answer itself. No timestamp, no refresh button: both would
// invite an operator to wonder whether what they are looking at is current,
// which is the question the read has already answered.
//
// ## Two things about the resolved view that must be rendered honestly
//
// `groups[name]` is DIRECT membership only — ansible does not flatten nesting
// into it, so a parent whose members all arrive through a child reports none of
// its own. Showing that list alone would read as "this group is empty", so the
// nesting is rendered beside it and labelled.
//
// `?limit=` is the authoritative answer to "what would this target", and it
// DOES expand through nesting. It is the safety surface rather than a
// convenience: auto-configure is deliberately broad, so visibility is the
// control, and the question has to be answerable before anything runs.

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { useTranslations } from 'next-intl'
import { apiFetch, fetchAllPages, parseApiError } from '@/lib/api'
import { useConfirm } from '@/lib/use-confirm'
import { LoadingSpinner } from '@/components/loading-spinner'
import { EmptyState } from '@/components/empty-state'
import { MobileCard, MobileCardList } from '@/components/mobile-card-list'
import { StatChip } from '@/components/stat-chip'

/* ── Wire types ─────────────────────────────────────────────────────────────
   The API's own kebab-case attribute names, read as they arrive. */

export interface InventoryHost {
  id: string
  attributes: {
    name: string
    'group-count': number
    'variable-count': number
  }
}

export interface InventoryGroup {
  id: string
  attributes: {
    name: string
    'member-count': number
    'child-count': number
    'variable-count': number
  }
}

/** A `host ∈ group` link, or a `parent ⊃ child` one. The id addresses the LINK. */
interface LinkRow {
  id: string
  relationships: Record<string, { data: { id: string; type: string } | null }>
}

interface InventoryVar {
  id: string
  attributes: {
    key: string
    /** The literal `***` when `sensitive` — never the stored value. */
    value: string
    structured: boolean
    sensitive: boolean
  }
}

interface InventorySettings {
  id: string
  attributes: {
    'include-platform': boolean
    'repo-url': string
    branch: string
    'working-directory': string
    'ignore-paths': string[]
  }
  relationships: {
    'vcs-connection': { data: { id: string } | null }
  }
}

interface ResolvedAttrs {
  hosts: Record<string, Record<string, unknown>>
  /** DIRECT membership per group. Nesting lives in `group-children`. */
  groups: Record<string, string[]>
  'group-children': Record<string, string[]>
  'host-count': number
  'group-count': number
  limit?: string
}

interface VcsConnection {
  id: string
  attributes: { name: string; provider: string }
}

/* ── Styling tokens ─────────────────────────────────────────────────────────
   One definition each, so a row action on the hosts table and the same action
   on a group's member list cannot drift apart. */

const SECTION = 'rounded-lg border border-slate-700/50 bg-slate-800/50 p-4'
const HEADING = 'text-sm font-semibold text-slate-200'
const BODY = 'mt-1 text-sm text-slate-400'
const INPUT =
  'w-full min-w-0 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-base text-slate-200 sm:text-sm'
const BTN =
  'px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors disabled:opacity-50'
const BTN_DANGER =
  'px-3 py-1.5 rounded-lg text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors disabled:opacity-50'
const BTN_PRIMARY =
  'px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors disabled:opacity-50'
const MONO = 'font-mono text-xs break-words text-slate-200'

/* ── The data gate ──────────────────────────────────────────────────────────- */

/**
 * Whether this workspace has an inventory at all — the tab's gate.
 *
 * Three probes, because there is no single question that answers it: a
 * workspace may hold hosts, or only groups, or only settings binding a VCS
 * source with every row still to be fetched. The host and group probes ask for
 * one row and read `meta.pagination.total-count`, so the gate costs three small
 * requests rather than three lists.
 *
 * Every failure leaves the gate shut. A 403 from a role without
 * `inventory:read` and a transport blip both mean "do not offer a surface this
 * reader cannot use", which is the safe direction either way.
 */
export function useInventoryPresence(workspaceId: string): boolean {
  const [present, setPresent] = useState(false)

  useEffect(() => {
    if (!workspaceId) return
    let cancelled = false

    const counted = async (path: string): Promise<boolean> => {
      const res = await apiFetch(`${path}?page%5Bsize%5D=1`)
      if (!res.ok) return false
      const body = await res.json()
      const total = body?.meta?.pagination?.['total-count']
      return typeof total === 'number' ? total > 0 : Array.isArray(body?.data) && body.data.length > 0
    }

    Promise.all([
      counted(`/api/v1/workspaces/${workspaceId}/inventory/hosts`).catch(() => false),
      counted(`/api/v1/workspaces/${workspaceId}/inventory/groups`).catch(() => false),
      // 404 is the ORDINARY state here: the settings row exists only to bind a
      // VCS source or to switch the declared rows off, so its absence is the
      // default rather than an error.
      apiFetch(`/api/v1/workspaces/${workspaceId}/inventory/settings`)
        .then((r) => r.ok)
        .catch(() => false),
    ]).then(([hosts, groups, settings]) => {
      if (!cancelled) setPresent(hosts || groups || settings)
    })

    return () => {
      cancelled = true
    }
  }, [workspaceId])

  return present
}

/* ── Small shared pieces ────────────────────────────────────────────────────-
   Defined at module scope, never inside a render: a component declared inside
   another component's render is a new type on every state change, so React
   unmounts and remounts the whole subtree under it. */

function Section({
  heading,
  body,
  children,
}: {
  heading: string
  body?: string
  children?: React.ReactNode
}) {
  return (
    <section className={SECTION}>
      <h3 className={HEADING}>{heading}</h3>
      {body && <p className={BODY}>{body}</p>}
      {children}
    </section>
  )
}

function ErrorText({ message }: { message: string }) {
  if (!message) return null
  return (
    <p className="mt-3 rounded-lg border border-red-800/50 bg-red-900/20 p-3 text-sm break-words text-red-300">
      {message}
    </p>
  )
}

/** The `name`-only create form that hosts and groups share. */
function NameForm({
  label,
  placeholder,
  submitLabel,
  busyLabel,
  inputId,
  onSubmit,
}: {
  label: string
  placeholder: string
  submitLabel: string
  busyLabel: string
  inputId: string
  onSubmit: (name: string) => Promise<void>
}) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!name.trim()) return
    setBusy(true)
    try {
      await onSubmit(name.trim())
      setName('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className="mt-3 flex flex-col gap-2 sm:flex-row">
      <div className="min-w-0 flex-1">
        <label htmlFor={inputId} className="block text-xs text-slate-500">
          {label}
        </label>
        <input
          id={inputId}
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={placeholder}
          dir="ltr"
          className={`mt-1 ${INPUT} font-mono`}
        />
      </div>
      <button type="submit" disabled={busy || !name.trim()} className={`${BTN_PRIMARY} sm:mt-5`}>
        {busy ? busyLabel : submitLabel}
      </button>
    </form>
  )
}

/** Inline rename, used by both detail views. */
function RenameForm({
  label,
  value,
  submitLabel,
  busyLabel,
  inputId,
  onSubmit,
}: {
  label: string
  value: string
  submitLabel: string
  busyLabel: string
  inputId: string
  onSubmit: (name: string) => Promise<void>
}) {
  const [name, setName] = useState(value)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setName(value)
  }, [value])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!name.trim() || name.trim() === value) return
    setBusy(true)
    try {
      await onSubmit(name.trim())
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className="mt-3 flex flex-col gap-2 sm:flex-row">
      <div className="min-w-0 flex-1">
        <label htmlFor={inputId} className="block text-xs text-slate-500">
          {label}
        </label>
        <input
          id={inputId}
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          dir="ltr"
          className={`mt-1 ${INPUT} font-mono`}
        />
      </div>
      <button
        type="submit"
        disabled={busy || !name.trim() || name.trim() === value}
        className={`${BTN} sm:mt-5 sm:self-start sm:py-2.5`}
      >
        {busy ? busyLabel : submitLabel}
      </button>
    </form>
  )
}

/**
 * A list of links (memberships, nestings) with a Remove per row and one Add.
 *
 * The four link surfaces — a host's groups, a group's members, its children and
 * its parents — are the same row with different ends, so they are one component
 * rather than four. A short pill list reflows at every width without a second
 * render, so this half of the tab needs no mobile twin.
 */
function LinkList({
  heading,
  body,
  emptyText,
  rows,
  options,
  addLabel,
  addBusyLabel,
  selectLabel,
  selectId,
  removeLabel,
  removeAria,
  onAdd,
  onRemove,
}: {
  heading: string
  body: string
  emptyText: string
  rows: { linkId: string; label: string }[]
  options: { id: string; label: string }[]
  addLabel: string
  addBusyLabel: string
  selectLabel: string
  selectId: string
  removeLabel: string
  removeAria: (name: string) => string
  onAdd: (id: string) => Promise<void>
  onRemove: (linkId: string, name: string) => Promise<void>
}) {
  const [choice, setChoice] = useState('')
  const [busy, setBusy] = useState(false)

  // A choice held over a list that no longer offers it would post an id the
  // server has stopped accepting, so it is dropped the moment the options move.
  useEffect(() => {
    if (choice && !options.some((o) => o.id === choice)) setChoice('')
  }, [options, choice])

  async function add(e: React.FormEvent) {
    e.preventDefault()
    if (!choice) return
    setBusy(true)
    try {
      await onAdd(choice)
      setChoice('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section heading={heading} body={body}>
      {rows.length === 0 ? (
        <p className="mt-3 text-sm text-slate-500">{emptyText}</p>
      ) : (
        <ul className="mt-3 flex flex-wrap gap-2">
          {rows.map((r) => (
            <li
              key={r.linkId}
              className="flex min-w-0 items-center gap-2 rounded-lg border border-slate-700/50 bg-slate-900/60 ps-3 pe-1.5 py-1.5"
            >
              <span className="min-w-0 font-mono text-xs break-all text-slate-200" dir="ltr">
                {r.label}
              </span>
              <button
                type="button"
                onClick={() => onRemove(r.linkId, r.label)}
                aria-label={removeAria(r.label)}
                className={BTN_DANGER}
              >
                {removeLabel}
              </button>
            </li>
          ))}
        </ul>
      )}

      {options.length > 0 && (
        <form onSubmit={add} className="mt-3 flex flex-col gap-2 sm:flex-row">
          <div className="min-w-0 flex-1">
            <label htmlFor={selectId} className="block text-xs text-slate-500">
              {selectLabel}
            </label>
            <select
              id={selectId}
              value={choice}
              onChange={(e) => setChoice(e.target.value)}
              className={`mt-1 ${INPUT}`}
            >
              <option value="">{selectLabel}</option>
              {options.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          <button
            type="submit"
            disabled={busy || !choice}
            className={`${BTN} sm:mt-5 sm:self-start sm:py-2.5`}
          >
            {busy ? addBusyLabel : addLabel}
          </button>
        </form>
      )}
    </Section>
  )
}

/**
 * The variable table, shared by all three scopes.
 *
 * One component for host, group and `group_vars/all` variables because the row
 * is identical in each and only the URLs differ — three near-copies is where
 * the fourth one quietly behaves differently.
 *
 * A sensitive value reads back as the literal `***`, so an edit starts with an
 * EMPTY value field and an omitted `value` on the PATCH leaves the stored one
 * alone. Writing the mask back would store the mask.
 */
function VariableTable({
  heading,
  body,
  listUrl,
  createUrl,
  rowBase,
  reloadKey,
  onChanged,
}: {
  heading: string
  body: string
  listUrl: string
  createUrl: string
  /** `/api/v1/inventory-host-vars`, `…-group-vars` or `…-global-vars`. */
  rowBase: string
  /** Bumped by the parent to force a reload after an unrelated write. */
  reloadKey?: number
  onChanged?: () => void
}) {
  const t = useTranslations('workspaceDetail.inventory')
  const { confirmDelete } = useConfirm()
  // The row base is a URL, so it cannot be an element id as it stands; one id
  // per scope keeps the three variable tables' labels pointing at their own
  // fields when two of them render on the same screen.
  const fieldId = rowBase.replace(/[^a-zA-Z0-9-]/g, '-')

  const [vars, setVars] = useState<InventoryVar[] | null>(null)
  const [error, setError] = useState('')
  const [editingId, setEditingId] = useState('')
  const [draft, setDraft] = useState({ key: '', value: '', structured: false, sensitive: false })
  const [saving, setSaving] = useState(false)
  const [adding, setAdding] = useState(false)

  const load = useCallback(async () => {
    try {
      setVars(await fetchAllPages<InventoryVar>(listUrl))
      setError('')
    } catch (err) {
      setVars([])
      setError(err instanceof Error ? err.message : t('errors.load'))
    }
  }, [listUrl, t])

  useEffect(() => {
    load()
  }, [load, reloadKey])

  function startAdd() {
    setEditingId('')
    setDraft({ key: '', value: '', structured: false, sensitive: false })
    setAdding(true)
  }

  function startEdit(v: InventoryVar) {
    setAdding(false)
    setEditingId(v.id)
    setDraft({
      key: v.attributes.key,
      // Blank for a sensitive row: the mask is all the API returns, and sending
      // it back would store it.
      value: v.attributes.sensitive ? '' : v.attributes.value,
      structured: v.attributes.structured,
      sensitive: v.attributes.sensitive,
    })
  }

  function cancel() {
    setAdding(false)
    setEditingId('')
    setError('')
  }

  async function save(e: React.FormEvent) {
    e.preventDefault()
    if (!draft.key.trim()) return
    setSaving(true)
    setError('')
    const editing = vars?.find((v) => v.id === editingId)
    // An edit of a sensitive row with the field left blank means "keep what is
    // stored", which the API expresses by the attribute being absent.
    const keepValue = Boolean(editing?.attributes.sensitive) && draft.value === ''
    const attributes: Record<string, unknown> = {
      key: draft.key.trim(),
      structured: draft.structured,
      sensitive: draft.sensitive,
    }
    if (!keepValue) attributes.value = draft.value
    try {
      const res = await apiFetch(editingId ? `${rowBase}/${editingId}` : createUrl, {
        method: editingId ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data: { attributes } }),
      })
      if (!res.ok) {
        setError(await parseApiError(res, t('errors.save')))
        return
      }
      cancel()
      await load()
      onChanged?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.save'))
    } finally {
      setSaving(false)
    }
  }

  async function remove(v: InventoryVar) {
    if (!confirmDelete(t('vars.confirmDelete', { key: v.attributes.key }))) return
    setError('')
    try {
      const res = await apiFetch(`${rowBase}/${v.id}`, { method: 'DELETE' })
      if (!res.ok && res.status !== 404) {
        setError(await parseApiError(res, t('errors.delete')))
        return
      }
      await load()
      onChanged?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.delete'))
    }
  }

  const form = (
    <form onSubmit={save} className="mt-3 space-y-3 rounded-lg border border-slate-700/50 p-3">
      <div className="min-w-0">
        <label htmlFor={`${fieldId}-key`} className="block text-xs text-slate-500">
          {t('vars.key')}
        </label>
        <input
          id={`${fieldId}-key`}
          type="text"
          value={draft.key}
          onChange={(e) => setDraft({ ...draft, key: e.target.value })}
          placeholder={t('vars.keyPlaceholder')}
          dir="ltr"
          className={`mt-1 ${INPUT} font-mono`}
        />
      </div>
      <div className="min-w-0">
        <label htmlFor={`${fieldId}-value`} className="block text-xs text-slate-500">
          {t('vars.value')}
        </label>
        <input
          id={`${fieldId}-value`}
          type="text"
          value={draft.value}
          onChange={(e) => setDraft({ ...draft, value: e.target.value })}
          dir="ltr"
          className={`mt-1 ${INPUT} font-mono`}
        />
        {editingId && vars?.find((v) => v.id === editingId)?.attributes.sensitive && (
          <p className="mt-1 text-xs text-slate-500">{t('vars.sensitiveKeep')}</p>
        )}
      </div>
      <div className="flex flex-wrap gap-4">
        <label className="flex items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={draft.structured}
            onChange={(e) => setDraft({ ...draft, structured: e.target.checked })}
            className="rounded border-slate-600 bg-slate-900"
          />
          {t('vars.structured')}
        </label>
        <label className="flex items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={draft.sensitive}
            onChange={(e) => setDraft({ ...draft, sensitive: e.target.checked })}
            className="rounded border-slate-600 bg-slate-900"
          />
          {t('vars.sensitive')}
        </label>
      </div>
      <p className="text-xs text-slate-500">{t('vars.structuredHelp')}</p>
      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={saving || !draft.key.trim()} className={BTN_PRIMARY}>
          {saving ? t('actions.saving') : t('actions.save')}
        </button>
        <button type="button" onClick={cancel} className={`${BTN} py-2.5`}>
          {t('actions.cancel')}
        </button>
      </div>
    </form>
  )

  return (
    <Section heading={heading} body={body}>
      <ErrorText message={error} />

      {vars === null && <LoadingSpinner />}

      {vars !== null && vars.length === 0 && !adding && (
        <div className="mt-3">
          <EmptyState message={t('vars.empty')} />
        </div>
      )}

      {vars !== null && vars.length > 0 && (
        <>
          {/* Desktop keeps the table; the card list below is the `< md` half, so
              nothing is hidden at phone width — a variable's key, its value and
              whether it is sensitive are the whole row. */}
          <div className="mt-3 hidden md:block">
            <table className="w-full table-fixed text-sm">
              <thead>
                <tr className="border-b border-slate-700/50 text-xs text-slate-500">
                  <th className="w-1/3 py-2 pe-3 text-start font-medium">{t('vars.key')}</th>
                  <th className="py-2 pe-3 text-start font-medium">{t('vars.value')}</th>
                  <th className="w-32 py-2 text-end font-medium">{t('vars.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {vars.map((v) => (
                  <tr key={v.id} className="border-b border-slate-700/30 align-top">
                    <td className={`py-2 pe-3 ${MONO}`} dir="ltr">
                      {v.attributes.key}
                    </td>
                    <td className="py-2 pe-3 font-mono text-xs break-words text-slate-400" dir="ltr">
                      {v.attributes.value}
                      {v.attributes.structured && (
                        <span className="ms-2 rounded bg-slate-700 px-1.5 py-0.5 text-xs text-slate-300">
                          {t('vars.structuredBadge')}
                        </span>
                      )}
                    </td>
                    <td className="py-2 text-end">
                      <div className="flex justify-end gap-2">
                        <button type="button" onClick={() => startEdit(v)} className={BTN}>
                          {t('actions.edit')}
                        </button>
                        <button type="button" onClick={() => remove(v)} className={BTN_DANGER}>
                          {t('actions.delete')}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-3">
            <MobileCardList>
              {vars.map((v) => (
                <MobileCard
                  key={v.id}
                  title={
                    <span className="font-mono text-sm break-all text-slate-200" dir="ltr">
                      {v.attributes.key}
                    </span>
                  }
                  badge={
                    v.attributes.structured ? (
                      <span className="shrink-0 rounded bg-slate-700 px-1.5 py-0.5 text-xs text-slate-300">
                        {t('vars.structuredBadge')}
                      </span>
                    ) : undefined
                  }
                  fields={[
                    {
                      label: t('vars.value'),
                      value: v.attributes.value,
                      valueClassName: 'text-slate-400 font-mono',
                    },
                  ]}
                  actions={
                    <>
                      <button type="button" onClick={() => startEdit(v)} className={BTN}>
                        {t('actions.edit')}
                      </button>
                      <button type="button" onClick={() => remove(v)} className={BTN_DANGER}>
                        {t('actions.delete')}
                      </button>
                    </>
                  }
                />
              ))}
            </MobileCardList>
          </div>
        </>
      )}

      {adding || editingId ? (
        form
      ) : (
        <button type="button" onClick={startAdd} className={`mt-3 ${BTN} py-2.5`}>
          {t('vars.add')}
        </button>
      )}
    </Section>
  )
}

/* ── Views ──────────────────────────────────────────────────────────────────- */

function HostsView({
  workspaceId,
  hosts,
  loading,
  onOpen,
  onReload,
}: {
  workspaceId: string
  hosts: InventoryHost[]
  loading: boolean
  onOpen: (id: string) => void
  onReload: () => void
}) {
  const t = useTranslations('workspaceDetail.inventory')
  const { confirmDelete } = useConfirm()
  const [writeError, setWriteError] = useState('')

  async function create(name: string) {
    setWriteError('')
    const res = await apiFetch(`/api/v1/workspaces/${workspaceId}/inventory/hosts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data: { attributes: { name } } }),
    })
    if (!res.ok) {
      setWriteError(await parseApiError(res, t('errors.save')))
      return
    }
    onReload()
  }

  async function remove(host: InventoryHost) {
    if (!confirmDelete(t('hosts.confirmDelete', { name: host.attributes.name }))) return
    setWriteError('')
    const res = await apiFetch(`/api/v1/inventory-hosts/${host.id}`, { method: 'DELETE' })
    if (!res.ok && res.status !== 404) {
      setWriteError(await parseApiError(res, t('errors.delete')))
      return
    }
    onReload()
  }

  return (
    <Section heading={t('hosts.heading')} body={t('hosts.body')}>
      <ErrorText message={writeError} />

      {loading && <LoadingSpinner />}

      {!loading && hosts.length === 0 && (
        <div className="mt-3">
          <EmptyState message={t('hosts.empty')} />
        </div>
      )}

      {!loading && hosts.length > 0 && (
        <>
          {/* The counts are the point of a host row, so they reach the phone
              render too rather than being hidden with the columns.

              Edit and the host name go to the same place, deliberately. The
              name is NAVIGATION and may be a link; Edit is an ACTION and has to
              be a real button with a tap target. The alternative — an inline
              rename in the row — would be a second rename surface beside the
              detail view, and two of them is how they come to disagree. */}
          <div className="mt-3 hidden md:block">
            <table className="w-full table-fixed text-sm">
              <thead>
                <tr className="border-b border-slate-700/50 text-xs text-slate-500">
                  <th className="py-2 pe-3 text-start font-medium">{t('hosts.colName')}</th>
                  <th className="w-24 py-2 pe-3 text-start font-medium">{t('hosts.colGroups')}</th>
                  <th className="w-24 py-2 pe-3 text-start font-medium">{t('hosts.colVars')}</th>
                  <th className="w-56 py-2 text-end font-medium">{t('vars.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {hosts.map((h) => (
                  <tr key={h.id} className="border-b border-slate-700/30 align-top">
                    <td className="py-2 pe-3">
                      <button
                        type="button"
                        onClick={() => onOpen(h.id)}
                        className="font-mono text-xs break-words text-brand-400 hover:text-brand-300"
                        dir="ltr"
                      >
                        {h.attributes.name}
                      </button>
                    </td>
                    <td className="py-2 pe-3 text-xs text-slate-400">
                      {h.attributes['group-count']}
                    </td>
                    <td className="py-2 pe-3 text-xs text-slate-400">
                      {h.attributes['variable-count']}
                    </td>
                    <td className="py-2 text-end">
                      <div className="flex justify-end gap-2">
                        <button type="button" onClick={() => onOpen(h.id)} className={BTN}>
                          {t('actions.edit')}
                        </button>
                        <button type="button" onClick={() => remove(h)} className={BTN_DANGER}>
                          {t('actions.delete')}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-3">
            <MobileCardList>
              {hosts.map((h) => (
                <MobileCard
                  key={h.id}
                  title={
                    <span className="font-mono text-sm break-all text-slate-200" dir="ltr">
                      {h.attributes.name}
                    </span>
                  }
                  fields={[
                    { label: t('hosts.colGroups'), value: h.attributes['group-count'] },
                    { label: t('hosts.colVars'), value: h.attributes['variable-count'] },
                  ]}
                  actions={
                    <>
                      <button type="button" onClick={() => onOpen(h.id)} className={BTN}>
                        {t('actions.edit')}
                      </button>
                      <button type="button" onClick={() => remove(h)} className={BTN_DANGER}>
                        {t('actions.delete')}
                      </button>
                    </>
                  }
                />
              ))}
            </MobileCardList>
          </div>
        </>
      )}

      <NameForm
        inputId="inventory-new-host"
        label={t('hosts.newLabel')}
        placeholder={t('hosts.newPlaceholder')}
        submitLabel={t('hosts.add')}
        busyLabel={t('actions.adding')}
        onSubmit={create}
      />
    </Section>
  )
}

function GroupsView({
  workspaceId,
  groups,
  loading,
  onOpen,
  onReload,
}: {
  workspaceId: string
  groups: InventoryGroup[]
  loading: boolean
  onOpen: (id: string) => void
  onReload: () => void
}) {
  const t = useTranslations('workspaceDetail.inventory')
  const { confirmDelete } = useConfirm()
  const [writeError, setWriteError] = useState('')

  async function create(name: string) {
    setWriteError('')
    const res = await apiFetch(`/api/v1/workspaces/${workspaceId}/inventory/groups`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data: { attributes: { name } } }),
    })
    if (!res.ok) {
      setWriteError(await parseApiError(res, t('errors.save')))
      return
    }
    onReload()
  }

  async function remove(group: InventoryGroup) {
    if (!confirmDelete(t('groups.confirmDelete', { name: group.attributes.name }))) return
    setWriteError('')
    const res = await apiFetch(`/api/v1/inventory-groups/${group.id}`, { method: 'DELETE' })
    if (!res.ok && res.status !== 404) {
      setWriteError(await parseApiError(res, t('errors.delete')))
      return
    }
    onReload()
  }

  return (
    <Section heading={t('groups.heading')} body={t('groups.body')}>
      <ErrorText message={writeError} />

      {loading && <LoadingSpinner />}

      {!loading && groups.length === 0 && (
        <div className="mt-3">
          <EmptyState message={t('groups.empty')} />
        </div>
      )}

      {!loading && groups.length > 0 && (
        <>
          <div className="mt-3 hidden md:block">
            <table className="w-full table-fixed text-sm">
              <thead>
                <tr className="border-b border-slate-700/50 text-xs text-slate-500">
                  <th className="py-2 pe-3 text-start font-medium">{t('groups.colName')}</th>
                  <th className="w-24 py-2 pe-3 text-start font-medium">{t('groups.colMembers')}</th>
                  <th className="w-24 py-2 pe-3 text-start font-medium">
                    {t('groups.colChildren')}
                  </th>
                  <th className="w-24 py-2 pe-3 text-start font-medium">{t('groups.colVars')}</th>
                  <th className="w-56 py-2 text-end font-medium">{t('vars.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {groups.map((g) => (
                  <tr key={g.id} className="border-b border-slate-700/30 align-top">
                    <td className="py-2 pe-3">
                      <button
                        type="button"
                        onClick={() => onOpen(g.id)}
                        className="font-mono text-xs break-words text-brand-400 hover:text-brand-300"
                        dir="ltr"
                      >
                        {g.attributes.name}
                      </button>
                    </td>
                    <td className="py-2 pe-3 text-xs text-slate-400">
                      {g.attributes['member-count']}
                    </td>
                    <td className="py-2 pe-3 text-xs text-slate-400">
                      {g.attributes['child-count']}
                    </td>
                    <td className="py-2 pe-3 text-xs text-slate-400">
                      {g.attributes['variable-count']}
                    </td>
                    <td className="py-2 text-end">
                      <div className="flex justify-end gap-2">
                        <button type="button" onClick={() => onOpen(g.id)} className={BTN}>
                          {t('actions.edit')}
                        </button>
                        <button type="button" onClick={() => remove(g)} className={BTN_DANGER}>
                          {t('actions.delete')}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-3">
            <MobileCardList>
              {groups.map((g) => (
                <MobileCard
                  key={g.id}
                  title={
                    <span className="font-mono text-sm break-all text-slate-200" dir="ltr">
                      {g.attributes.name}
                    </span>
                  }
                  fields={[
                    { label: t('groups.colMembers'), value: g.attributes['member-count'] },
                    { label: t('groups.colChildren'), value: g.attributes['child-count'] },
                    { label: t('groups.colVars'), value: g.attributes['variable-count'] },
                  ]}
                  actions={
                    <>
                      <button type="button" onClick={() => onOpen(g.id)} className={BTN}>
                        {t('actions.edit')}
                      </button>
                      <button type="button" onClick={() => remove(g)} className={BTN_DANGER}>
                        {t('actions.delete')}
                      </button>
                    </>
                  }
                />
              ))}
            </MobileCardList>
          </div>
        </>
      )}

      <NameForm
        inputId="inventory-new-group"
        label={t('groups.newLabel')}
        placeholder={t('groups.newPlaceholder')}
        submitLabel={t('groups.add')}
        busyLabel={t('actions.adding')}
        onSubmit={create}
      />
    </Section>
  )
}

function HostDetailView({
  host,
  groups,
  onReload,
}: {
  host: InventoryHost
  groups: InventoryGroup[]
  onReload: () => void
}) {
  const t = useTranslations('workspaceDetail.inventory')
  const { confirmTouchMutation } = useConfirm()

  const [links, setLinks] = useState<LinkRow[] | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      setLinks(await fetchAllPages<LinkRow>(`/api/v1/inventory-hosts/${host.id}/groups`))
      setError('')
    } catch (err) {
      setLinks([])
      setError(err instanceof Error ? err.message : t('errors.load'))
    }
  }, [host.id, t])

  useEffect(() => {
    // Fetching from the API on mount is what an effect is for, and the loader
    // sets its state from the response — the lint cannot see through the await
    // and reads the whole body as synchronous.
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data fetch, not derived state
    load()
  }, [load])

  const byId = useMemo(() => new Map(groups.map((g) => [g.id, g.attributes.name])), [groups])

  const rows = (links ?? []).map((l) => {
    const id = l.relationships?.group?.data?.id ?? ''
    return { linkId: l.id, label: byId.get(id) ?? id }
  })
  const joined = new Set((links ?? []).map((l) => l.relationships?.group?.data?.id ?? ''))
  const options = groups
    .filter((g) => !joined.has(g.id))
    .map((g) => ({ id: g.id, label: g.attributes.name }))

  async function rename(name: string) {
    setError('')
    const res = await apiFetch(`/api/v1/inventory-hosts/${host.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data: { attributes: { name } } }),
    })
    if (!res.ok) {
      setError(await parseApiError(res, t('errors.save')))
      return
    }
    onReload()
  }

  async function addGroup(groupId: string) {
    setError('')
    const res = await apiFetch(`/api/v1/inventory-hosts/${host.id}/groups`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        data: { relationships: { group: { data: { id: groupId, type: 'inventory-groups' } } } },
      }),
    })
    if (!res.ok) {
      setError(await parseApiError(res, t('errors.save')))
      return
    }
    await load()
    onReload()
  }

  async function removeGroup(linkId: string, name: string) {
    // Reversible — the membership can be added straight back — so this is the
    // touch-only tier rather than the unconditional one a delete gets.
    if (!confirmTouchMutation(t('hostDetail.confirmRemoveGroup', { name }))) return
    setError('')
    const res = await apiFetch(`/api/v1/inventory-host-groups/${linkId}`, { method: 'DELETE' })
    if (!res.ok && res.status !== 404) {
      setError(await parseApiError(res, t('errors.delete')))
      return
    }
    await load()
    onReload()
  }

  return (
    <div className="space-y-6">
      <Section
        heading={t('hostDetail.heading', { name: host.attributes.name })}
        body={t('hostDetail.body')}
      >
        <ErrorText message={error} />
        <RenameForm
          inputId="inventory-host-name"
          label={t('hosts.renameLabel')}
          value={host.attributes.name}
          submitLabel={t('actions.rename')}
          busyLabel={t('actions.saving')}
          onSubmit={rename}
        />
      </Section>

      <VariableTable
        heading={t('hostDetail.vars')}
        body={t('hostDetail.varsBody')}
        listUrl={`/api/v1/inventory-hosts/${host.id}/vars`}
        createUrl={`/api/v1/inventory-hosts/${host.id}/vars`}
        rowBase="/api/v1/inventory-host-vars"
        onChanged={onReload}
      />

      {links === null ? (
        <LoadingSpinner />
      ) : (
        <LinkList
          heading={t('hostDetail.memberships')}
          body={t('hostDetail.membershipsBody')}
          emptyText={t('hostDetail.membershipsEmpty')}
          rows={rows}
          options={options}
          addLabel={t('hostDetail.addGroup')}
          addBusyLabel={t('actions.adding')}
          selectLabel={t('hostDetail.addGroupLabel')}
          selectId="inventory-host-add-group"
          removeLabel={t('actions.remove')}
          removeAria={(name) => t('hostDetail.removeGroupAria', { name })}
          onAdd={addGroup}
          onRemove={removeGroup}
        />
      )}
    </div>
  )
}

function GroupDetailView({
  group,
  hosts,
  groups,
  onReload,
}: {
  group: InventoryGroup
  hosts: InventoryHost[]
  groups: InventoryGroup[]
  onReload: () => void
}) {
  const t = useTranslations('workspaceDetail.inventory')
  const { confirmTouchMutation } = useConfirm()

  const [members, setMembers] = useState<LinkRow[] | null>(null)
  const [children, setChildren] = useState<LinkRow[] | null>(null)
  const [parents, setParents] = useState<LinkRow[] | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      const [m, c, p] = await Promise.all([
        fetchAllPages<LinkRow>(`/api/v1/inventory-groups/${group.id}/hosts`),
        fetchAllPages<LinkRow>(`/api/v1/inventory-groups/${group.id}/children`),
        fetchAllPages<LinkRow>(`/api/v1/inventory-groups/${group.id}/parents`),
      ])
      setMembers(m)
      setChildren(c)
      setParents(p)
      setError('')
    } catch (err) {
      setMembers([])
      setChildren([])
      setParents([])
      setError(err instanceof Error ? err.message : t('errors.load'))
    }
  }, [group.id, t])

  useEffect(() => {
    // Fetching from the API on mount is what an effect is for, and the loader
    // sets its state from the response — the lint cannot see through the await
    // and reads the whole body as synchronous.
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data fetch, not derived state
    load()
  }, [load])

  const hostName = useMemo(
    () => new Map(hosts.map((h) => [h.id, h.attributes.name])),
    [hosts],
  )
  const groupName = useMemo(
    () => new Map(groups.map((g) => [g.id, g.attributes.name])),
    [groups],
  )

  async function write(url: string, body: unknown, after: () => Promise<void>) {
    setError('')
    const res = await apiFetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) {
      setError(await parseApiError(res, t('errors.save')))
      return
    }
    await after()
  }

  async function unlink(url: string, after: () => Promise<void>) {
    setError('')
    const res = await apiFetch(url, { method: 'DELETE' })
    if (!res.ok && res.status !== 404) {
      setError(await parseApiError(res, t('errors.delete')))
      return
    }
    await after()
  }

  const refresh = async () => {
    await load()
    onReload()
  }

  async function rename(name: string) {
    setError('')
    const res = await apiFetch(`/api/v1/inventory-groups/${group.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data: { attributes: { name } } }),
    })
    if (!res.ok) {
      setError(await parseApiError(res, t('errors.save')))
      return
    }
    onReload()
  }

  const memberRows = (members ?? []).map((l) => {
    const id = l.relationships?.host?.data?.id ?? ''
    return { linkId: l.id, label: hostName.get(id) ?? id }
  })
  const memberIds = new Set((members ?? []).map((l) => l.relationships?.host?.data?.id ?? ''))
  const memberOptions = hosts
    .filter((h) => !memberIds.has(h.id))
    .map((h) => ({ id: h.id, label: h.attributes.name }))

  const childRows = (children ?? []).map((l) => {
    const id = l.relationships?.['child-group']?.data?.id ?? ''
    return { linkId: l.id, label: groupName.get(id) ?? id }
  })
  const childIds = new Set(
    (children ?? []).map((l) => l.relationships?.['child-group']?.data?.id ?? ''),
  )
  const childOptions = groups
    .filter((g) => g.id !== group.id && !childIds.has(g.id))
    .map((g) => ({ id: g.id, label: g.attributes.name }))

  const parentRows = (parents ?? []).map((l) => {
    const id = l.relationships?.['parent-group']?.data?.id ?? ''
    return { linkId: l.id, label: groupName.get(id) ?? id }
  })
  const parentIds = new Set(
    (parents ?? []).map((l) => l.relationships?.['parent-group']?.data?.id ?? ''),
  )
  const parentOptions = groups
    .filter((g) => g.id !== group.id && !parentIds.has(g.id))
    .map((g) => ({ id: g.id, label: g.attributes.name }))

  if (members === null || children === null || parents === null) {
    return <LoadingSpinner />
  }

  return (
    <div className="space-y-6">
      <Section
        heading={t('groupDetail.heading', { name: group.attributes.name })}
        body={t('groupDetail.body')}
      >
        <ErrorText message={error} />
        <RenameForm
          inputId="inventory-group-name"
          label={t('groups.renameLabel')}
          value={group.attributes.name}
          submitLabel={t('actions.rename')}
          busyLabel={t('actions.saving')}
          onSubmit={rename}
        />
      </Section>

      <VariableTable
        heading={t('groupDetail.vars')}
        body={t('groupDetail.varsBody')}
        listUrl={`/api/v1/inventory-groups/${group.id}/vars`}
        createUrl={`/api/v1/inventory-groups/${group.id}/vars`}
        rowBase="/api/v1/inventory-group-vars"
        onChanged={onReload}
      />

      <LinkList
        heading={t('groupDetail.members')}
        body={t('groupDetail.membersBody')}
        emptyText={t('groupDetail.membersEmpty')}
        rows={memberRows}
        options={memberOptions}
        addLabel={t('groupDetail.addHost')}
        addBusyLabel={t('actions.adding')}
        selectLabel={t('groupDetail.addHostLabel')}
        selectId="inventory-group-add-host"
        removeLabel={t('actions.remove')}
        removeAria={(name) => t('groupDetail.removeHostAria', { name })}
        onAdd={(hostId) =>
          write(
            `/api/v1/inventory-groups/${group.id}/hosts`,
            { data: { relationships: { host: { data: { id: hostId, type: 'inventory-hosts' } } } } },
            refresh,
          )
        }
        onRemove={async (linkId, name) => {
          if (!confirmTouchMutation(t('groupDetail.confirmRemoveHost', { name }))) return
          await unlink(`/api/v1/inventory-host-groups/${linkId}`, refresh)
        }}
      />

      <LinkList
        heading={t('groupDetail.children')}
        body={t('groupDetail.childrenBody')}
        emptyText={t('groupDetail.childrenEmpty')}
        rows={childRows}
        options={childOptions}
        addLabel={t('groupDetail.addChild')}
        addBusyLabel={t('actions.adding')}
        selectLabel={t('groupDetail.addChildLabel')}
        selectId="inventory-group-add-child"
        removeLabel={t('actions.remove')}
        removeAria={(name) => t('groupDetail.removeChildAria', { name })}
        onAdd={(childId) =>
          write(
            `/api/v1/inventory-groups/${group.id}/children`,
            {
              data: {
                relationships: {
                  'child-group': { data: { id: childId, type: 'inventory-groups' } },
                },
              },
            },
            refresh,
          )
        }
        onRemove={async (linkId, name) => {
          if (!confirmTouchMutation(t('groupDetail.confirmRemoveChild', { name }))) return
          await unlink(`/api/v1/inventory-group-children/${linkId}`, refresh)
        }}
      />

      <LinkList
        heading={t('groupDetail.parents')}
        body={t('groupDetail.parentsBody')}
        emptyText={t('groupDetail.parentsEmpty')}
        rows={parentRows}
        options={parentOptions}
        addLabel={t('groupDetail.addParent')}
        addBusyLabel={t('actions.adding')}
        selectLabel={t('groupDetail.addParentLabel')}
        selectId="inventory-group-add-parent"
        removeLabel={t('actions.remove')}
        removeAria={(name) => t('groupDetail.removeParentAria', { name })}
        onAdd={(parentId) =>
          write(
            `/api/v1/inventory-groups/${group.id}/parents`,
            {
              data: {
                relationships: {
                  'parent-group': { data: { id: parentId, type: 'inventory-groups' } },
                },
              },
            },
            refresh,
          )
        }
        onRemove={async (linkId, name) => {
          if (!confirmTouchMutation(t('groupDetail.confirmRemoveParent', { name }))) return
          await unlink(`/api/v1/inventory-group-children/${linkId}`, refresh)
        }}
      />
    </div>
  )
}

/** Host variables as one readable `key=value` run. Values are not prose. */
function formatVars(vars: Record<string, unknown>): string {
  return Object.entries(vars)
    .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(' ')
}

function ResolvedView({ workspaceId, reloadKey }: { workspaceId: string; reloadKey: number }) {
  const t = useTranslations('workspaceDetail.inventory')

  const [limit, setLimit] = useState('')
  // Only an APPLIED limit reaches the request. Typing one must not re-resolve on
  // every keystroke, and a half-typed pattern expands to a target set that is
  // silently wrong rather than empty.
  const [applied, setApplied] = useState('')
  const [resolved, setResolved] = useState<ResolvedAttrs | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const qs = applied ? `?limit=${encodeURIComponent(applied)}` : ''
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}/inventory/resolved${qs}`)
      if (!res.ok) {
        setResolved(null)
        // The refusals name WHAT to do about them — a source that cannot be
        // resolved, or ansible not yet installed from the cache — so the
        // message is shown as it arrived rather than replaced with "unavailable".
        setError(await parseApiError(res, t('errors.resolved')))
        return
      }
      const body = await res.json()
      setResolved(body.data?.attributes ?? null)
    } catch (err) {
      setResolved(null)
      setError(err instanceof Error ? err.message : t('errors.resolved'))
    } finally {
      setLoading(false)
    }
  }, [workspaceId, applied, t])

  useEffect(() => {
    load()
  }, [load, reloadKey])

  const hostNames = resolved ? Object.keys(resolved.hosts).sort() : []
  const groupNames = resolved ? Object.keys(resolved.groups).sort() : []
  const nesting = resolved?.['group-children'] ?? {}
  const nestingParents = Object.keys(nesting)
    .filter((p) => (nesting[p] ?? []).length > 0)
    .sort()

  return (
    <div className="space-y-6">
      <Section heading={t('resolved.limitHeading')} body={t('resolved.limitBody')}>
        <div className="mt-3 flex flex-col gap-2 sm:flex-row">
          <input
            id="inventory-limit"
            type="text"
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
            placeholder={t('resolved.limitPlaceholder')}
            aria-label={t('resolved.limitLabel')}
            dir="ltr"
            className={`${INPUT} flex-1 font-mono`}
          />
          <button
            type="button"
            onClick={() => setApplied(limit.trim())}
            disabled={loading}
            className={BTN_PRIMARY}
          >
            {t('resolved.limitApply')}
          </button>
          {applied && (
            <button
              type="button"
              onClick={() => {
                setLimit('')
                setApplied('')
              }}
              className={`${BTN} py-2.5`}
            >
              {t('resolved.limitClear')}
            </button>
          )}
        </div>
        {applied && (
          <p className="mt-2 text-sm text-slate-300 break-words">
            {t('resolved.limitApplied', { limit: applied })}
          </p>
        )}
      </Section>

      <Section heading={t('resolved.heading')} body={t('resolved.body')}>
        <ErrorText message={error} />
        {loading && <LoadingSpinner />}

        {!loading && resolved && (
          <>
            <div className="mt-3 flex flex-wrap gap-2">
              <StatChip label={t('resolved.hosts')} value={resolved['host-count']} />
              <StatChip label={t('resolved.groups')} value={resolved['group-count']} />
            </div>
            {/* No date, deliberately: the read resolved these rows to answer
                itself, so there is no other resolution this could be and
                nothing for a timestamp to distinguish it from. */}
            <p className="mt-3 text-xs text-slate-500">{t('resolved.liveNote')}</p>

            {hostNames.length === 0 ? (
              <p className="mt-3 text-sm text-slate-500">{t('resolved.noHosts')}</p>
            ) : (
              <dl className="mt-4 space-y-2">
                {hostNames.map((h) => (
                  <div key={h} className="flex flex-wrap items-baseline gap-2">
                    <dt className="font-mono text-xs text-slate-200" dir="ltr">
                      {h}
                    </dt>
                    <dd className="min-w-0 font-mono text-xs break-words text-slate-400" dir="ltr">
                      {Object.keys(resolved.hosts[h] ?? {}).length === 0
                        ? t('resolved.noVars')
                        : formatVars(resolved.hosts[h] ?? {})}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </>
        )}
      </Section>

      {!loading && resolved && (
        <Section heading={t('resolved.groupsHeading')} body={t('resolved.directOnly')}>
          {groupNames.length === 0 ? (
            <p className="mt-3 text-sm text-slate-500">{t('resolved.noGroups')}</p>
          ) : (
            <dl className="mt-3 space-y-2">
              {groupNames.map((g) => (
                <div key={g} className="flex flex-wrap items-baseline gap-2">
                  <dt className="font-mono text-xs text-slate-200" dir="ltr">
                    {g}
                  </dt>
                  <dd className="min-w-0 font-mono text-xs break-words text-slate-400" dir="ltr">
                    {(resolved.groups[g] ?? []).length === 0
                      ? t('resolved.noDirectMembers')
                      : (resolved.groups[g] ?? []).join(', ')}
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </Section>
      )}

      {!loading && resolved && (
        <Section heading={t('resolved.nestingHeading')} body={t('resolved.nestingBody')}>
          {nestingParents.length === 0 ? (
            <p className="mt-3 text-sm text-slate-500">{t('resolved.noNesting')}</p>
          ) : (
            <dl className="mt-3 space-y-2">
              {nestingParents.map((p) => (
                <div key={p} className="flex flex-wrap items-baseline gap-2">
                  <dt className="font-mono text-xs text-slate-200" dir="ltr">
                    {p}
                  </dt>
                  <dd className="min-w-0 font-mono text-xs break-words text-slate-400" dir="ltr">
                    {(nesting[p] ?? []).join(', ')}
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </Section>
      )}
    </div>
  )
}

function SettingsView({
  workspaceId,
  settings,
  loading,
  onReload,
}: {
  workspaceId: string
  settings: InventorySettings | null
  loading: boolean
  onReload: () => void
}) {
  const t = useTranslations('workspaceDetail.inventory')
  const { confirmDelete } = useConfirm()

  const [editing, setEditing] = useState(false)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [conns, setConns] = useState<VcsConnection[]>([])

  const [includePlatform, setIncludePlatform] = useState(true)
  const [vcsId, setVcsId] = useState('')
  const [repoUrl, setRepoUrl] = useState('')
  const [branch, setBranch] = useState('')
  const [workingDirectory, setWorkingDirectory] = useState('')
  const [ignorePaths, setIgnorePaths] = useState('')

  useEffect(() => {
    fetchAllPages<VcsConnection>('/api/v1/vcs-connections')
      .then(setConns)
      .catch(() => {
        /* a reader without `vcs:read` simply gets no picker */
      })
  }, [])

  const reset = useCallback(() => {
    const a = settings?.attributes
    setIncludePlatform(a ? a['include-platform'] : true)
    setVcsId(settings?.relationships['vcs-connection']?.data?.id ?? '')
    setRepoUrl(a?.['repo-url'] ?? '')
    setBranch(a?.branch ?? '')
    setWorkingDirectory(a?.['working-directory'] ?? '')
    setIgnorePaths((a?.['ignore-paths'] ?? []).join('\n'))
  }, [settings])

  useEffect(() => {
    reset()
  }, [reset])

  async function save(e: React.FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError('')
    // PUT rather than PATCH: the form holds every field, so the body IS the
    // complete intended state — and the same call creates the row when the
    // workspace has none, which is the ordinary starting point.
    try {
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}/inventory/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          data: {
            attributes: {
              'include-platform': includePlatform,
              'repo-url': repoUrl.trim(),
              branch: branch.trim(),
              'working-directory': workingDirectory.trim(),
              'ignore-paths': ignorePaths
                .split('\n')
                .map((s) => s.trim())
                .filter(Boolean),
            },
            relationships: {
              'vcs-connection': {
                data: vcsId ? { id: vcsId, type: 'vcs-connections' } : null,
              },
            },
          },
        }),
      })
      if (!res.ok) {
        setError(await parseApiError(res, t('errors.settings')))
        return
      }
      setEditing(false)
      onReload()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.settings'))
    } finally {
      setSaving(false)
    }
  }

  async function clear() {
    if (!confirmDelete(t('settings.confirmClear'))) return
    setError('')
    const res = await apiFetch(`/api/v1/workspaces/${workspaceId}/inventory/settings`, {
      method: 'DELETE',
    })
    if (!res.ok && res.status !== 404) {
      setError(await parseApiError(res, t('errors.settings')))
      return
    }
    setEditing(false)
    onReload()
  }

  if (loading) return <LoadingSpinner />

  const conn = settings
    ? conns.find((c) => c.id === settings.relationships['vcs-connection']?.data?.id)
    : undefined

  return (
    <Section heading={t('settings.heading')} body={t('settings.body')}>
      <ErrorText message={error} />

      {!editing && (
        <>
          {!settings && <p className="mt-3 text-sm text-slate-400">{t('settings.absent')}</p>}
          {settings && (
            <dl className="mt-3 space-y-2 text-sm">
              <div className="flex flex-wrap items-baseline gap-2">
                <dt className="text-slate-500">{t('settings.includePlatform')}</dt>
                <dd className="text-slate-200">
                  {settings.attributes['include-platform']
                    ? t('settings.included')
                    : t('settings.excluded')}
                </dd>
              </div>
              <div className="flex flex-wrap items-baseline gap-2">
                <dt className="text-slate-500">{t('settings.vcsConnection')}</dt>
                <dd className="min-w-0 break-words text-slate-200">
                  {conn
                    ? conn.attributes.name
                    : settings.relationships['vcs-connection']?.data?.id || t('settings.vcsNone')}
                </dd>
              </div>
              {(['repo-url', 'branch', 'working-directory'] as const).map((field) => (
                <div key={field} className="flex flex-wrap items-baseline gap-2">
                  <dt className="text-slate-500">{t(`settings.field.${field}`)}</dt>
                  <dd className="min-w-0 font-mono text-xs break-all text-slate-300" dir="ltr">
                    {settings.attributes[field] || t('settings.unset')}
                  </dd>
                </div>
              ))}
              <div className="flex flex-wrap items-baseline gap-2">
                <dt className="text-slate-500">{t('settings.ignorePaths')}</dt>
                <dd className="min-w-0 font-mono text-xs break-all text-slate-300" dir="ltr">
                  {settings.attributes['ignore-paths'].length === 0
                    ? t('settings.unset')
                    : settings.attributes['ignore-paths'].join(', ')}
                </dd>
              </div>
            </dl>
          )}
          <div className="mt-3 flex flex-wrap gap-2">
            <button type="button" onClick={() => setEditing(true)} className={`${BTN} py-2.5`}>
              {settings ? t('actions.edit') : t('settings.create')}
            </button>
            {settings && (
              <button type="button" onClick={clear} className={`${BTN_DANGER} py-2.5`}>
                {t('settings.clear')}
              </button>
            )}
          </div>
        </>
      )}

      {editing && (
        // `min-w-0` is required on a fieldset: it defaults to
        // `min-inline-size: min-content`, so without this it refuses to shrink
        // below its content and pushes the page sideways at phone width however
        // narrow its children are.
        <form onSubmit={save} className="mt-3">
          <fieldset className="min-w-0 space-y-3 border-0 p-0">
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={includePlatform}
                onChange={(e) => setIncludePlatform(e.target.checked)}
                className="rounded border-slate-600 bg-slate-900"
              />
              {t('settings.includePlatform')}
            </label>
            <p className="text-xs text-slate-500">{t('settings.includePlatformHelp')}</p>

            <div className="min-w-0">
              <label htmlFor="inventory-vcs" className="block text-xs text-slate-500">
                {t('settings.vcsConnection')}
              </label>
              <select
                id="inventory-vcs"
                value={vcsId}
                onChange={(e) => setVcsId(e.target.value)}
                className={`mt-1 ${INPUT}`}
              >
                <option value="">{t('settings.vcsNone')}</option>
                {conns.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.attributes.name}
                  </option>
                ))}
              </select>
            </div>

            <div className="min-w-0">
              <label htmlFor="inventory-repo" className="block text-xs text-slate-500">
                {t('settings.field.repo-url')}
              </label>
              <input
                id="inventory-repo"
                type="text"
                value={repoUrl}
                onChange={(e) => setRepoUrl(e.target.value)}
                dir="ltr"
                className={`mt-1 ${INPUT} font-mono`}
              />
            </div>

            <div className="min-w-0">
              <label htmlFor="inventory-branch" className="block text-xs text-slate-500">
                {t('settings.field.branch')}
              </label>
              <input
                id="inventory-branch"
                type="text"
                value={branch}
                onChange={(e) => setBranch(e.target.value)}
                dir="ltr"
                className={`mt-1 ${INPUT} font-mono`}
              />
              <p className="mt-1 text-xs text-slate-500">{t('settings.branchHelp')}</p>
            </div>

            <div className="min-w-0">
              <label htmlFor="inventory-workdir" className="block text-xs text-slate-500">
                {t('settings.field.working-directory')}
              </label>
              <input
                id="inventory-workdir"
                type="text"
                value={workingDirectory}
                onChange={(e) => setWorkingDirectory(e.target.value)}
                dir="ltr"
                className={`mt-1 ${INPUT} font-mono`}
              />
            </div>

            <div className="min-w-0">
              <label htmlFor="inventory-ignore" className="block text-xs text-slate-500">
                {t('settings.ignorePaths')}
              </label>
              <textarea
                id="inventory-ignore"
                value={ignorePaths}
                onChange={(e) => setIgnorePaths(e.target.value)}
                rows={3}
                dir="ltr"
                className={`mt-1 ${INPUT} font-mono`}
              />
              <p className="mt-1 text-xs text-slate-500">{t('settings.ignorePathsHelp')}</p>
            </div>

            <div className="flex flex-wrap gap-2">
              <button type="submit" disabled={saving} className={BTN_PRIMARY}>
                {saving ? t('actions.saving') : t('actions.save')}
              </button>
              <button
                type="button"
                onClick={() => {
                  setEditing(false)
                  reset()
                }}
                className={`${BTN} py-2.5`}
              >
                {t('actions.cancel')}
              </button>
            </div>
          </fieldset>
        </form>
      )}
    </Section>
  )
}

/* ── The tab ────────────────────────────────────────────────────────────────- */

const VIEWS = ['hosts', 'groups', 'vars', 'resolved', 'settings'] as const
type View = (typeof VIEWS)[number]

/**
 * `?inv=` carries the sub-view, so a reload, the back button and a shared link
 * all land where the reader was. One parameter is enough because the drilled-in
 * values are prefixed ids — `invhost-…` / `invgroup-…` — which cannot collide
 * with a view name.
 */
function parseView(raw: string | null): { view: View; hostId: string; groupId: string } {
  if (raw && raw.startsWith('invhost-')) return { view: 'hosts', hostId: raw, groupId: '' }
  if (raw && raw.startsWith('invgroup-')) return { view: 'groups', hostId: '', groupId: raw }
  const view = (VIEWS as readonly string[]).includes(raw ?? '') ? (raw as View) : 'hosts'
  return { view, hostId: '', groupId: '' }
}

export function InventoryPanel({ workspaceId }: { workspaceId: string }) {
  const t = useTranslations('workspaceDetail.inventory')
  const router = useRouter()
  const searchParams = useSearchParams()

  const { view, hostId, groupId } = parseView(searchParams.get('inv'))

  const [hosts, setHosts] = useState<InventoryHost[]>([])
  const [groups, setGroups] = useState<InventoryGroup[]>([])
  const [settings, setSettings] = useState<InventorySettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  // Bumped by every write, so the resolved view re-resolves against the rows as
  // they now are rather than showing an answer the edit has already invalidated.
  const [generation, setGeneration] = useState(0)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [h, g] = await Promise.all([
        fetchAllPages<InventoryHost>(`/api/v1/workspaces/${workspaceId}/inventory/hosts`),
        fetchAllPages<InventoryGroup>(`/api/v1/workspaces/${workspaceId}/inventory/groups`),
      ])
      setHosts(h)
      setGroups(g)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.load'))
    }
    try {
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}/inventory/settings`)
      // 404 is the default state, not a failure: the settings row exists only
      // to bind a VCS source or to switch the declared rows off.
      setSettings(res.ok ? ((await res.json()).data ?? null) : null)
    } catch {
      setSettings(null)
    }
    setLoading(false)
  }, [workspaceId, t])

  useEffect(() => {
    // Fetching from the API on mount is what an effect is for, and the loader
    // sets its state from the response — the lint cannot see through the await
    // and reads the whole body as synchronous.
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data fetch, not derived state
    load()
  }, [load])

  const reload = useCallback(() => {
    setGeneration((n) => n + 1)
    load()
  }, [load])

  function go(target: string) {
    router.replace(`?tab=inventory&inv=${encodeURIComponent(target)}`, { scroll: false })
  }

  const host = hosts.find((h) => h.id === hostId)
  const group = groups.find((g) => g.id === groupId)

  return (
    // Named so a test can assert the tab rendered without depending on a
    // locale's heading text — the RTL and novelty catalogues legitimately make
    // every visible string different.
    <div data-testid="inventory-tab" className="space-y-6">
      <div className={SECTION}>
        <h3 className={HEADING}>{t('scope.heading')}</h3>
        <p className={BODY}>{t('scope.body')}</p>
      </div>

      {/* A wrapping button row rather than a scrolling strip: five entries fit
          at phone width once they are allowed to wrap, and an inner scroller
          would hide whichever one the reader needs. */}
      <nav aria-label={t('nav.label')} className="flex flex-wrap gap-2">
        {VIEWS.map((v) => (
          <button
            key={v}
            type="button"
            onClick={() => go(v)}
            aria-current={view === v && !hostId && !groupId ? 'page' : undefined}
            className={
              'px-3 py-2 rounded-lg text-sm font-medium transition-colors ' +
              (view === v && !hostId && !groupId
                ? 'bg-brand-600 text-white'
                : 'bg-slate-800 text-slate-300 hover:bg-slate-700')
            }
          >
            {t(`nav.${v}`)}
          </button>
        ))}
      </nav>

      <ErrorText message={error} />

      {(hostId || groupId) && (
        <button type="button" onClick={() => go(hostId ? 'hosts' : 'groups')} className={BTN}>
          {hostId ? t('hostDetail.back') : t('groupDetail.back')}
        </button>
      )}

      {hostId &&
        (host ? (
          <HostDetailView host={host} groups={groups} onReload={reload} />
        ) : loading ? (
          <LoadingSpinner />
        ) : (
          <EmptyState message={t('hostDetail.gone')} />
        ))}

      {groupId &&
        (group ? (
          <GroupDetailView group={group} hosts={hosts} groups={groups} onReload={reload} />
        ) : loading ? (
          <LoadingSpinner />
        ) : (
          <EmptyState message={t('groupDetail.gone')} />
        ))}

      {!hostId && !groupId && view === 'hosts' && (
        <HostsView
          workspaceId={workspaceId}
          hosts={hosts}
          loading={loading}
          onOpen={go}
          onReload={reload}
        />
      )}

      {!hostId && !groupId && view === 'groups' && (
        <GroupsView
          workspaceId={workspaceId}
          groups={groups}
          loading={loading}
          onOpen={go}
          onReload={reload}
        />
      )}

      {!hostId && !groupId && view === 'vars' && (
        <VariableTable
          heading={t('globalVars.heading')}
          body={t('globalVars.body')}
          listUrl={`/api/v1/workspaces/${workspaceId}/inventory/vars`}
          createUrl={`/api/v1/workspaces/${workspaceId}/inventory/vars`}
          rowBase="/api/v1/inventory-global-vars"
          reloadKey={generation}
          onChanged={reload}
        />
      )}

      {!hostId && !groupId && view === 'resolved' && (
        <ResolvedView workspaceId={workspaceId} reloadKey={generation} />
      )}

      {!hostId && !groupId && view === 'settings' && (
        <SettingsView
          workspaceId={workspaceId}
          settings={settings}
          loading={loading}
          onReload={reload}
        />
      )}
    </div>
  )
}
