// Labels browser — read-only cross-entity view over the
// /api/terrapod/v1/labels endpoints. Three view states, all on `/labels`,
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
import { useTranslations } from 'next-intl'
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

// Translation keys (under the `labels` namespace) for each entity type's
// display name. Resolved via `useTranslations` at the call sites — module
// scope can't call the hook.
const ENTITY_LABEL_KEYS: Record<EntityType, string> = {
  'workspaces': 'entity.workspaces',
  'agent-pools': 'entity.agentPools',
  'registry-modules': 'entity.modules',
  'registry-providers': 'entity.providers',
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
  const t = useTranslations('labels')
  const [keys, setKeys] = useState<KeyEntry[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    apiFetch('/api/terrapod/v1/labels')
      .then(async r => {
        if (!r.ok) throw new Error(t('loadKeysFailed', { status: r.status }))
        return r.json()
      })
      .then(d => alive && setKeys(d.data ?? []))
      .catch(e => alive && setError(e instanceof Error ? e.message : String(e)))
    return () => { alive = false }
  }, [t])

  if (error) return <ErrorBanner message={error} />
  if (keys === null) return <LoadingSpinner />
  if (keys.length === 0) {
    return (
      <EmptyState message={t('emptyKeys')} />
    )
  }

  return (
    <div className="overflow-hidden rounded-xl border border-slate-800">
      <table className="w-full text-sm">
        <thead className="bg-slate-900/50 text-slate-400">
          <tr>
            <th className="px-4 py-3 text-left font-medium">{t('columns.labelKey')}</th>
            <th className="px-4 py-3 text-right font-medium">{t('columns.distinctValues')}</th>
            {ENTITY_TYPES.map(type => (
              <th key={type} className="px-4 py-3 text-right font-medium">{t(ENTITY_LABEL_KEYS[type])}</th>
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
  const t = useTranslations('labels')
  const [values, setValues] = useState<ValueEntry[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    apiFetch(`/api/terrapod/v1/labels/${encodeURIComponent(labelKey)}`)
      .then(async r => {
        if (!r.ok) throw new Error(t('loadValuesFailed', { key: labelKey, status: r.status }))
        return r.json()
      })
      .then(d => alive && setValues(d.data ?? []))
      .catch(e => alive && setError(e instanceof Error ? e.message : String(e)))
    return () => { alive = false }
  }, [labelKey, t])

  return (
    <>
      <Breadcrumb items={[{ label: t('title'), href: '/labels' }, { label: labelKey }]} />
      {error ? (
        <ErrorBanner message={error} />
      ) : values === null ? (
        <LoadingSpinner />
      ) : values.length === 0 ? (
        <EmptyState message={t('emptyValues', { key: labelKey })} />
      ) : (
        <div className="overflow-hidden rounded-xl border border-slate-800">
          <table className="w-full text-sm">
            <thead className="bg-slate-900/50 text-slate-400">
              <tr>
                <th className="px-4 py-3 text-left font-medium">{t('columns.value')}</th>
                <th className="px-4 py-3 text-right font-medium">{t('columns.total')}</th>
                {ENTITY_TYPES.map(type => (
                  <th key={type} className="px-4 py-3 text-right font-medium">{t(ENTITY_LABEL_KEYS[type])}</th>
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
  const t = useTranslations('labels')
  const [groups, setGroups] = useState<Record<EntityType, EntityRow[]> | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    apiFetch(
      `/api/terrapod/v1/labels/${encodeURIComponent(labelKey)}/${encodeURIComponent(labelValue)}`,
    )
      .then(async r => {
        if (!r.ok) throw new Error(t('loadEntitiesFailed', { status: r.status }))
        return r.json()
      })
      .then(d => alive && setGroups(d.data ?? null))
      .catch(e => alive && setError(e instanceof Error ? e.message : String(e)))
    return () => { alive = false }
  }, [labelKey, labelValue, t])

  return (
    <>
      <Breadcrumb
        items={[
          { label: t('title'), href: '/labels' },
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
                  {t(ENTITY_LABEL_KEYS[type])}
                  <span className="text-slate-500">({rows.length})</span>
                </h2>
                {rows.length === 0 ? (
                  <p className="px-4 py-3 text-sm text-slate-500 italic border border-slate-800 rounded-lg">
                    {t('none')}
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
  const t = useTranslations('labels')
  return (
    <div className="min-h-dvh bg-slate-950 text-slate-200">
      <NavBar />
      <main className="mx-auto max-w-6xl px-4 py-8">
        <PageHeader
          title={t('title')}
          description={t('description')}
        />
        {/* Deprecated surface — removed from the primary nav; reachable by
            direct link only, pending removal in a future release. */}
        <div
          role="status"
          className="mb-6 rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-200"
        >
          <span className="font-semibold">{t('deprecated.badge')}</span>{' '}
          {t.rich('deprecated.body', {
            workspacesLink: (chunks) => (
              <Link href="/workspaces" className="underline hover:text-amber-100">
                {chunks}
              </Link>
            ),
            issueLink: (chunks) => (
              <a
                href="https://github.com/mattrobinsonsre/terrapod/issues/new"
                target="_blank"
                rel="noopener noreferrer"
                className="underline hover:text-amber-100"
              >
                {chunks}
              </a>
            ),
          })}
        </div>
        <Suspense fallback={<LoadingSpinner />}>
          <LabelsBrowserInner />
        </Suspense>
      </main>
    </div>
  )
}
