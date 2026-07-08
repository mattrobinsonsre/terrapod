// Compact stat chip — a small inline "LABEL value" pill used for the workspace
// list's Total / Health / Locked counts (#719). Replaces the big stat cards on
// every viewport so the counts don't burn vertical space. When `onClick` is
// supplied the chip is an interactive toggle (e.g. Health → `status:unhealthy`
// filter); otherwise it's a static readout. One primitive → all three counts
// stay visually consistent and DRY.

export function StatChip({
  label,
  value,
  valueClassName,
  onClick,
  active,
  activeClassName = 'bg-red-500/10 border-red-500/50',
  ariaLabel,
  className,
}: {
  label: string
  value: React.ReactNode
  valueClassName?: string
  onClick?: () => void
  active?: boolean
  // Tailwind classes for the active (filter-applied) state. Defaults to red
  // (health/error); pass an amber variant for neutral toggles like "Locked".
  activeClassName?: string
  ariaLabel?: string
  className?: string
}) {
  const base =
    'inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium border transition-colors whitespace-nowrap'
  const content = (
    <>
      <span className="text-xs uppercase tracking-wider text-slate-500">{label}</span>
      <span className={'font-semibold ' + (valueClassName ?? 'text-slate-200')}>{value}</span>
    </>
  )

  if (onClick) {
    return (
      <button
        type="button"
        aria-pressed={active}
        aria-label={ariaLabel}
        title={ariaLabel}
        onClick={onClick}
        className={
          base +
          ' focus:outline-none focus:ring-2 focus:ring-brand-500 ' +
          (active
            ? activeClassName
            : 'bg-slate-800/50 border-slate-700/50 hover:bg-slate-700/60') +
          (className ? ' ' + className : '')
        }
      >
        {content}
      </button>
    )
  }
  return (
    <div className={base + ' bg-slate-800/50 border-slate-700/50' + (className ? ' ' + className : '')}>
      {content}
    </div>
  )
}
