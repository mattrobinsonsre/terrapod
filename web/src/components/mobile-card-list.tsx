import Link from 'next/link'
import type { ReactNode } from 'react'

/**
 * Mobile-card primitive for the `< md` half of the responsive list pattern
 * (#719). Desktop keeps its bespoke, sortable `<table>` (gated `hidden
 * md:block`); this renders the `md:hidden` stacked-card view that every list
 * tab was hand-rolling identically (runs, variables, state, …).
 *
 * Deliberately owns ONLY the mobile half — it never touches the desktop table,
 * which is what sank the earlier "own both halves" abstraction (each table has
 * different columns + its own SortableHeaders). A card is: a `title`, an
 * optional `badge` pill, a set of `{label, value}` `fields` that reflow as
 * label/value rows, and either an `actions` row (buttons) or an `href` that
 * makes the whole card tappable (mutually exclusive — you can't nest buttons
 * inside a Link).
 *
 * The row markup mirrors what the tabs already emitted so migrating an existing
 * hand-rolled card onto it is a visual no-op.
 */
export interface MobileCardField {
  label: string
  value: ReactNode
  /** dd colour class; defaults to `text-slate-300`. */
  valueClassName?: string
}

export function MobileCardList({ children }: { children: ReactNode }) {
  return <ul className="md:hidden space-y-2">{children}</ul>
}

interface MobileCardProps {
  title: ReactNode
  /** Optional pill shown top-right (status, "latest", category, …). */
  badge?: ReactNode
  fields: MobileCardField[]
  /** Action buttons row, rendered below the fields. Ignored when `href` is set. */
  actions?: ReactNode
  /** When set, the whole card is a `<Link>` (no actions row possible). */
  href?: string
}

export function MobileCard({ title, badge, fields, actions, href }: MobileCardProps) {
  const body = (
    <>
      <div className="mb-2 flex items-start justify-between gap-2">
        {title}
        {badge}
      </div>
      {fields.length > 0 && (
        <dl className="space-y-1 text-xs">
          {fields.map((f) => (
            <div key={f.label} className="flex items-baseline justify-between gap-3">
              <dt className="shrink-0 text-slate-500">{f.label}</dt>
              <dd className={`min-w-0 break-words text-right ${f.valueClassName ?? 'text-slate-300'}`}>
                {f.value}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {actions && <div className="mt-2 flex flex-wrap gap-2">{actions}</div>}
    </>
  )

  const cardClass = 'block rounded-lg border border-slate-700/50 bg-slate-800/50 p-3'

  if (href) {
    return (
      <li>
        <Link href={href} className={`${cardClass} active:bg-slate-700/30`}>
          {body}
        </Link>
      </li>
    )
  }
  return <li className={cardClass}>{body}</li>
}
