interface PageHeaderProps {
  title: string
  description?: string
  actions?: React.ReactNode
}

export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <div className="flex items-start justify-between gap-4 mb-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-100">{title}</h1>
        {description && (
          <p className="text-slate-400 mt-1">{description}</p>
        )}
      </div>
      {actions && <div className="flex-shrink-0">{actions}</div>}
    </div>
  )
}
