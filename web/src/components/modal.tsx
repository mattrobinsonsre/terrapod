'use client'

import { useEffect, useRef } from 'react'

/**
 * Accessible modal dialog primitive.
 *
 * Provides what every inline `fixed inset-0` modal in the app was missing:
 * `role="dialog"` + `aria-modal`, Escape-to-close, a focus trap (Tab cycles
 * within the dialog), initial focus into the dialog, focus restoration to the
 * previously-focused element on close, and backdrop-click-to-close. Especially
 * important on destructive flows (catalog destroy / orphan) that a keyboard or
 * screen-reader user otherwise can't perceive or dismiss.
 */

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

export function Modal({
  open,
  onClose,
  title,
  children,
  panelClassName = 'bg-slate-800 rounded-lg border border-slate-700 w-full max-w-lg p-5',
}: {
  open: boolean
  onClose: () => void
  /** Accessible name for the dialog (screen readers). */
  title?: string
  children: React.ReactNode
  /** Full visual classes for the panel (bg/border/rounded/width/padding). */
  panelClassName?: string
}) {
  const panelRef = useRef<HTMLDivElement>(null)
  const restoreRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!open) return
    restoreRef.current = document.activeElement as HTMLElement
    const panel = panelRef.current
    const first = panel?.querySelector<HTMLElement>(FOCUSABLE)
    ;(first ?? panel)?.focus()

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
        return
      }
      if (e.key !== 'Tab' || !panel) return
      const items = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE))
      if (items.length === 0) return
      const firstEl = items[0]
      const lastEl = items[items.length - 1]
      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault()
        lastEl.focus()
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault()
        firstEl.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      restoreRef.current?.focus?.()
    }
  }, [open, onClose])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        className={`shadow-xl focus:outline-none ${panelClassName}`}
      >
        {children}
      </div>
    </div>
  )
}
