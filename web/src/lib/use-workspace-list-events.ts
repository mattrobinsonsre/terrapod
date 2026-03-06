import { useEffect, useRef } from 'react'
import { getAuthState } from '@/lib/auth'

interface WorkspaceListEvent {
  event: string
  [key: string]: unknown
}

/**
 * SSE hook for real-time workspace list updates.
 *
 * Connects to the workspace list SSE endpoint and calls onEvent
 * for each event (run status changes, drift status changes).
 * Reconnects automatically with exponential backoff.
 */
export function useWorkspaceListEvents(
  enabled: boolean,
  onEvent: (event: WorkspaceListEvent) => void,
) {
  const onEventRef = useRef(onEvent)
  onEventRef.current = onEvent

  useEffect(() => {
    if (!enabled) return

    const auth = getAuthState()
    if (!auth?.token) return

    let aborted = false
    let retryDelay = 1000
    const MAX_RETRY = 30000
    let timeoutId: ReturnType<typeof setTimeout> | undefined

    async function connect() {
      if (aborted) return

      try {
        const res = await fetch('/api/v2/workspace-events', {
          headers: { Authorization: `Bearer ${auth!.token}` },
        })

        if (!res.ok || !res.body) {
          throw new Error(`SSE connect failed: ${res.status}`)
        }

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

          let currentData = ''

          for (const line of lines) {
            if (line.startsWith('data:')) {
              currentData = line.slice(5).trim()
            } else if (line === '' && currentData) {
              try {
                const payload = JSON.parse(currentData) as WorkspaceListEvent
                onEventRef.current(payload)
              } catch {
                // Ignore malformed events
              }
              currentData = ''
            }
          }
        }
      } catch {
        // Connection failed or closed
      }

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
  }, [enabled])
}
