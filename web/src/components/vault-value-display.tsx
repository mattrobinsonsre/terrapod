'use client'

/**
 * A vault-sourced variable's stored value, rendered as coordinates (#1439).
 *
 * The value is a *reference* — mount, path, field — not the secret. Showing
 * `***` here would hide configuration the operator needs while concealing
 * nothing: the secret it points at is resolved at run time and never stored,
 * returned or logged.
 *
 * A reference delivered as a file (#1619) also shows the file name, since
 * that is what the variable actually carries at run time — a path, not the
 * secret. An unnamed file defaults to the variable key, so `varKey` fills in.
 */

import { useTranslations } from 'next-intl'

export function VaultValueDisplay({ value, varKey }: { value: string; varKey?: string }) {
  const t = useTranslations('workspaceDetail.variables')
  let ref: Record<string, unknown> = {}
  try {
    ref = JSON.parse(value || '{}')
  } catch {
    // A reference that no longer parses is a real problem, but the variables
    // list is not where it gets diagnosed — say so plainly and move on.
    return <span className="text-xs text-amber-400">{t('vaultReferenceUnreadable')}</span>
  }

  const str = (v: unknown) => (typeof v === 'string' ? v : '')
  const coords = [str(ref.mount), str(ref.path)].filter(Boolean).join('/')
  const file = ref.file && typeof ref.file === 'object' ? (ref.file as Record<string, unknown>) : null
  const fileName = file ? str(file.name) || varKey || '' : ''
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <span className="px-2 py-0.5 rounded text-xs font-medium bg-violet-900/40 text-violet-300">
        {t('sourceVaultBadge')}
      </span>
      <span className="font-mono text-xs text-slate-300 break-all">
        {coords}
        {ref.field ? <span className="text-slate-500"> · {str(ref.field)}</span> : null}
      </span>
      {ref.vault ? <span className="text-xs text-slate-500">({str(ref.vault)})</span> : null}
      {file ? (
        <span className="inline-flex items-center gap-1">
          <span className="px-2 py-0.5 rounded text-xs font-medium bg-slate-700 text-slate-300">
            {t('vaultFileBadge')}
          </span>
          <span className="font-mono text-xs text-slate-300 break-all">{fileName}</span>
        </span>
      ) : null}
    </span>
  )
}
