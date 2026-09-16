'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import {
  CheckCircle2,
  HelpCircle,
  Lock,
  LockOpen,
  PauseCircle,
  XCircle,
  type LucideIcon,
} from 'lucide-react'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { usePollingInterval } from '@/lib/use-polling-interval'
import { getAuthState, isAdminOrAudit } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

/**
 * Vault status (#1663) — admin and audit only.
 *
 * Reads a sample the API takes once a minute; the page never contacts Vault,
 * so polling it is cheap. One card per instance at every width: a card holds
 * the primary signal (reachable, sealed, login) as labelled pills that wrap on
 * a phone, so nothing has to be hidden to fit, and there is no table to
 * dual-render.
 *
 * Every probe field can be null, which means "not known yet" — never sampled,
 * or not attempted (a sealed Vault is never logged in to). It renders as
 * Unknown, never as a failure.
 */

interface LastError {
  class: string
  message: string
  at: string
}

interface Instance {
  name: string
  default: boolean
  address: string
  namespace: string
  'auth-method': string
  'auth-mount': string
  'auth-role': string
  'tls-trust': string
  reachable: boolean | null
  initialized: boolean | null
  sealed: boolean | null
  standby: boolean | null
  version: string
  'health-error': string | null
  'login-ok': boolean | null
  'login-error': string | null
  'ttl-seconds': number | null
  'checked-at': string | null
  'last-error': LastError | null
}

interface Meta {
  enabled: boolean
  'sampled-at': string | null
  'unavailable-reason': string | null
}

type Tone = 'good' | 'bad' | 'warn' | 'neutral'

const TONE: Record<Tone, string> = {
  good: 'bg-emerald-950/50 text-emerald-300 border-emerald-800',
  bad: 'bg-red-950/50 text-red-300 border-red-800',
  warn: 'bg-amber-950/50 text-amber-300 border-amber-800',
  neutral: 'bg-slate-800 text-slate-300 border-slate-600',
}

