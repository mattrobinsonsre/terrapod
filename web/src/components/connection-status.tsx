/**
 * Small unobtrusive indicator shown when SSE connection is lost.
 * Renders nothing when connected.
 */
export function ConnectionStatus({ connected }: { connected: boolean }) {
  if (connected) return null

  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-amber-400">
      <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
      Reconnecting...
    </span>
  )
}
