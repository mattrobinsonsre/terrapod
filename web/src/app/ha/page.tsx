'use client'

import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { usePollingInterval } from '@/lib/use-polling-interval'
import { getAuthState } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

/**
 * High availability (#1163, #1165).
 *
 * Reached by clicking the nav-bar indicator, and open to every signed-in user —
 * which node you are talking to is context, not an administrative task, and the
 * person whose next write a follower is about to refuse is precisely who needs
 * it. The in-cluster half is the exception: it describes the deployment rather
 * than this node, so it stays admin/audit and says so.
 *
 * Read-only by design. There is no control here that changes a role: failover is
 * moving DNS, and a button that looked like it could fail over would be actively
 * dangerous.
 *
 * The page's job is to be readable under pressure, because that is when it is
 * read. Three things it must never do:
 *
 *  - render a SAMPLED blob-readiness result as a clean estate;
 *  - render a class nobody checked as a class that passed;
 *  - render a copy cycle that stopped at its budget as finished.
 */

interface Component {
  name: string
  ready: number
  desired: number
  nodes: number | null
  zones: number | null
  pdb: boolean
  'pdb-permits-disruption': boolean | null
}

interface Pool {
  id: string
  name: string
  status: string
  listeners: number
}

interface Finding {
  component: string
  kind: string
  detail: string
}

interface HAStatus {
  'node-id': string | null
  role: string
  'peer-configured': boolean
  'replication-enabled': boolean
  'last-sync-at': string | null
  'seconds-since-last-sync': number | null
  'backfilling-classes': string[]
  'in-sync': boolean
  'events-retained': number
  'oldest-event-age-seconds': number | null
  'retention-seconds': number
  'replicated-classes': string[]
  components: Component[]
  'schedulable-nodes': number | null
  'cluster-zones': number | null
  'ha-findings': Finding[]
  'components-restricted'?: boolean
  'components-sampled-at': string | null
  'components-unavailable-reason': string | null
  'single-replica-components': string[]
}

