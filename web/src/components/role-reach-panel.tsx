'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslations } from 'next-intl'

import { apiFetch } from '@/lib/api'

/** One workspace in a reach result. Mirrors the server's JSON:API attributes. */
interface ReachWorkspace {
  id: string
  name: string
  labels?: Record<string, string>
  'owner-email'?: string
  verdict: string
  reason?: string
  capabilities?: string[]
  notes?: string[]
}

interface Reach {
  'granted-count': number
  'denied-count': number
  'matched-count': number
  workspaces: ReachWorkspace[]
  denied?: ReachWorkspace[]
  'denied-truncated'?: boolean
}

export interface RoleRule {
  allowLabels: Record<string, string>
  allowNames: string[]
  denyLabels: Record<string, string>
  denyNames: string[]
  capabilities: string[]
}

/** A rule with nothing on the allow side reaches nothing, so there is no
 *  point asking the server — and an empty result reads better than a spinner
 *  that resolves to zero every keystroke while someone is still typing. */
function reachesNothing(rule: RoleRule): boolean {
  return Object.keys(rule.allowLabels).length === 0 && rule.allowNames.length === 0
}

/** Renders a `reason` string from the server (`allow-label:env=prod`,
 *  `deny-name`, …) as something a person reads. The server keeps it
 *  machine-readable so the wire contract does not carry prose. */
function ReasonChip({ reason }: { reason?: string }) {
  const t = useTranslations('roleReach')
  if (!reason) return null
  const [kind, detail] = reason.split(':', 2)
  const label =
    kind === 'allow-name'
      ? t('reason.allowName')
      : kind === 'deny-name'
        ? t('reason.denyName')
        : kind === 'allow-label'
          ? t('reason.allowLabel', { rule: detail ?? '' })
          : kind === 'deny-label'
            ? t('reason.denyLabel', { rule: detail ?? '' })
            : reason
  const denied = kind.startsWith('deny')
  return (
    <span
      className={`inline-block px-1.5 py-0.5 rounded text-[11px] font-mono ${
        denied ? 'bg-red-900/40 text-red-300' : 'bg-slate-700/60 text-slate-300'
      }`}
    >
      {label}
    </span>
  )
}

function NoteChips({ notes }: { notes?: string[] }) {
  const t = useTranslations('roleReach')
  if (!notes?.length) return null
  const copy: Record<string, string> = {
    'catalog-clamped': t('note.catalogClamped'),
    'everyone-floor': t('note.everyoneFloor'),
    'has-owner': t('note.hasOwner'),
  }
  return (
    <>
      {notes.map((n) => (
        <span
          key={n}
          title={copy[n] ?? n}
          className="inline-block px-1.5 py-0.5 rounded text-[11px] bg-amber-900/30 text-amber-300"
        >
          {copy[n] ?? n}
        </span>
      ))}
    </>
  )
}

function WorkspaceRow({ ws }: { ws: ReachWorkspace }) {
  return (
    <li className="flex flex-wrap items-center gap-2 py-1.5 border-b border-slate-700/40 last:border-0">
      <span className="text-sm text-slate-200 font-medium break-all">{ws.name}</span>
      <ReasonChip reason={ws.reason} />
      <NoteChips notes={ws.notes} />
    </li>
  )
}

/**
 * Live "which workspaces does this rule reach" panel for the role editor.
 *
 * The allow/deny interaction is where rules go wrong, and until now nothing
 * showed the outcome before saving — so people either over-granted or avoided
 * deny rules entirely. Seeing "matched 47, 3 denied" while typing is what makes
 * a deny rule safe to write.
 *
 * It previews the UNSAVED rule, so it works while authoring a role that does
 * not exist yet, and it is read-only: nothing here mutates.
 */
