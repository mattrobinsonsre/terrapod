// Labels browser — read-only cross-entity view over the
// /api/v2/labels endpoints. Three view states, all on `/labels`,
// driven by query string:
//
//   /labels                    → list distinct label keys
//   /labels?key=foo            → list distinct values for `foo`
//   /labels?key=foo&value=bar  → list entities tagged exactly foo=bar
//
// Read-only by design (matching the API). Editing labels happens on
// each entity's own edit page; this surface deliberately doesn't try
// to be a labels admin.

'use client'

import { Suspense, useEffect, useState } from 'react'
import Link from 'next/link'
import { useRouter, useSearchParams } from 'next/navigation'
import { Layers, Server, Package, Blocks, ChevronRight } from 'lucide-react'

import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { getAuthState } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

// Stable order — matches the backend ENTITY_TYPES tuple. Used for both
// the label-key drilldown table and the entity grouping.
const ENTITY_TYPES = ['workspaces', 'agent-pools', 'registry-modules', 'registry-providers'] as const
type EntityType = (typeof ENTITY_TYPES)[number]

const ENTITY_LABELS: Record<EntityType, string> = {
  'workspaces': 'Workspaces',
  'agent-pools': 'Agent pools',
  'registry-modules': 'Modules',
  'registry-providers': 'Providers',
}

const ENTITY_ICONS: Record<EntityType, typeof Layers> = {
  'workspaces': Layers,
  'agent-pools': Server,
  'registry-modules': Package,
  'registry-providers': Blocks,
}

interface KeyEntry {
  key: string
  'value-count': number
  'entity-counts': Record<EntityType, number>
}

interface ValueEntry {
  value: string
  'entity-counts': Record<EntityType, number>
}

interface EntityRow {
  type: EntityType
  id: string
  name: string
  namespace?: string
  provider?: string
  labels: Record<string, string>
}

// Map an entity row to the URL on its own detail page. We deliberately
// link OUT to each entity's own page rather than rendering details
// inline — keeps the labels surface focused on browse + drill-down.
function entityHref(row: EntityRow): string {
  switch (row.type) {
    case 'workspaces':
      return `/workspaces/${row.id.replace(/^ws-/, '')}`
    case 'agent-pools':
      return `/admin/agent-pools/${row.id.replace(/^apool-/, '')}`
    case 'registry-modules':
      return `/registry/modules/${row.namespace ?? 'default'}/${row.name}/${row.provider ?? 'aws'}`
    case 'registry-providers':
      return `/registry/providers/${row.namespace ?? 'default'}/${row.name}`
  }
}

function totalEntities(counts: Record<EntityType, number>): number {
  return ENTITY_TYPES.reduce((acc, t) => acc + (counts[t] ?? 0), 0)
}

// ── Inner component: reads useSearchParams, dispatches by URL state ──
function LabelsBrowserInner() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const key = searchParams.get('key')
  const value = searchParams.get('value')

  useEffect(() => {
    if (!getAuthState()) router.push('/login')
  }, [router])

  // Render is one of three sub-views. Each is its own component so
  // hooks (loaders) reset cleanly between view transitions.
  if (!key) return <KeysView />
  if (!value) return <ValuesView labelKey={key} />
  return <EntitiesView labelKey={key} labelValue={value} />
}

// ── View 1: list distinct label keys ─────────────────────────────────

