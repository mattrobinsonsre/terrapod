import { useEffect, useRef } from 'react'
import { getAuthState } from '@/lib/auth'

interface RunEvent {
  event: string
  run_id: string
  workspace_id: string
  old_status: string
  new_status: string
}

/**
 * SSE hook for real-time run status updates.
 *
 * Connects to the workspace run events SSE endpoint and calls onEvent
 * for each status change. Reconnects automatically with exponential backoff.
 */
export function useRunEvents(
  workspaceId: string | undefined,
  onEvent: (event: RunEvent) => void,
) {
  const onEventRef = useRef(onEvent)
  onEventRef.current = onEvent

  useEffect(() => {
    if (!workspaceId) return

    const auth = getAuthState()
    if (!auth?.token) return

    let aborted = false
    let retryDelay = 1000
    const MAX_RETRY = 30000
    let timeoutId: ReturnType<typeof setTimeout> | undefined

    async function connect() {
      if (aborted) return

      try {
        const res = await fetch(`/api/v2/workspaces/${workspaceId}/runs/events`, {
          headers: { Authorization: `Bearer ${auth!.token}` },
        })

        if (!res.ok || !res.body) {
          throw new Error(`SSE connect failed: ${res.status}`)
        }

        // Reset retry delay on successful connection
        retryDelay = 1000

        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        while (!aborted) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          let currentEvent = ''
          let currentData = ''

          for (const line of lines) {
            if (line.startsWith('event:')) {
              currentEvent = line.slice(6).trim()
            } else if (line.startsWith('data:')) {
              currentData = line.slice(5).trim()
            } else if (line === '' && currentData) {
              // Empty line = end of event
              try {
                const payload = JSON.parse(currentData) as RunEvent
                onEventRef.current(payload)
              } catch {
                // Ignore malformed events
              }
              currentEvent = ''
              currentData = ''
            }
            // Ignore comment lines (keepalives start with ':')
          }
        }
      } catch {
        // Connection failed or closed
      }

      // Reconnect with exponential backoff
      if (!aborted) {
        timeoutId = setTimeout(() => {
          retryDelay = Math.min(retryDelay * 2, MAX_RETRY)
          connect()
        }, retryDelay)
      }
    }

    connect()

    return () => {
      aborted = true
      if (timeoutId !== undefined) clearTimeout(timeoutId)
    }
  }, [workspaceId])
}
