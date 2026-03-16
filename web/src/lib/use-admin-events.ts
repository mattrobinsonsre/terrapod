import { useSSE } from '@/lib/use-sse'

interface AdminEvent {
  event: string
  [key: string]: unknown
}

/**
 * SSE hook for real-time admin health dashboard updates.
 */
export function useAdminEvents(
  enabled: boolean,
  onEvent: (event: AdminEvent) => void,
) {
  return useSSE({
    url: '/api/v2/admin/health-dashboard/events',
    enabled,
    onEvent: onEvent as (data: Record<string, unknown>) => void,
    onReconnect: () => onEvent({ event: 'reconnect' }),
  })
}
