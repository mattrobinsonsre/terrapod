import { useEffect, useRef, useState } from 'react'
import { getAuthState } from '@/lib/auth'

interface UseSSEOptions {
  /** SSE endpoint path */
  url: string
  /** Connect when true */
  enabled: boolean
  /** Called on each parsed event */
  onEvent: (data: Record<string, unknown>) => void
  /** Called after successful reconnect (not initial connect) — use for data refresh */
  onReconnect?: () => void
  /** Detect silent drops if no data within this interval (default: 10000ms) */
  readTimeoutMs?: number
}

interface UseSSEResult {
  /** True when stream is actively receiving data */
  connected: boolean
}

/**
 * Shared SSE engine with robust connection handling.
 *
 * Features:
 * - Read timeout detection (catches NAT timeouts, hung backends)
 * - Reconnect-triggers-reload via onReconnect callback
 * - Exponential backoff (1s → 30s cap, resets on success)
 * - Visibility-aware (pauses in background tabs)
 */
export function useSSE(options: UseSSEOptions): UseSSEResult {
  const { url, enabled, readTimeoutMs = 10_000 } = options
  const onEventRef = useRef(options.onEvent)
  const onReconnectRef = useRef(options.onReconnect)

  useEffect(() => {
    onEventRef.current = options.onEvent
    onReconnectRef.current = options.onReconnect
  })

  const [connected, setConnected] = useState(false)

  useEffect(() => {
    if (!enabled) return

    const auth = getAuthState()
    if (!auth?.token) return

    let aborted = false
    let retryDelay = 1000
    const MAX_RETRY = 30000
    let timeoutId: ReturnType<typeof setTimeout> | undefined
    let hasConnectedBefore = false
    let activeController: AbortController | undefined
    let generation = 0

    async function connect() {
      if (aborted) return

      // Skip reconnection in background tabs
      if (document.hidden) {
        timeoutId = setTimeout(connect, 2000)
        return
      }

      // Abort any existing connection before starting a new one
      activeController?.abort()
      const myGeneration = ++generation

      try {
        const controller = new AbortController()
        activeController = controller
        const res = await fetch(url, {
          headers: { Authorization: `Bearer ${auth!.token}` },
          signal: controller.signal,
        })

        if (!res.ok || !res.body) {
          throw new Error(`SSE connect failed: ${res.status}`)
        }

        // Reset retry delay on successful connection
        retryDelay = 1000
        setConnected(true)

        // Fire reconnect callback (not on initial connect)
        if (hasConnectedBefore) {
          onReconnectRef.current?.()
        }
        hasConnectedBefore = true

        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        let currentData = ''

        while (!aborted) {
          // Read timeout: if no data within readTimeoutMs, assume dead connection
          const readTimeout = setTimeout(() => {
            controller.abort()
          }, readTimeoutMs)

          const { done, value } = await reader.read()
          clearTimeout(readTimeout)

          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          for (const rawLine of lines) {
            const line = rawLine.replace(/\r$/, '')
            if (line.startsWith('data:')) {
              currentData = line.slice(5).trim()
            } else if (line === '' && currentData) {
              try {
                const payload = JSON.parse(currentData) as Record<string, unknown>
                onEventRef.current(payload)
              } catch {
                // Ignore malformed events
              }
              currentData = ''
            }
            // Ignore comment lines (keepalives start with ':')
          }
        }
      } catch {
        // Connection failed, closed, or timed out
      }

      // Only clean up if this is still the active connection (not superseded)
      if (generation !== myGeneration) return

      activeController = undefined
      setConnected(false)

      // Reconnect with exponential backoff
      if (!aborted) {
        timeoutId = setTimeout(() => {
          retryDelay = Math.min(retryDelay * 2, MAX_RETRY)
          connect()
        }, retryDelay)
      }
    }

    connect()

    // Pause/resume on visibility change
    function onVisibilityChange() {
      if (!document.hidden && !aborted) {
        // Tab became visible — abort stale connection and reconnect
        activeController?.abort()
        if (timeoutId !== undefined) {
          clearTimeout(timeoutId)
          timeoutId = undefined
        }
        connect()
      }
    }
    document.addEventListener('visibilitychange', onVisibilityChange)

    return () => {
      aborted = true
      activeController?.abort()
      if (timeoutId !== undefined) clearTimeout(timeoutId)
      document.removeEventListener('visibilitychange', onVisibilityChange)
      setConnected(false)
    }
  }, [url, enabled, readTimeoutMs])

  return { connected }
}
