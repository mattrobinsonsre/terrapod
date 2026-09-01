'use client'

/**
 * Read-only view of the variable sets applying to a workspace (#1440).
 *
 * The association is managed from the variable-set side, and with rule-based
 * assignment it may not be managed by hand at all — so from the workspace there
 * was previously no way to answer "where did this variable come from". This
 * panel answers it, and says how each set came to apply, because "someone bound
 * it" and "it matches a rule" call for very different actions.
 *
 * Deliberately not editable: an explicit binding is edited on the set itself,
 * and a global or rule-derived one has no per-workspace binding to remove. An
 * unbind control that silently did nothing would be worse than none.
 */

import { useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import Link from 'next/link'
import { fetchAllPages } from '@/lib/api'

export interface WorkspaceVarset {
  id: string
  attributes: {
    name: string
    priority: boolean
    'variable-count': number
    'assignment-source': 'explicit' | 'global' | 'rule'
  }
}

const SOURCE_STYLES: Record<string, string> = {
  explicit: 'bg-slate-700 text-slate-200',
  global: 'bg-blue-900/40 text-blue-300',
  rule: 'bg-violet-900/40 text-violet-300',
}

export function ApplicableVarsets({ workspaceId }: { workspaceId: string }) {
  const t = useTranslations('workspaceDetail.applicableVarsets')
  const [varsets, setVarsets] = useState<WorkspaceVarset[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const rows = await fetchAllPages<WorkspaceVarset>(
          `/api/terrapod/v1/workspaces/${workspaceId}/varsets`,
        )
        if (!cancelled) setVarsets(rows)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : t('loadError'))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [workspaceId, t])

  if (loading || error || varsets.length === 0) {
    // Nothing worth a panel: no sets apply, or the list could not be loaded.
    // A failure here must not obscure the variables themselves, which are the
    // point of the tab — so it stays quiet rather than raising a banner.
    return null
  }

  return (
    <div className="mb-6">
      <h3 className="text-sm font-medium text-slate-300 mb-1">{t('title')}</h3>
      <p className="text-xs text-slate-500 mb-3">{t('description')}</p>

      <ul className="space-y-2">
        {varsets.map((vs) => {
          const source = vs.attributes['assignment-source']
          return (
            <li
              key={vs.id}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-slate-700/50 bg-slate-800/50 px-3 py-2"
            >
              <Link
                href={`/admin/variable-sets/${vs.id}`}
                className="text-sm font-medium text-brand-400 hover:text-brand-300"
              >
                {vs.attributes.name}
              </Link>
              <span
                className={`px-2 py-0.5 rounded text-xs font-medium ${SOURCE_STYLES[source] ?? SOURCE_STYLES.explicit}`}
              >
                {t(`source.${source}`)}
              </span>
              {vs.attributes.priority && (
                <span className="px-2 py-0.5 rounded text-xs font-medium bg-amber-900/40 text-amber-300">
                  {t('priority')}
                </span>
              )}
              <span className="text-xs text-slate-500 tabular-nums">
                {t('variableCount', { count: vs.attributes['variable-count'] })}
              </span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
