'use client'

/**
 * A checkbox that matches the rest of the UI.
 *
 * A native checkbox ignores `bg-*` and `border-*` until its default appearance is
 * removed, so the plain one renders as a white box on the dark surface. This keeps
 * the real `<input type="checkbox">`, so the keyboard, focus, form and screen
 * reader behaviour are the browser's own, and draws over it: a dark rounded box
 * that fills with the brand colour when checked, with a check mark, or a dash
 * for the indeterminate state a header uses for a partial selection.
 *
 * Colours are theme tokens (`slate-*`, `brand-*`), so the light theme follows.
 */

import { useEffect, useRef } from 'react'

export function Checkbox({
  checked,
  onChange,
  indeterminate = false,
  ariaLabel,
  testId,
}: {
  checked: boolean
  onChange: () => void
  /** A partial selection. Shown as a dash; `checked` is ignored for the look. */
  indeterminate?: boolean
  ariaLabel: string
  testId?: string
}) {
  const ref = useRef<HTMLInputElement | null>(null)
  // `indeterminate` is a DOM property with no attribute, so it is set here.
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate
  }, [indeterminate])

  return (
    <span className="relative inline-flex h-[18px] w-[18px] shrink-0 align-middle">
      <input
        ref={ref}
        type="checkbox"
        checked={checked}
        onChange={onChange}
        aria-label={ariaLabel}
        data-testid={testId}
        className={
          'peer h-[18px] w-[18px] cursor-pointer appearance-none rounded-[5px] border ' +
          'border-slate-600 bg-slate-800/60 transition-colors ' +
          'hover:border-slate-500 hover:bg-slate-700/60 ' +
          'light:border-slate-500/35 light:hover:border-slate-500/55 ' +
          'checked:border-brand-500 checked:bg-brand-600 ' +
          'indeterminate:border-brand-500 indeterminate:bg-brand-600 ' +
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400/60 ' +
          'focus-visible:ring-offset-1 focus-visible:ring-offset-slate-900'
        }
      />
      <svg
        aria-hidden="true"
        viewBox="0 0 16 16"
        fill="none"
        className="pointer-events-none absolute inset-0 m-auto h-3 w-3 text-white opacity-0 transition-opacity peer-checked:opacity-100 peer-indeterminate:opacity-0"
      >
        <path d="M3.5 8.5l3 3 6-7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <svg
        aria-hidden="true"
        viewBox="0 0 16 16"
        fill="none"
        className="pointer-events-none absolute inset-0 m-auto h-3 w-3 text-white opacity-0 transition-opacity peer-indeterminate:opacity-100"
      >
        <path d="M4 8h8" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      </svg>
    </span>
  )
}
