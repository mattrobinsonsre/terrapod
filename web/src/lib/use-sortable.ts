import { useState, useMemo, useEffect } from 'react'
import { parseSort, readPref, serializeSort, writePref } from './workspace-view-prefs'

export interface SortState<K extends string> {
  key: K
  direction: 'asc' | 'desc'
}

export function useSortable<T, K extends string>(
  items: T[],
  defaultKey: K,
  defaultDirection: 'asc' | 'desc',
  accessor: (item: T, key: K) => string | number | null | undefined,
  persist?: { storageKey: string; keys: readonly K[] },
) {
  // Opt-in: only a caller that passes `persist` keeps its sort across navigation.
  const [sortState, setSortState] = useState<SortState<K>>({
    key: defaultKey,
    direction: defaultDirection,
  })

  // Writes wait until the stored sort has been applied, so the default never overwrites it.
  const [restored, setRestored] = useState(false)
  const storageKey = persist?.storageKey
  const keys = persist?.keys

  useEffect(() => {
    if (!storageKey || !keys) return
    const stored = parseSort(readPref(window.localStorage, storageKey), keys)
    if (stored) setSortState(stored)
    setRestored(true)
    // Read once on mount: the stored value is the starting point, not a live source.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storageKey])

  useEffect(() => {
    if (!storageKey || !restored) return
    const isDefault = sortState.key === defaultKey && sortState.direction === defaultDirection
    writePref(window.localStorage, storageKey, isDefault ? '' : serializeSort(sortState))
  }, [sortState, restored, storageKey, defaultKey, defaultDirection])

  function toggleSort(key: K) {
    setSortState((prev) =>
      prev.key === key
        ? { key, direction: prev.direction === 'asc' ? 'desc' : 'asc' }
        : { key, direction: 'asc' },
    )
  }

  const sortedItems = useMemo(() => {
    const { key, direction } = sortState
    return [...items].sort((a, b) => {
      const va = accessor(a, key)
      const vb = accessor(b, key)

      // null/undefined sort last regardless of direction
      if (va == null && vb == null) return 0
      if (va == null) return 1
      if (vb == null) return -1

      let cmp: number
      if (typeof va === 'number' && typeof vb === 'number') {
        cmp = va - vb
      } else {
        const sa = String(va)
        const sb = String(vb)
        // Try parsing as dates for ISO date strings
        if (sa.match(/^\d{4}-\d{2}-\d{2}/) && sb.match(/^\d{4}-\d{2}-\d{2}/)) {
          cmp = new Date(sa).getTime() - new Date(sb).getTime()
        } else {
          cmp = sa.localeCompare(sb, undefined, { sensitivity: 'base' })
        }
      }

      return direction === 'asc' ? cmp : -cmp
    })
  }, [items, sortState, accessor])

  return { sortedItems, sortState, toggleSort }
}