function Pill({ tone, icon: Icon, text }: { tone: Tone; icon: LucideIcon; text: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-xs font-medium ${TONE[tone]}`}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {text}
    </span>
  )
}

function when(raw: string | null): string {
  if (!raw) return '—'
  const d = new Date(raw)
  return Number.isNaN(d.getTime()) ? raw : d.toLocaleString()
}

function duration(seconds: number | null): string {
  if (seconds === null) return '—'
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86400)}d`
}

const TLS_KEYS: Record<string, string> = {
  'instance-ca': 'tlsInstanceCa',
  'global-bundle': 'tlsGlobalBundle',
  default: 'tlsDefault',
  'skip-verify': 'tlsSkipVerify',
}

export default function VaultStatusPage() {
  const t = useTranslations('adminVault')
  const router = useRouter()
  const [instances, setInstances] = useState<Instance[]>([])
  const [meta, setMeta] = useState<Meta | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      const res = await apiFetch('/api/terrapod/v1/admin/vault')
      if (!res.ok) throw new Error(await res.text())
      const body = await res.json()
      setInstances((body.data ?? []).map((d: { attributes: Instance }) => d.attributes))
      setMeta(body.meta?.vault ?? null)
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!getAuthState()) {
      router.push('/login')
      return
    }
    if (!isAdminOrAudit()) {
      router.push('/')
      return
    }
    void load()
  }, [router, load])

  // The sample refreshes once a minute; polling faster would show nothing new.
  usePollingInterval(true, 30000, load)

  if (loading) return <LoadingSpinner />

  const tlsLabel = (v: string) => (TLS_KEYS[v] ? t(TLS_KEYS[v] as 'tlsDefault') : v)

  return (
    <div className="min-h-dvh bg-slate-950 text-slate-100">
      <NavBar />
      <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6">
        <PageHeader
          title={t('title')}
          description={t('description')}
          actions={
            <button
              type="button"
              onClick={() => void load()}
              className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600"
            >
              {t('refresh')}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}

        {meta && !meta.enabled ? (
          <EmptyState message={t('disabled')} />
        ) : (
          <>
            <p className="mb-4 text-xs text-slate-400">
              {meta?.['unavailable-reason'] === 'cache unreachable'
                ? t('cacheUnreachable')
                : meta?.['sampled-at']
                  ? t('sampledAt', { time: when(meta['sampled-at']) })
                  : t('notSampled')}
            </p>
            <ul className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              {instances.map((i) => {
                const reach =
                  i.reachable === null ? (
                    <Pill tone="neutral" icon={HelpCircle} text={`${t('reachable')}: ${t('unknown')}`} />
                  ) : i.reachable ? (
                    <Pill tone="good" icon={CheckCircle2} text={t('reachable')} />
                  ) : (
                    <Pill tone="bad" icon={XCircle} text={t('unreachable')} />
                  )
                const seal =
                  i.sealed === null ? null : i.sealed ? (
                    <Pill tone="bad" icon={Lock} text={t('sealed')} />
                  ) : (
                    <Pill tone="good" icon={LockOpen} text={t('unsealed')} />
                  )
                const login =
                  i['login-ok'] === null ? (
                    <Pill tone="neutral" icon={HelpCircle} text={`${t('loginOk')}: ${t('unknown')}`} />
                  ) : i['login-ok'] ? (
                    <Pill tone="good" icon={CheckCircle2} text={t('loginOk')} />
                  ) : (
                    <Pill tone="bad" icon={XCircle} text={t('loginFailed')} />
                  )
                return (
                  <li
                    key={i.name}
                    data-testid={`vault-instance-${i.name}`}
                    className="rounded-xl border border-slate-800 bg-slate-900/50 p-4"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="text-lg font-medium break-all">{i.name}</h2>
                      {i.default && <Pill tone="neutral" icon={CheckCircle2} text={t('default')} />}
                    </div>
                    <div className="mt-3 flex flex-wrap gap-2">
                      {reach}
                      {seal}
                      {i.initialized === false && (
                        <Pill tone="bad" icon={XCircle} text={t('notInitialized')} />
                      )}
                      {i.standby && <Pill tone="warn" icon={PauseCircle} text={t('standby')} />}
                      {login}
                    </div>
                    {i['health-error'] && (
                      <p className="mt-2 text-xs text-red-300 break-words">{i['health-error']}</p>
                    )}
                    {i['login-error'] && (
                      <p className="mt-2 text-xs text-amber-300 break-words">{i['login-error']}</p>
                    )}
                    <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
                      <div className="min-w-0">
                        <dt className="text-xs text-slate-400">{t('address')}</dt>
                        <dd className="font-mono text-xs break-all">{i.address}</dd>
                      </div>
                      {i.namespace && (
                        <div className="min-w-0">
                          <dt className="text-xs text-slate-400">{t('namespace')}</dt>
                          <dd className="font-mono text-xs break-all">{i.namespace}</dd>
                        </div>
                      )}
                      <div className="min-w-0">
                        <dt className="text-xs text-slate-400">{t('authMethod')}</dt>
                        <dd className="font-mono text-xs break-all">
                          {i['auth-method']} · {i['auth-mount']} · {i['auth-role']}
                        </dd>
                      </div>
                      <div className="min-w-0">
                        <dt className="text-xs text-slate-400">{t('tlsTrust')}</dt>
                        <dd className="text-xs">{tlsLabel(i['tls-trust'])}</dd>
                      </div>
                      <div className="min-w-0">
                        <dt className="text-xs text-slate-400">{t('version')}</dt>
                        <dd className="text-xs">{i.version || '—'}</dd>
                      </div>
                      <div className="min-w-0">
                        <dt className="text-xs text-slate-400">{t('tokenTtl')}</dt>
                        <dd className="text-xs tabular-nums">{duration(i['ttl-seconds'])}</dd>
                      </div>
                      <div className="min-w-0">
                        <dt className="text-xs text-slate-400">{t('checkedAt')}</dt>
                        <dd className="text-xs">{when(i['checked-at'])}</dd>
                      </div>
                    </dl>
                    <div className="mt-3 border-t border-slate-800 pt-3">
                      <div className="text-xs text-slate-400">{t('lastError')}</div>
                      {i['last-error'] ? (
                        <div className="mt-1">
                          <div className="text-xs text-slate-300">
                            {t('lastErrorLine', {
                              kind: i['last-error'].class,
                              time: when(i['last-error'].at),
                            })}
                          </div>
                          <p className="mt-1 text-xs text-red-300 break-words">
                            {i['last-error'].message}
                          </p>
                        </div>
                      ) : (
                        <div className="mt-1 text-xs text-slate-300">{t('noLastError')}</div>
                      )}
                    </div>
                  </li>
                )
              })}
            </ul>
          </>
        )}
      </main>
    </div>
  )
}
