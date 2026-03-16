import { useEffect, useRef } from 'react'

/**
 * Reusable polling hook that runs a callback at a fixed interval.
 * Skips ticks when the tab is hidden (document.hidden).
 */
export function usePollingInterval(
  enabled: boolean,
  intervalMs: number,
  callback: () => void,
) {
  const callbackRef = useRef(callback)

  useEffect(() => {
    callbackRef.current = callback
  })

  useEffect(() => {
    if (!enabled) return

    const id = setInterval(() => {
      if (!document.hidden) {
        callbackRef.current()
      }
    }, intervalMs)

    return () => clearInterval(id)
  }, [enabled, intervalMs])
}
