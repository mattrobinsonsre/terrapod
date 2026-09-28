'use client'

// Stack outputs (#1568) — the output values carried by a workspace's latest
// state, shown on the State tab for BOTH engines. Terraform calls them outputs
// and Pulumi calls them stack outputs; they are the same idea and the API
// returns them in the same shape, so one surface serves both.
//
// A sensitive value NEVER reaches the browser: the server substitutes the
// literal string `(sensitive)` for it, so there is nothing here to redact and
// nothing to leak into a screenshot. That literal is what `SENSITIVE` matches,
// and it is a wire value rather than copy — it is not translated, and a real
// output whose value happens to be that string simply renders masked, which is
// the safe way round.
import { useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { apiFetch } from '@/lib/api'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'

/** The placeholder the server sends in place of a sensitive output value. */
const SENSITIVE = '(sensitive)'

interface StateOutputs {
  outputs: Record<string, unknown>
  state_version?: string | null
  serial?: number | null
}

/** One output's value, rendered by shape.
 *
 *  Strings/numbers/booleans render inline and wrap; objects and arrays render
 *  as pretty JSON in their own horizontally-scrollable block, so a wide value
 *  scrolls inside its box rather than pushing the page sideways on a phone. */
function OutputValue({ value }: { value: unknown }) {
  const t = useTranslations('workspaceDetail.stateOutputs')

  if (value === SENSITIVE) {
    return (
      <span className="inline-flex flex-wrap items-center gap-2">
        <span aria-hidden="true" className="font-mono text-xs text-slate-500">
          ••••••••
        </span>
        <span className="inline-flex items-center rounded-full bg-amber-900/40 px-2 py-0.5 text-xs font-medium text-amber-300">
          {t('sensitive')}
        </span>
      </span>
    )
  }

  if (typeof value === 'string') {
    return <span className="block break-words font-mono text-xs text-slate-200">{value}</span>
  }

  if (value === null || typeof value === 'number' || typeof value === 'boolean') {
    return <span className="block font-mono text-xs text-slate-200">{JSON.stringify(value)}</span>
  }

  return (
    <pre className="overflow-x-auto rounded-md border border-slate-700/50 bg-slate-900/60 p-2 text-xs leading-relaxed text-slate-200">
      <code>{JSON.stringify(value, null, 2)}</code>
    </pre>
  )
}

interface StackOutputsPanelProps {
  workspaceId: string
  /** Bump to refetch (the State tab passes the state-version count, so a new
   *  state version re-reads the outputs without a manual reload). */
  refreshKey?: number
}

/** `absent` = a 404: this deployment has no outputs surface, or the workspace
 *  went away under us. Render nothing at all rather than an error banner under
 *  a state-version list that is perfectly fine. */
type Status = 'ok' | 'failed' | 'absent'

export function StackOutputsPanel({ workspaceId, refreshKey = 0 }: StackOutputsPanelProps) {
  const t = useTranslations('workspaceDetail.stateOutputs')
  // The result carries the request it answers, so "still loading" is DERIVED
  // (`result.key !== requestKey`) rather than a second state set synchronously
  // at the top of the effect, which would cascade a render.
  const requestKey = `${workspaceId}:${refreshKey}`
  const [result, setResult] = useState<{ key: string; status: Status; data: StateOutputs | null } | null>(null)

  useEffect(() => {
    let cancelled = false
    apiFetch(`/api/v1/workspaces/${workspaceId}/state-outputs`)
      .then(async (res): Promise<{ status: Status; data: StateOutputs | null }> => {
        if (res.status === 404) return { status: 'absent', data: null }
        if (!res.ok) throw new Error(String(res.status))
        const attrs = (await res.json())?.data?.attributes
        return {
          status: 'ok',
          data: {
            outputs: (attrs?.outputs ?? {}) as Record<string, unknown>,
            state_version: attrs?.state_version ?? null,
            serial: typeof attrs?.serial === 'number' ? attrs.serial : null,
          },
        }
      })
      .catch((): { status: Status; data: StateOutputs | null } => ({ status: 'failed', data: null }))
      .then((r) => {
        if (!cancelled) setResult({ key: requestKey, ...r })
      })
    return () => {
      cancelled = true
    }
    // `t` is deliberately absent: next-intl's translator is not referentially
    // stable, so depending on it would refetch on every render. `requestKey`
    // covers `refreshKey`.
  }, [workspaceId, requestKey])

  const current = result?.key === requestKey ? result : null
  if (current?.status === 'absent') return null

  const data = current?.data ?? null
  const entries = Object.entries(data?.outputs ?? {})

  return (
    <section className="mt-8">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="text-sm font-semibold text-slate-200">{t('title')}</h2>
        {typeof data?.serial === 'number' && (
          <span className="text-xs text-slate-500">{t('fromSerial', { serial: data.serial })}</span>
        )}
      </div>

      {current === null ? (
        <LoadingSpinner />
      ) : current.status === 'failed' ? (
        <ErrorBanner message={t('loadFailed')} />
      ) : entries.length === 0 ? (
        <EmptyState message={t('empty')} />
      ) : (
        <dl className="space-y-2">
          {entries.map(([name, value]) => (
            <div
              key={name}
              className="rounded-lg border border-slate-700/50 bg-slate-800/50 p-3 md:grid md:grid-cols-[minmax(0,14rem)_minmax(0,1fr)] md:items-start md:gap-4"
            >
              <dt className="min-w-0 break-words font-mono text-xs font-medium text-slate-300">
                {name}
              </dt>
              <dd className="mt-2 min-w-0 md:mt-0">
                <OutputValue value={value} />
              </dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  )
}
