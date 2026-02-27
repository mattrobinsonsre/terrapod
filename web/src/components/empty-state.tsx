interface EmptyStateProps {
  message: string
}

export function EmptyState({ message }: EmptyStateProps) {
  return (
    <div className="text-center py-12 text-slate-500">
      {message}
    </div>
  )
}
