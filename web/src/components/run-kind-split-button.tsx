'use client'

/**
 * The run button: one primary action with the partials behind a caret (#2413
 * F12 / F10).
 *
 * The Runs tab offered five controls in a row — Options, Destroy, Plan,
 * Plan + apply, Configure — and their visual weight did not track risk. The two
 * loudest were the safest action (`Plan`, solid brand) and a fleet-changing one
 * (`Configure`, solid violet), while the two most destructive were the quiet
 * ones (`Destroy` and `Plan + apply`, both muted). F10 is that finding; this is
 * its resolution, and it comes from F12 rather than from a restyle: with one
 * primary action there is one loud control, and it is the one the workspace
 * says is the complete thing to do.
 *
 * **The primary is DERIVED, never remembered.** It is the most complete option
 * the workspace declares and the caller may run — so it changes when the
 * workspace changes, and two operators with different roles see different
 * primaries on the same workspace, each correct for them. Deliberately no
 * memory of the last choice: a button whose meaning depends on what you pressed
 * last is the opposite of making a fleet-changing action clear.
 *
 * `Destroy` is NOT a kind here. It is its own grant (`run:apply-destroy`) and
 * keeps its own inline two-step, because burying the most consequential action
 * in a menu is the one place a caret would cost more than it saves.
 *
 * ## Why a menu and not a `<select>`
 *
 * A `<select>` would be fewer lines and is the wrong control: these are actions,
 * not a value, so choosing one must RUN it rather than set it. That is what
 * makes the keyboard handling this component's own problem — `aria-haspopup`,
 * `role="menu"`, arrow keys, Escape returning focus to the caret, and a click
 * outside closing — rather than the browser's.
 *
 * The caret is a 44px tap target at phone width (`min-h-11`, collapsing on a
 * precise pointer), because it is the smallest control in the row and the one
 * most likely to be missed with a thumb.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

export interface RunKindOption {
  /** Stable id, for `key` and the test id. */
  id: string
  label: string
  /** The hover/`title` text. Supplementary only -- never the sole route to anything. */
  title?: string
  /**
   * Why it cannot run now; renders disabled, with the reason VISIBLE in the
   * control's own label.
   *
   * It used to go in `title`, which is a hover tooltip -- so on touch, a
   * first-class target here, the reason did not exist at all, and a disabled
   * button is not focusable so keyboard and AT users did not reach it either
   * (#2557).
   */
  blockedReason?: string
  run: () => void
}

/**
 * The blocked reason, rendered inside the control rather than hovered over.
 *
 * One component for both call sites deliberately: they are the same statement
 * to the same reader, and the previous arrangement had the identical
 * `title={... ?? ...}` expression written out twice.
 *
 * This is the pattern the bulk Selection menu already uses -- `Apply  0 of 3
 * eligible` -- where an unavailable action is inert AND says why, with no
 * hover required.
 */
function BlockedReason({ id, reason }: { id: string; reason: string }) {
  return (
    <span
      data-testid={`ws-run-${id}-blocked`}
      className="block text-xs font-normal opacity-90"
    >
      {reason}
    </span>
  )
}

export function RunKindSplitButton({
  options,
  menuLabel,
  busyLabel,
  busy,
  primaryClassName,
}: {
  /** Most complete FIRST. The caller derives the order; this renders it. */
  options: RunKindOption[]
  /** Accessible name for the caret, e.g. "Other run kinds". */
  menuLabel: string
  /** Replaces every label while a queue is in flight. */
  busyLabel: string
  busy: boolean
  primaryClassName: string
}) {
  const [open, setOpen] = useState(false)
  const wrap = useRef<HTMLDivElement | null>(null)
  const caret = useRef<HTMLButtonElement | null>(null)
  const items = useRef<(HTMLButtonElement | null)[]>([])

  const close = useCallback(
    (returnFocus: boolean) => {
      setOpen(false)
      if (returnFocus) caret.current?.focus()
    },
    [],
  )

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

  // Focus the first item when it opens, which is what makes the keyboard path
  // usable at all: a menu you can open and not reach is worse than no menu.
  useEffect(() => {
    if (open) items.current[0]?.focus()
  }, [open])

  if (options.length === 0) return null

  const [primary, ...rest] = options

  const primaryButton = (
    <button
      type="button"
      data-testid={`ws-run-${primary.id}`}
      onClick={primary.run}
      disabled={busy || !!primary.blockedReason}
      title={primary.title}
      className={`${primaryClassName} ${rest.length > 0 ? 'rounded-s-lg' : 'rounded-lg'}`}
    >
      {busy ? busyLabel : primary.label}
      {!busy && primary.blockedReason && (
        <BlockedReason id={primary.id} reason={primary.blockedReason} />
      )}
    </button>
  )

  // One option and nothing to choose between: a caret opening an empty menu is
  // a control that cannot do anything, which is the shape this project keeps
  // deleting.
  if (rest.length === 0) return primaryButton

  const onMenuKey = (e: React.KeyboardEvent, i: number) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      close(true)
    } else if (e.key === 'ArrowDown') {
      e.preventDefault()
      items.current[(i + 1) % rest.length]?.focus()
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      items.current[(i - 1 + rest.length) % rest.length]?.focus()
    } else if (e.key === 'Tab') {
      // Tabbing out of a menu closes it, without stealing the focus move.
      setOpen(false)
    }
  }

  return (
    <div className="relative inline-flex" ref={wrap}>
      {primaryButton}
      <button
        type="button"
        ref={caret}
        data-testid="ws-run-more"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={menuLabel}
        disabled={busy}
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
        className={`${primaryClassName} rounded-e-lg border-s border-black/20 px-2 min-h-11 sm:min-h-0`}
      >
        <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <div
          role="menu"
          data-testid="ws-run-menu"
          className="absolute end-0 top-full z-20 mt-1 min-w-max rounded-lg border border-slate-700 bg-slate-800 p-1 shadow-lg"
        >
          {rest.map((o, i) => (
            <button
              key={o.id}
              type="button"
              role="menuitem"
              ref={(el) => {
                items.current[i] = el
              }}
              data-testid={`ws-run-${o.id}`}
              disabled={busy || !!o.blockedReason}
              title={o.title}
              onClick={() => {
                setOpen(false)
                o.run()
              }}
              onKeyDown={(e) => onMenuKey(e, i)}
              className="block w-full rounded px-3 py-2 text-start text-sm font-medium text-slate-200 hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed min-h-11 sm:min-h-0"
            >
              {o.label}
              {!busy && o.blockedReason && <BlockedReason id={o.id} reason={o.blockedReason} />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
