import { useSSE } from '@/lib/use-sse'

interface PoolEvent {
  event: string
  listener_id?: string
  listener_name?: string
  [key: string]: unknown
}

/**
 * SSE hook for real-time agent pool events (heartbeats, joins).
 */
export function usePoolEvents(
  poolId: string | undefined,
  onEvent: (event: PoolEvent) => void,
) {
  return useSSE({
    url: `/api/v2/agent-pools/${poolId}/events`,
    enabled: !!poolId,
    onEvent: onEvent as (data: Record<string, unknown>) => void,
    onReconnect: () => onEvent({ event: 'reconnect' }),
  })
}