export function RoleReachPanel({ rule }: { rule: RoleRule }) {
  const t = useTranslations('roleReach')
  const [reach, setReach] = useState<Reach | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  // Serialised rather than compared by identity: the parent rebuilds these
  // objects on every keystroke, so an identity check would refetch constantly.
  const key = JSON.stringify(rule)
  const latest = useRef(0)

  const load = useCallback(async (signature: string) => {
    const parsed: RoleRule = JSON.parse(signature)
    if (reachesNothing(parsed)) {
      setReach(null)
      setError('')
      return
    }
    const seq = ++latest.current
    setLoading(true)
    setError('')
    try {
      const res = await apiFetch('/api/terrapod/v1/roles/preview?page[size]=10', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'roles',
            attributes: {
              name: '(preview)',
              'allow-labels': parsed.allowLabels,
              'allow-names': parsed.allowNames,
              'deny-labels': parsed.denyLabels,
              'deny-names': parsed.denyNames,
              capabilities: parsed.capabilities,
            },
          },
        }),
      })
      // An out-of-order response must never overwrite a newer one, or the
      // panel shows the reach of a rule the operator has already edited past.
      if (seq !== latest.current) return
      if (!res.ok) {
        setError(t('failed'))
        setReach(null)
        return
      }
      setReach((await res.json()).data?.attributes ?? null)
    } catch {
      if (seq === latest.current) {
        setError(t('failed'))
        setReach(null)
      }
    } finally {
      if (seq === latest.current) setLoading(false)
    }
  }, [t])

  useEffect(() => {
    // Debounced: this fires on every keystroke in the label boxes, and a
    // request per character would hammer an endpoint that counts the fleet.
    const handle = setTimeout(() => void load(key), 400)
    return () => clearTimeout(handle)
  }, [key, load])

  const empty = reachesNothing(rule)

  return (
    <div
      data-testid="role-reach"
      className="rounded-lg border border-slate-700/50 bg-slate-900/40 p-3"
    >
      <div className="flex items-center justify-between gap-2 mb-2">
        <h4 className="text-sm font-medium text-slate-300">{t('title')}</h4>
        {loading && <span className="text-xs text-slate-500">{t('checking')}</span>}
      </div>

      {empty ? (
        <p className="text-xs text-slate-500">{t('noAllowRules')}</p>
      ) : error ? (
        <p className="text-xs text-red-400">{error}</p>
      ) : reach ? (
        <>
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 mb-3">
            <span className="text-sm text-slate-200">
              <span className="text-lg font-semibold tabular-nums">{reach['granted-count']}</span>{' '}
              {t('granted')}
            </span>
            {reach['denied-count'] > 0 && (
              <span className="text-sm text-red-300">
                <span className="text-lg font-semibold tabular-nums">{reach['denied-count']}</span>{' '}
                {t('deniedCount')}
              </span>
            )}
            <span className="text-xs text-slate-500">
              {t('matched', { count: reach['matched-count'] })}
            </span>
          </div>

          {reach.workspaces.length > 0 && (
            <ul className="mb-2">
              {reach.workspaces.map((w) => (
                <WorkspaceRow key={w.id} ws={w} />
              ))}
            </ul>
          )}
          {reach['granted-count'] > reach.workspaces.length && (
            <p className="text-xs text-slate-500">
              {t('andMore', { count: reach['granted-count'] - reach.workspaces.length })}
            </p>
          )}

          {reach.denied && reach.denied.length > 0 && (
            <div className="mt-3 pt-2 border-t border-slate-700/50">
              {/* Shown rather than silently omitted: someone who cannot see what
                  a deny removed cannot tell an intended exclusion from a typo. */}
              <h5 className="text-xs font-medium text-red-300 mb-1">{t('excludedByDeny')}</h5>
              <ul>
                {reach.denied.map((w) => (
                  <WorkspaceRow key={w.id} ws={w} />
                ))}
              </ul>
            </div>
          )}
        </>
      ) : (
        <p className="text-xs text-slate-500">{t('checking')}</p>
      )}
    </div>
  )
}
