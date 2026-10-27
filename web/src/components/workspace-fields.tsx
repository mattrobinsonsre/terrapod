'use client'

/**
 * `WsField` -- one field's label and either its stored value or its control.
 *
 * The table, the logic and the payload builder moved to
 * `@/lib/workspace-fields` so `web/tests/` can import them (the runner is bare
 * `node --test`, which cannot load a file containing JSX). Everything there is
 * re-exported from here, so both import paths work and no consumer changed.
 */

import type { ReactNode } from 'react'
import type { FieldMode } from '@/lib/workspace-fields'

export * from '@/lib/workspace-fields'

/**
 * One field: its label, and either the stored value or the control.
 *
 * The two forms are structurally different -- the edit tab is a `<dl>` whose
 * `<dd>` becomes an `<input>` while editing, and the create form is a
 * `<label>` plus a control that is always editable. That is why this takes a
 * `mode` rather than the markup being lifted as-is: `view` renders the `<dt>`
 * and `<dd>` pair, and `edit`/`create` render a label and the caller's
 * control. Collapsing the two into one shape would have meant giving up the
 * edit tab's read view, which is the thing most visits to that page want.
 *
 * `min-w-0` is on the wrapper deliberately. A grid item has an intrinsic
 * minimum width and refuses to shrink below its content without it, which is
 * the single CSS line behind a phone-width overflow this repo has met before.
 */
export function WsField({
  id,
  label,
  mode,
  display,
  hint,
  children,
}: {
  id: string
  label: ReactNode
  mode: FieldMode
  /** What `view` mode shows. Ignored in the other two. */
  display?: ReactNode
  hint?: ReactNode
  /** The control, rendered in `edit` and `create`. */
  children?: ReactNode
}) {
  if (mode === 'view') {
    return (
      <div className="min-w-0">
        <dt className="text-xs text-slate-500">{label}</dt>
        <dd className="mt-1 text-sm text-slate-200 break-words" data-testid={`${id}-value`}>
          {display}
        </dd>
        {hint}
      </div>
    )
  }
  const labelClass =
    mode === 'create'
      ? 'block text-sm font-medium text-slate-300 mb-1'
      : 'text-xs text-slate-500'
  return (
    <div className="min-w-0">
      <label htmlFor={id} className={labelClass}>
        {label}
      </label>
      {children}
      {hint}
    </div>
  )
}
