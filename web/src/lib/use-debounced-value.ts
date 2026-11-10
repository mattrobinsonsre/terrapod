import { useEffect, useState } from 'react'

/**
 * A value that follows its input once typing settles.
 *
 * For a text filter the server applies. Without it the request fires per
 * KEYSTROKE, which on the caches page means a full list fetch plus a totals
 * aggregate for every character — and on the VCS archive cache a storage walk
 * per character, against a bucket the operator came here because it is large.
 *
 * One producer rather than a timer in each panel: two copies of a debounce are
 * two intervals to keep in step, and the one that drifts is the one nobody
 * notices until a filter feels slower on one page than the other.
 */
export function useDebouncedValue<T>(value: T, delayMs = 350): T {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setSettled(value), delayMs)
    return () => clearTimeout(id)
  }, [value, delayMs])
  return settled
}
