'use client'

/**
 * A vault-sourced variable's stored value, rendered as coordinates (#1439).
 *
 * The value is a *reference* — mount, path, field — not the secret. Showing
 * `***` here would hide configuration the operator needs while concealing
 * nothing: the secret it points at is resolved at run time and never stored,
 * returned or logged.
 */

import { useTranslations } from 'next-intl'

export function VaultValueDisplay({ value }: { value: string }) {
  const t = useTranslations('workspaceDetail.variables')
  let ref: Record<string, string> = {}
  try {
    ref = JSON.parse(value || '{}')
  } catch {
    // A reference that no longer parses is a real problem, but the variables
    // list is not where it gets diagnosed — say so plainly and move on.
    return <span className="text-xs text-amber-400">{t('vaultReferenceUnreadable')}</span>
  }

  const coords = [ref.mount, ref.path].filter(Boolean).join('/')
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <span className="px-2 py-0.5 rounded text-xs font-medium bg-violet-900/40 text-violet-300">
        {t('sourceVaultBadge')}
      </span>
      <span className="font-mono text-xs text-slate-300">
        {coords}
        {ref.field ? <span className="text-slate-500"> · {ref.field}</span> : null}
      </span>
      {ref.vault ? <span className="text-xs text-slate-500">({ref.vault})</span> : null}
    </span>
  )
}