function duration(seconds: number | null): string {
  if (seconds === null) return '—'
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86400)}d`
}

export default function HAPage() {
  const t = useTranslations('adminHa')
  // A role or tier the catalog doesn't know must render as itself, not throw
  // MISSING_MESSAGE and take the whole page down. This page is read during an
  // incident; a crash is the worst possible failure mode for it.
  const label = (key: string, fallback: string) =>
    t.has(key as 'role.leader') ? t(key as 'role.leader') : fallback
  const router = useRouter()
  const [status, setStatus] = useState<HAStatus | null>(null)
  const [pools, setPools] = useState<Pool[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      const resp = await apiFetch('/api/terrapod/v1/ha/status')
      if (!resp.ok) throw new Error(await resp.text())
      const body = await resp.json()
      setStatus(body.data.attributes)

      // Runner readiness duplicates the agent-pools page, deliberately: "can
      // this node actually run anything" is part of the HA question, and an
      // operator deciding whether to fail over should not have to visit two
      // pages to answer it.
      //
      // Read from the pools endpoint rather than a new attribute on /ha/status:
      // it already derives online/offline from live listeners AND already
      // filters by pool RBAC, so a second implementation could only drift from
      // it — and would have to re-decide who may see which pool.
      const poolsResp = await apiFetch('/api/terrapod/v1/agent-pools')
      if (poolsResp.ok) {
        const list = (await poolsResp.json()).data as Array<{
          id: string
          attributes: Record<string, string | number>
        }>
        setPools(
          list.map((p) => ({
            id: p.id,
            name: String(p.attributes.name),
            status: String(p.attributes.status || 'offline'),
            // The server counts only listeners that could actually take work,
            // from the same predicate `status` comes from — so the number and
            // the word can never disagree.
            listeners: Number(p.attributes['listener-count'] ?? 0),
          })),
        )
      } else {
        setPools(null)
      }
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const auth = getAuthState()
    if (!auth?.token) {
      router.push('/login')
      return
    }
    void load()
  }, [router, load])

  // `/ha/status` is answered from local state in milliseconds, so polling it is
  // cheap and keeps a role change visible without a reload. Blob readiness is
  // deliberately NOT polled — see below.
  usePollingInterval(true, 15000, load)

  if (loading) return <LoadingSpinner />

  const singleNode = status !== null && !status['peer-configured']

  return (
    <div className="min-h-dvh bg-slate-950 text-slate-100">
      <NavBar />
      <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6">
        <PageHeader title={t('title')} description={t('description')} />

        {error && <ErrorBanner message={error} />}

        {status && (
          <>
            {/* Role. A follower must be unmistakable — an operator reading the
                wrong node's UI as the leader is the mistake this prevents. */}
            <section className="mb-6 rounded-xl border border-slate-800 bg-slate-900/50 p-4 sm:p-6">
              <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <div className="text-xs uppercase tracking-wide text-slate-400">
                    {t('roleLabel')}
                  </div>
                  <div
                    className={`mt-1 text-2xl font-semibold ${
                      status.role === 'leader' ? 'text-emerald-300' : 'text-amber-300'
                    }`}
                  >
                    {label(`role.${status.role}`, status.role)}
                  </div>
                  {status.role === 'follower' && (
                    <p className="mt-1 max-w-prose text-sm text-amber-200/80">
                      {t('followerNote')}
                    </p>
                  )}
                </div>
                <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
                  <dt className="text-slate-400">{t('nodeId')}</dt>
                  <dd className="font-mono">{status['node-id'] || '—'}</dd>
                  <dt className="text-slate-400">{t('peer')}</dt>
                  <dd>{status['peer-configured'] ? t('configured') : t('notConfigured')}</dd>
                </dl>
              </div>
            </section>

            {/* A single node is the overwhelming majority of installs. Say so
                plainly rather than rendering a wall of unknowns that reads as
                broken. */}
            {singleNode ? (
              <section className="mb-6 rounded-xl border border-slate-800 bg-slate-900/50 p-6">
                <h2 className="text-lg font-medium">{t('singleNode.title')}</h2>
                <p className="mt-2 max-w-prose text-sm text-slate-400">
                  {t('singleNode.body')}
                </p>
              </section>
            ) : (
              <section className="mb-6 rounded-xl border border-slate-800 bg-slate-900/50 p-4 sm:p-6">
                <h2 className="mb-4 text-lg font-medium">{t('replication.title')}</h2>
                {!status['replication-enabled'] ? (
                  <p className="text-sm text-slate-400">{t('replication.disabled')}</p>
                ) : (
                  <>
                    {/* Backfilling means NOT in sync however recent the last
                        cycle was. A green tick beside a fresh timestamp would be
                        the wrong answer. */}
                    <div
                      className={`mb-4 rounded-lg px-3 py-2 text-sm ${
                        status['in-sync']
                          ? 'bg-emerald-950/40 text-emerald-200'
                          : 'bg-amber-950/40 text-amber-200'
                      }`}
                    >
                      {status['in-sync']
                        ? t('replication.inSync')
                        : t('replication.notInSync')}
                    </div>
                    <dl className="grid grid-cols-1 gap-x-8 gap-y-2 text-sm sm:grid-cols-2">
                      <div className="flex justify-between gap-4">
                        <dt className="text-slate-400">{t('replication.sinceSync')}</dt>
                        <dd className="tabular-nums">
                          {duration(status['seconds-since-last-sync'])}
                        </dd>
                      </div>
                      <div className="flex justify-between gap-4">
                        <dt className="text-slate-400">{t('replication.backfilling')}</dt>
                        <dd className="tabular-nums">
                          {status['backfilling-classes'].length}
                        </dd>
                      </div>
                      <div className="flex justify-between gap-4">
                        <dt className="text-slate-400">{t('replication.retained')}</dt>
                        <dd className="tabular-nums">{status['events-retained']}</dd>
                      </div>
                      <div className="flex justify-between gap-4">
                        <dt className="text-slate-400">{t('replication.oldestEvent')}</dt>
                        <dd className="tabular-nums">
                          {duration(status['oldest-event-age-seconds'])}
                          <span className="text-slate-500">
                            {' / '}
                            {duration(status['retention-seconds'])}
                          </span>
                        </dd>
                      </div>
                    </dl>
                    {status['backfilling-classes'].length > 0 && (
                      <ul className="mt-3 flex flex-wrap gap-2">
                        {status['backfilling-classes'].map((c) => (
                          <li
                            key={c}
                            className="rounded bg-amber-950/40 px-2 py-1 font-mono text-xs text-amber-200"
                          >
                            {c}
                          </li>
                        ))}
                      </ul>
                    )}
                  </>
                )}
              </section>
            )}

            {/* Component readiness — findings appear ONLY where the cluster
                could have done better, so a one-node cluster shows none. */}
            <section className="rounded-xl border border-slate-800 bg-slate-900/50 p-4 sm:p-6">
              <h2 className="mb-4 text-lg font-medium">{t('components.title')}</h2>
              {status['components-restricted'] ? (
                // "You may not see this" is a different statement from "I
                // cannot see this", and from "nothing is running". Saying it
                // outright is the only one of the three that is true here.
                <p className="text-sm text-slate-400">{t('components.restricted')}</p>
              ) : status['components-unavailable-reason'] ? (
                <p className="text-sm text-slate-400">
                  {t('components.unavailable', {
                    reason: status['components-unavailable-reason'],
                  })}
                </p>
              ) : status.components.length === 0 ? (
                <p className="text-sm text-slate-400">{t('components.none')}</p>
              ) : (
                <>
                  <ul className="space-y-2">
                    {status.components.map((c) => (
                      <li
                        key={c.name}
                        className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-slate-950/50 px-3 py-2 text-sm"
                      >
                        <span className="font-mono">{c.name}</span>
                        <span className="flex items-center gap-3">
                          <span
                            className={`tabular-nums ${
                              c.ready < c.desired ? 'text-amber-300' : 'text-slate-300'
                            }`}
                          >
                            {t('components.ready', { ready: c.ready, desired: c.desired })}
                          </span>
                          {c.pdb && c['pdb-permits-disruption'] === false && (
                            <span className="rounded bg-amber-950/40 px-2 py-0.5 text-xs text-amber-200">
                              {t('components.pdbBlocks')}
                            </span>
                          )}
                        </span>
                      </li>
                    ))}
                  </ul>
                  {status['ha-findings'].length > 0 && (
                    <ul className="mt-4 space-y-2">
                      {status['ha-findings'].map((f, i) => (
                        <li
                          key={`${f.component}-${f.kind}-${i}`}
                          className="rounded-lg bg-amber-950/30 px-3 py-2 text-sm text-amber-100"
                        >
                          <span className="font-mono text-xs text-amber-300">
                            {f.component}
                          </span>{' '}
                          {f.detail}
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              )}
            </section>

            {/* Runner readiness. A node that replicates flawlessly and has no
                live listener cannot run a single plan, so this belongs beside
                the rest of the failover decision rather than one page away. */}
            <section className="mt-6 rounded-xl border border-slate-800 bg-slate-900/50 p-4 sm:p-6">
              <h2 className="text-lg font-medium">{t('listeners.title')}</h2>
              <p className="mt-1 max-w-prose text-sm text-slate-400">
                {t('listeners.intro')}
              </p>
              {pools === null ? (
                <p className="mt-4 text-sm text-slate-400">{t('listeners.unavailable')}</p>
              ) : pools.length === 0 ? (
                <p className="mt-4 text-sm text-slate-400">{t('listeners.none')}</p>
              ) : (
                <>
                  {pools.some((p) => p.status !== 'online') && (
                    // Named rather than left for the reader to spot: a pool with
                    // no live listener is the thing being looked for.
                    <p className="mt-4 rounded-lg bg-amber-950/40 px-3 py-2 text-sm text-amber-200">
                      {t('listeners.offlineWarning', {
                        pools: pools
                          .filter((p) => p.status !== 'online')
                          .map((p) => p.name)
                          .join(', '),
                      })}
                    </p>
                  )}
                  <ul className="mt-4 space-y-2">
                    {pools.map((p) => (
                      <li
                        key={p.id}
                        className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-slate-950/50 px-3 py-2 text-sm"
                      >
                        <Link
                          href={`/admin/agent-pools/${p.id}`}
                          className="font-mono text-brand-300 hover:text-brand-200"
                        >
                          {p.name}
                        </Link>
                        <span
                          className={`tabular-nums ${
                            p.status === 'online' ? 'text-emerald-300' : 'text-amber-300'
                          }`}
                        >
                          {p.status === 'online'
                            ? t('listeners.count', { count: p.listeners })
                            : t('listeners.offline')}
                        </span>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </section>

          </>
        )}
      </main>
    </div>
  )
}
