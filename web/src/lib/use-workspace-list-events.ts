import { useSSE } from '@/lib/use-sse'

interface WorkspaceListEvent {
  event: string
  [key: string]: unknown
}

/**
 * SSE hook for real-time workspace list updates.
 */
export function useWorkspaceListEvents(
  enabled: boolean,
  onEvent: (event: WorkspaceListEvent) => void,
) {
  return useSSE({
    url: '/api/v2/workspace-events',
    enabled,
    onEvent: onEvent as (data: Record<string, unknown>) => void,
    onReconnect: () => onEvent({ event: 'reconnect' }),
  })
}
