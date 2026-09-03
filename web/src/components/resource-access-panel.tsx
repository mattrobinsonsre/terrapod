'use client'

import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'

import { apiFetch } from '@/lib/api'

interface AccessRole {
  role: string
  verdict: string
  reason?: string
  capabilities?: string[]
  notes?: string[]
  'held-by'?: string[]
}

interface Access {
  roles: AccessRole[]
  'denied-roles'?: AccessRole[]
  'role-count': number
  'platform-paths'?: string[]
}

function RoleRow({ entry, denied }: { entry: AccessRole; denied?: boolean }) {
  const t = useTranslations('resourceAccess')
  const held = entry['held-by'] ?? []
  return (
    <li className="py-2 border-b border-slate-700/40 last:border-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`text-sm font-medium ${denied ? 'text-red-300' : 'text-slate-200'}`}>
          {entry.role}
        </span>
        {entry.reason && (
          <span
            className={`px-1.5 py-0.5 rounded text-[11px] font-mono ${
              denied ? 'bg-red-900/40 text-red-300' : 'bg-slate-700/60 text-slate-300'
            }`}
          >
            {entry.reason}
          </span>
        )}
      </div>
      {/* Who holds it is the actionable half — a role nobody holds reaches
          nothing in practice, and a role held by twenty people is the finding. */}
      <div className="mt-0.5 text-xs text-slate-500 break-all">
        {held.length === 0 ? t('heldByNobody') : t('heldBy', { who: held.join(', ') })}
      </div>
    </li>
  )
}

/**
 * "Who can reach this?" for one resource — the inverse of the role editor's
 * reach panel.
 *
 * The platform-paths block is not decoration. A list of roles reads as the
 * complete answer when it is not: a platform admin reaches everything, an owner
 * holds admin on their own resource, and an `access: everyone` label makes a
 * thing readable with no role involved at all.
 */
export function ResourceAccessPanel({ kind, id }: { kind: string; id: string }) {
  const t = useTranslations('resourceAccess')
  const [access, setAccess] = useState<Access | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/${kind}/${id}/access`)
      if (!res.ok) {
        // 403 is the ordinary case for a non-admin, not a fault: the answer
        // spans the estate, so it is offered only to those who can see it.
        setError(res.status === 403 ? t('forbidden') : t('failed'))
        return
      }
      setAccess((await res.json()).data?.attributes ?? null)
    } catch {
      setError(t('failed'))
    } finally {
      setLoading(false)
    }
  }, [kind, id, t])

  useEffect(() => {
    void load()
  }, [load])

  const pathCopy: Record<string, string> = {
    'platform-admin': t('path.platformAdmin'),
    'platform-audit': t('path.platformAudit'),
    owner: t('path.owner'),
    'everyone-floor': t('path.everyoneFloor'),
    'catalog-clamped': t('path.catalogClamped'),
  }

  return (
    <div data-testid="resource-access" className="space-y-3">
      <div>
        <h3 className="text-sm font-medium text-slate-300">{t('title')}</h3>
        <p className="text-xs text-slate-500 mt-0.5">{t('subtitle')}</p>
      </div>

      {loading ? (
        <p className="text-xs text-slate-500">{t('loading')}</p>
      ) : error ? (
        <p className="text-xs text-red-400">{error}</p>
      ) : access ? (
        <>
          {access.roles.length === 0 ? (
            <p className="text-xs text-slate-500">{t('noRoles')}</p>
          ) : (
            <ul>
              {access.roles.map((r) => (
                <RoleRow key={r.role} entry={r} />
              ))}
            </ul>
          )}

          {access['denied-roles'] && access['denied-roles'].length > 0 && (
            <div className="pt-2 border-t border-slate-700/50">
              <h4 className="text-xs font-medium text-red-300 mb-1">{t('deniedRoles')}</h4>
              <ul>
                {access['denied-roles'].map((r) => (
                  <RoleRow key={r.role} entry={r} denied />
                ))}
              </ul>
            </div>
          )}

          {access['platform-paths'] && access['platform-paths'].length > 0 && (
            <div className="pt-2 border-t border-slate-700/50">
              <h4 className="text-xs font-medium text-amber-300 mb-1">{t('alsoReachable')}</h4>
              <ul className="space-y-0.5">
                {access['platform-paths'].map((p) => (
                  <li key={p} className="text-xs text-slate-400">
                    {pathCopy[p] ?? p}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      ) : null}
    </div>
  )
}
