'use client'

/**
 * The Workspaces list's `Selection` control: ONE button that opens a menu of the
 * actions the current selection can take.
 *
 * It is styled like the workspace page's run button (`RunKindSplitButton`), and
 * reuses its keyboard handling (`aria-haspopup`, `role="menu"`, arrow keys,
 * Escape returning focus to the trigger, a click outside closing), but it is a
 * different component on purpose, for one reason. That component is structurally
 * a primary action plus a caret, and it DERIVES the primary: "the most complete
 * option the workspace declares". That is right for one workspace and wrong for
 * an arbitrary selection, where there is no most-complete action and a loud
 * default applying across a dozen workspaces is the outcome to avoid. So this has
 * no default action: pressing it anywhere opens the menu, and that is all it
 * does. Every action lives inside.
 *
 * **The reason an action is unavailable is in the row, not in a tooltip.** Each
 * item reads `Apply · 9 of 12 eligible`, so it is readable with no interaction at
 * all, by a keyboard or screen-reader user as much as a mouse. A disabled item
 * is `aria-disabled` and stays focusable, so a screen reader reaches it and
 * hears why; a `disabled` button would be skipped and read as nothing.
 *
 * Desktop only: the caller hides it below `sm`, and the checkbox column with it.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

export interface SelectionMenuItem {
  id: string
  /** The action's name, e.g. `Apply`. */
  label: string
  /** The eligibility text shown beside it, e.g. `9 of 12 eligible`. */
  detail: string
  enabled: boolean
  run: () => void
}

export function SelectionMenu({
  items,
  label,
  menuLabel,
  className = '',
}: {
  items: SelectionMenuItem[]
  /** The button text, already carrying the count, e.g. `Selection · 12`. */
  label: string
  /** Accessible name for the menu. */
  menuLabel: string
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const wrap = useRef<HTMLDivElement | null>(null)
  const trigger = useRef<HTMLButtonElement | null>(null)
  const refs = useRef<(HTMLButtonElement | null)[]>([])

  const close = useCallback((returnFocus: boolean) => {
    setOpen(false)
    if (returnFocus) trigger.current?.focus()
  }, [])

  // A click anywhere else closes it. `mousedown` rather than `click`, so the
  // menu is gone before the click lands on whatever is underneath.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  // Focus the first item when it opens: a menu you can open and not reach is
  // worse than no menu.
  useEffect(() => {
    if (open) refs.current[0]?.focus()
  }, [open])

  const onItemKey = (e: React.KeyboardEvent, i: number) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      close(true)
    } else if (e.key === 'ArrowDown') {
      e.preventDefault()
      refs.current[(i + 1) % items.length]?.focus()
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      refs.current[(i - 1 + items.length) % items.length]?.focus()
    } else if (e.key === 'Tab') {
      // Tabbing out of a menu closes it, without stealing the focus move.
      setOpen(false)
    }
  }

  return (
    <div className="relative inline-flex" ref={wrap}>
      <button
        type="button"
        ref={trigger}
        data-testid="ws-selection"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown' && !open) {
            e.preventDefault()
            setOpen(true)
          } else if (e.key === 'Escape' && open) {
            e.preventDefault()
            close(false)
          }
        }}
        className={`inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-colors ${className}`}
      >
        <span>{label}</span>
        <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <div
          role="menu"
          aria-label={menuLabel}
          data-testid="ws-selection-menu"
          className="absolute end-0 top-full z-20 mt-1 min-w-max rounded-lg border border-slate-700 bg-slate-800 p-1 shadow-lg"
        >
          {items.map((it, i) => (
            <button
              key={it.id}
              type="button"
              role="menuitem"
              ref={(el) => {
                refs.current[i] = el
              }}
              data-testid={`ws-selection-${it.id}`}
              aria-disabled={!it.enabled}
              onClick={() => {
                // Stays focusable when off, so the reason beside it is reachable.
                if (!it.enabled) return
                setOpen(false)
                it.run()
              }}
              onKeyDown={(e) => onItemKey(e, i)}
              className={
                'flex w-full items-baseline justify-between gap-6 rounded px-3 py-2 text-start text-sm font-medium ' +
                (it.enabled
                  ? 'text-slate-200 hover:bg-slate-700'
                  : 'cursor-not-allowed text-slate-500')
              }
            >
              <span>{it.label}</span>
              <span className={'text-xs font-normal ' + (it.enabled ? 'text-slate-400' : 'text-slate-500')}>
                {it.detail}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
