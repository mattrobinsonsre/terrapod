'use client'

import Link from 'next/link'
import { useTranslations } from 'next-intl'
import type { WorkspaceStatusDef } from '@/lib/workspace-status'

// Colour → Tailwind pill classes for the workspace status/lifecycle badges.
// Single source of truth so the desktop STATUS column and the mobile status
// line (below the `lg` breakpoint, where the column is hidden) render
// identically and can never drift (#719).
const badgeColors: Record<string, string> = {
  amber: 'bg-amber-900/50 text-amber-300',
  red: 'bg-red-900/50 text-red-300',
  blue: 'bg-blue-900/50 text-blue-300',
  green: 'bg-green-900/50 text-green-300',
  slate: 'bg-slate-700/50 text-slate-400',
  gray: 'text-slate-500',
}

const pill =
  'inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium whitespace-nowrap'

/**
 * The workspace's status pill (linked to its latest run when there is one)
 * plus any lifecycle badge (pending-deletion / archived). Rendered in the
 * desktop STATUS column AND inline in the row on mobile — one implementation,
 * so the two viewports always agree.
 */
export function WorkspaceStatusBadges({
  workspaceId,
  def,
  runId,
  lifecycleState,
}: {
  workspaceId: string
  def: WorkspaceStatusDef | null
  runId: string | null
  lifecycleState?: 'active' | 'pending_deletion' | 'archived'
}) {
  // Status labels live in the `status` namespace keyed by the stable `filter`
  // token (state-diverged, errored, …), so the shared workspace-status.ts data
  // module stays i18n-free while the displayed text localizes (#767).
  const t = useTranslations('status')
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {!def ? (
        <span className="text-xs text-slate-500">&mdash;</span>
      ) : runId ? (
        <Link
          href={`/workspaces/${workspaceId}/runs/${runId}`}
          className={`${pill} hover:opacity-80 transition-opacity ${badgeColors[def.color]}`}
        >
          {t(def.filter)}
        </Link>
      ) : (
        <span className={`${pill} ${badgeColors[def.color]}`}>{t(def.filter)}</span>
      )}
      {lifecycleState === 'pending_deletion' && (
        <span className={`${pill} ${badgeColors.amber}`}>{t('pendingDeletion')}</span>
      )}
      {lifecycleState === 'archived' && (
        <span className={`${pill} ${badgeColors.slate}`}>{t('archived')}</span>
      )}
    </div>
  )
}
