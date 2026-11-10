import { useState, useMemo, useEffect } from 'react'
import { parseSort, readPref, serializeSort, sortStorageKey, writePref } from './view-prefs'

export interface SortState<K extends string> {
  key: K
  direction: 'asc' | 'desc'
}

export function useSortable<T, K extends string>(
  items: T[],
  defaultKey: K,
  defaultDirection: 'asc' | 'desc',
  accessor: (item: T, key: K) => string | number | null | undefined,
  // A table id opts this call site in to keeping its sort for the life of the tab.
  tableId?: string,
) {
  const storageKey = tableId ? sortStorageKey(tableId) : null
  // Read in the initializer, as the Workspaces page does for its filter, so there is no
  // effect that sets state and no first render with the default sort.
  const [sortState, setSortState] = useState<SortState<K>>(() => {
    const stored =
      storageKey && typeof window !== 'undefined'
        ? parseSort<K>(readPref(window.sessionStorage, storageKey))
        : null
    return stored ?? { key: defaultKey, direction: defaultDirection }
  })

  useEffect(() => {
    if (!storageKey) return
    const isDefault = sortState.key === defaultKey && sortState.direction === defaultDirection
    writePref(window.sessionStorage, storageKey, isDefault ? '' : serializeSort(sortState))
  }, [sortState, storageKey, defaultKey, defaultDirection])

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
