interface PageHeaderProps {
  title: string
  description?: string
  actions?: React.ReactNode
}

export function PageHeader({ title, description, actions }: PageHeaderProps) {
  // Stack title over actions on a phone, side-by-side from `sm` up. A wide
  // actions cluster (e.g. a "New …" button + filters) can't fit beside the
  // title at phone width — forcing a row there pushes it off the edge
  // (horizontal-scroll / clipped-button regression, #719). The `sm:`-prefixed
  // row rules keep desktop pixel-identical.
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between sm:gap-4 mb-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-100">{title}</h1>
        {/* The explanatory subtitle is desktop-only — on a phone the title
            alone is enough and the vertical space is better spent on content
            (#719). Restored at `sm`+ so desktop is unchanged. */}
        {description && (
          <p className="text-slate-400 mt-1 hidden sm:block">{description}</p>
        )}
      </div>
      {actions && <div className="sm:flex-shrink-0">{actions}</div>}
    </div>
  )
}