function KeysView() {
  const [keys, setKeys] = useState<KeyEntry[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    apiFetch('/api/v2/labels')
      .then(async r => {
        if (!r.ok) throw new Error(`Failed to load labels (${r.status})`)
        return r.json()
      })
      .then(d => alive && setKeys(d.data ?? []))
      .catch(e => alive && setError(e instanceof Error ? e.message : String(e)))
    return () => { alive = false }
  }, [])

  if (error) return <ErrorBanner message={error} />
  if (keys === null) return <LoadingSpinner />
  if (keys.length === 0) {
    return (
      <EmptyState message="No labels in use yet. Label any workspace, agent pool, registry module, or registry provider to start grouping things here." />
    )
  }

  return (
    <div className="overflow-hidden rounded-xl border border-slate-800">
      <table className="w-full text-sm">
        <thead className="bg-slate-900/50 text-slate-400">
          <tr>
            <th className="px-4 py-3 text-left font-medium">Label key</th>
            <th className="px-4 py-3 text-right font-medium">Distinct values</th>
            {ENTITY_TYPES.map(t => (
              <th key={t} className="px-4 py-3 text-right font-medium">{ENTITY_LABELS[t]}</th>
            ))}
            <th className="w-8" aria-hidden />
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800">
          {keys.map(entry => (
            <tr key={entry.key} className="hover:bg-slate-900/30 transition-colors">
              <td className="px-4 py-3">
                <Link
                  href={`/labels?key=${encodeURIComponent(entry.key)}`}
                  className="font-mono text-slate-100 hover:text-blue-400"
                >
                  {entry.key}
                </Link>
              </td>
              <td className="px-4 py-3 text-right text-slate-300">{entry['value-count']}</td>
              {ENTITY_TYPES.map(t => (
                <td key={t} className="px-4 py-3 text-right text-slate-400">
                  {entry['entity-counts'][t] ?? 0}
                </td>
              ))}
              <td className="px-2 text-slate-600">
                <ChevronRight size={16} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── View 2: list distinct values for a key ───────────────────────────

function ValuesView({ labelKey }: { labelKey: string }) {
  const [values, setValues] = useState<ValueEntry[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    apiFetch(`/api/v2/labels/${encodeURIComponent(labelKey)}`)
      .then(async r => {
        if (!r.ok) throw new Error(`Failed to load values for ${labelKey} (${r.status})`)
        return r.json()
      })
      .then(d => alive && setValues(d.data ?? []))
      .catch(e => alive && setError(e instanceof Error ? e.message : String(e)))
    return () => { alive = false }
  }, [labelKey])

  return (
    <>
      <Breadcrumb items={[{ label: 'Labels', href: '/labels' }, { label: labelKey }]} />
      {error ? (
        <ErrorBanner message={error} />
      ) : values === null ? (
        <LoadingSpinner />
      ) : values.length === 0 ? (
        <EmptyState message={`No values for "${labelKey}". Either no readable entity carries this label, or it was removed since the last view.`} />
      ) : (
        <div className="overflow-hidden rounded-xl border border-slate-800">
          <table className="w-full text-sm">
            <thead className="bg-slate-900/50 text-slate-400">
              <tr>
                <th className="px-4 py-3 text-left font-medium">Value</th>
                <th className="px-4 py-3 text-right font-medium">Total</th>
                {ENTITY_TYPES.map(t => (
                  <th key={t} className="px-4 py-3 text-right font-medium">{ENTITY_LABELS[t]}</th>
                ))}
                <th className="w-8" aria-hidden />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {values.map(entry => (
                <tr key={entry.value} className="hover:bg-slate-900/30 transition-colors">
                  <td className="px-4 py-3">
                    <Link
                      href={`/labels?key=${encodeURIComponent(labelKey)}&value=${encodeURIComponent(entry.value)}`}
                      className="font-mono text-slate-100 hover:text-blue-400"
                    >
                      {entry.value}
                    </Link>
                  </td>
                  <td className="px-4 py-3 text-right text-slate-300">{totalEntities(entry['entity-counts'])}</td>
                  {ENTITY_TYPES.map(t => (
                    <td key={t} className="px-4 py-3 text-right text-slate-400">
                      {entry['entity-counts'][t] ?? 0}
                    </td>
                  ))}
                  <td className="px-2 text-slate-600">
                    <ChevronRight size={16} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

// ── View 3: list entities tagged with a specific key=value ──────────

function EntitiesView({ labelKey, labelValue }: { labelKey: string; labelValue: string }) {
  const [groups, setGroups] = useState<Record<EntityType, EntityRow[]> | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    apiFetch(
      `/api/v2/labels/${encodeURIComponent(labelKey)}/${encodeURIComponent(labelValue)}`,
    )
      .then(async r => {
        if (!r.ok) throw new Error(`Failed to load entities (${r.status})`)
        return r.json()
      })
      .then(d => alive && setGroups(d.data ?? null))
      .catch(e => alive && setError(e instanceof Error ? e.message : String(e)))
    return () => { alive = false }
  }, [labelKey, labelValue])

  return (
    <>
      <Breadcrumb
        items={[
          { label: 'Labels', href: '/labels' },
          { label: labelKey, href: `/labels?key=${encodeURIComponent(labelKey)}` },
          { label: labelValue },
        ]}
      />
      {error ? (
        <ErrorBanner message={error} />
      ) : groups === null ? (
        <LoadingSpinner />
      ) : (
        <div className="space-y-6">
          {ENTITY_TYPES.map(type => {
            const rows = groups[type] ?? []
            const Icon = ENTITY_ICONS[type]
            return (
              <section key={type}>
                <h2 className="mb-2 flex items-center gap-2 text-sm font-medium text-slate-300">
                  <Icon size={14} className="text-slate-500" />
                  {ENTITY_LABELS[type]}
                  <span className="text-slate-500">({rows.length})</span>
                </h2>
                {rows.length === 0 ? (
                  <p className="px-4 py-3 text-sm text-slate-500 italic border border-slate-800 rounded-lg">
                    None.
                  </p>
                ) : (
                  <ul className="divide-y divide-slate-800 rounded-lg border border-slate-800">
                    {rows.map(row => (
                      <li key={row.id}>
                        <Link
                          href={entityHref(row)}
                          className="block px-4 py-3 hover:bg-slate-900/30 transition-colors"
                        >
                          <div className="flex items-center justify-between gap-4">
                            <span className="font-mono text-slate-100">{row.name}</span>
                            <div className="flex flex-wrap gap-1.5">
                              {Object.entries(row.labels).map(([k, v]) => (
                                <span
                                  key={k}
                                  className={
                                    'inline-flex items-center rounded px-1.5 py-0.5 text-xs font-mono ' +
                                    (k === labelKey && v === labelValue
                                      ? 'bg-blue-900/40 text-blue-200'
                                      : 'bg-slate-800 text-slate-400')
                                  }
                                >
                                  {k}: {v}
                                </span>
                              ))}
                            </div>
                          </div>
                        </Link>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            )
          })}
        </div>
      )}
    </>
  )
}

// ── Breadcrumb (small inline component) ──────────────────────────────

function Breadcrumb({ items }: { items: { label: string; href?: string }[] }) {
  return (
    <nav className="mb-4 flex flex-wrap items-center gap-1 text-sm text-slate-400">
      {items.map((item, i) => {
        const isLast = i === items.length - 1
        return (
          <span key={i} className="flex items-center gap-1">
            {item.href && !isLast ? (
              <Link href={item.href} className="hover:text-slate-200">
                {item.label}
              </Link>
            ) : (
              <span className={isLast ? 'font-mono text-slate-200' : 'font-mono'}>
                {item.label}
              </span>
            )}
            {!isLast && <ChevronRight size={14} className="text-slate-600" />}
          </span>
        )
      })}
    </nav>
  )
}

// ── Page wrapper ─────────────────────────────────────────────────────
// `useSearchParams` triggers Next.js's CSR bailout; the Suspense
// boundary keeps the page statically buildable.

export default function LabelsPage() {
  return (
    <div className="min-h-dvh bg-slate-950 text-slate-200">
      <NavBar />
      <main className="mx-auto max-w-6xl px-4 py-8">
        <PageHeader
          title="Labels"
          description="Browse labels in use across workspaces, agent pools, registry modules, and registry providers. Read-only — labels are edited on each entity's own page."
        />
        <Suspense fallback={<LoadingSpinner />}>
          <LabelsBrowserInner />
        </Suspense>
      </main>
    </div>
  )
}
