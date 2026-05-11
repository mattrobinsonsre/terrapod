/**
 * Compact resource-change summary for a planned run.
 *
 * Renders one chip per non-zero count (additions, changes, destructions,
 * replacements, imports), TFE/HCP-style: `+5  ~2  -1  ⟳3  ↓1`. When every
 * count is zero we render a single "No changes" pill so the user knows the
 * plan ran and reported nothing to do (distinct from "we don't know yet",
 * which is signalled by `summary == null` — the caller renders nothing in
 * that case).
 */

interface PlanSummary {
  add: number
  change: number
  destroy: number
  replace: number
  import: number
}

interface Props {
  summary: PlanSummary
  size?: 'sm' | 'md'
}

export function PlanSummaryBadges({ summary, size = 'md' }: Props) {
  const { add, change, destroy, replace, import: imports } = summary
  const total = add + change + destroy + replace + imports

  const containerCls = `flex flex-wrap items-center gap-1.5 ${size === 'sm' ? '' : 'mb-4'}`

  // Zero-count case: render nothing. Callers that want a "No changes"
  // pill (e.g. inline on the runs list) wrap a small inline render
  // around this; the run detail page already has its own No-changes
  // callout block, so a duplicate pill would just be noise.
  if (total === 0) {
    if (size === 'sm') {
      return (
        <span
          className="inline-flex items-center rounded-full bg-slate-700/40 text-slate-300 px-2 py-0.5 text-xs"
          title="Plan reported no resource changes"
        >
          no changes
        </span>
      )
    }
    return null
  }

  const cls =
    size === 'sm'
      ? 'inline-flex items-center rounded px-1.5 py-0.5 text-xs font-mono font-medium'
      : 'inline-flex items-center rounded px-2 py-0.5 text-sm font-mono font-medium'

  return (
    <div className={containerCls}>
      {add > 0 && (
        <span className={`${cls} bg-green-900/40 text-green-300`} title={`${add} to add`}>
          +{add}
        </span>
      )}
      {change > 0 && (
        <span className={`${cls} bg-amber-900/40 text-amber-300`} title={`${change} to change`}>
          ~{change}
        </span>
      )}
      {destroy > 0 && (
        <span className={`${cls} bg-red-900/40 text-red-300`} title={`${destroy} to destroy`}>
          -{destroy}
        </span>
      )}
      {replace > 0 && (
        <span
          className={`${cls} bg-purple-900/40 text-purple-300`}
          title={`${replace} to replace`}
        >
          ⟳{replace}
        </span>
      )}
      {imports > 0 && (
        <span className={`${cls} bg-blue-900/40 text-blue-300`} title={`${imports} to import`}>
          ↓{imports}
        </span>
      )}
    </div>
  )
}
