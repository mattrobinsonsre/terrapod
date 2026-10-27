'use client'

/**
 * The two phase-slot checkboxes a variable carries, and the note that explains
 * why ticking one greys out the other (#2190).
 *
 * A key is held in exactly one of three slots:
 *
 *   * ordinary      — every phase;
 *   * apply-only    — an apply and a configure (#2189);
 *   * configure-only — a configure alone.
 *
 * Configure-only is a THIRD slot rather than a rename of the second, because a
 * configure already receives every apply-only row: `apply-only` therefore
 * cannot express "not in an apply either", and that is exactly what the
 * fleet's own credential wants.
 *
 * ONE producer of the exclusion, shared by all four write surfaces — the
 * workspace add form (twice, the Ansible branch and the general one), the
 * variable-set add form, and the shared edit panel. The API refuses the pair
 * with a 422, so a surface that forgot to disable the other control would
 * offer a state the save rejects; written out per surface, that is a rule to
 * get wrong four times rather than once.
 *
 * The flags are a fragment rather than their own row, so each caller keeps the
 * flex row it already has and these sit beside Sensitive and Structured.
 */

import { useTranslations } from 'next-intl'

export interface PhaseSlotState {
  applyOnly: boolean
  configureOnly: boolean
}

export function PhaseSlotFlags({
  namespace,
  state,
  onChange,
  /** `text-sm text-slate-300` in the add forms, `text-xs text-slate-400` in the edit panel. */
  labelClassName,
}: {
  namespace: string
  state: PhaseSlotState
  onChange: (patch: Partial<PhaseSlotState>) => void
  labelClassName: string
}) {
  const t = useTranslations(namespace)
  return (
    <>
      <label
        className={`flex items-center gap-2 ${state.configureOnly ? 'opacity-60' : 'cursor-pointer'}`}
        title={t('applyOnlyHint')}
      >
        <input
          type="checkbox"
          checked={state.applyOnly}
          disabled={state.configureOnly}
          onChange={(e) => onChange({ applyOnly: e.target.checked })}
          className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
        />
        <span className={labelClassName}>{t('applyOnly')}</span>
      </label>
      <label
        className={`flex items-center gap-2 ${state.applyOnly ? 'opacity-60' : 'cursor-pointer'}`}
        title={t('configureOnlyHint')}
      >
        <input
          type="checkbox"
          checked={state.configureOnly}
          disabled={state.applyOnly}
          onChange={(e) => onChange({ configureOnly: e.target.checked })}
          className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
        />
        <span className={labelClassName}>{t('configureOnly')}</span>
      </label>
    </>
  )
}

/**
 * The reason the other control is disabled, as TEXT.
 *
 * Not left in the `title` the labels already carry: a tooltip is hover-only,
 * so on touch — where a mis-tap is likeliest — a greyed-out checkbox would
 * have no explanation at all. Shown only while one of the two is set, which is
 * exactly when a control is disabled and the question arises.
 */
export function PhaseSlotNote({
  namespace,
  state,
  className = 'text-xs text-slate-500',
}: {
  namespace: string
  state: PhaseSlotState
  className?: string
}) {
  const t = useTranslations(namespace)
  if (!state.applyOnly && !state.configureOnly) return null
  return <p className={className}>{t('phaseSlotExclusive')}</p>
}
