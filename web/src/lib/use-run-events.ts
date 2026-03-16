import { useSSE } from '@/lib/use-sse'

interface RunEvent {
  event: string
  workspace_id: string
  run_id?: string
  old_status?: string
  new_status?: string
  [key: string]: unknown
}

/**
 * SSE hook for real-time workspace events (run status changes,
 * lock/unlock, state uploads, workspace updates).
 */
export function useRunEvents(
  workspaceId: string | undefined,
  onEvent: (event: RunEvent) => void,
) {
  return useSSE({
    url: `/api/v2/workspaces/${workspaceId}/runs/events`,
    enabled: !!workspaceId,
    onEvent: onEvent as (data: Record<string, unknown>) => void,
    onReconnect: () => onEvent({ event: 'reconnect', workspace_id: workspaceId! }),
  })
}
