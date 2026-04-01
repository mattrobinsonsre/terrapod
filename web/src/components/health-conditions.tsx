interface HealthCondition {
  code: string
  severity: 'error' | 'warning'
  title: string
  detail: string
}

const severityStyles: Record<string, { border: string; bg: string; icon: string; title: string; detail: string }> = {
  error: {
    border: 'border-red-500/50',
    bg: 'bg-red-500/10',
    icon: 'text-red-400',
    title: 'text-red-300',
    detail: 'text-red-400/80',
  },
  warning: {
    border: 'border-amber-500/50',
    bg: 'bg-amber-500/10',
    icon: 'text-amber-400',
    title: 'text-amber-300',
    detail: 'text-amber-400/80',
  },
}

export function HealthConditions({ conditions }: { conditions: HealthCondition[] }) {
  if (!conditions || conditions.length === 0) return null

  return (
    <div className="space-y-3">
      {conditions.map((c) => {
        const s = severityStyles[c.severity] || severityStyles.warning
        return (
          <div key={c.code} className={`rounded-lg border ${s.border} ${s.bg} p-4 flex items-start gap-3`}>
            <svg className={`w-5 h-5 ${s.icon} mt-0.5 flex-shrink-0`} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z" />
            </svg>
            <div>
              <p className={`text-sm font-medium ${s.title}`}>{c.title}</p>
              <p className={`text-xs ${s.detail} mt-1`}>{c.detail}</p>
            </div>
          </div>
        )
      })}
    </div>
  )
}
