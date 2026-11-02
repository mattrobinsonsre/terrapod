interface EmptyStateProps {
  message: string
}

export function EmptyState({ message }: EmptyStateProps) {
  return (
    // The test id is what lets the screenshot capture refuse to publish a
    // page whose whole content is "nothing here yet" -- a documentation
    // image of an empty state teaches a reader nothing, and it is not
    // something an error or spinner check can see. Matching on the
    // rendered COMPONENT rather than on its words keeps that guard exact
    // across all 32 locales.
    <div data-testid="empty-state" className="text-center py-12 text-slate-500">
      {message}
    </div>
  )
}
