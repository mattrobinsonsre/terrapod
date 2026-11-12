// A filter or a sort is a transient working set, so it lives in sessionStorage: it survives
// reload and in-tab navigation, dies with the tab, and never reaches another tab. Durable
// display preferences (grouping) stay in localStorage.

export const FILTER_KEY = 'terrapod:workspace-filter'
export const ENGINE_KEY = 'terrapod:workspace-engine'

// One key per call site, not per page: several pages sort more than one table.
export const sortStorageKey = (tableId: string) => `terrapod:sort:${tableId}`

export interface PrefStore {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

// Storage can be missing or throw (private mode, disabled), so reads and writes never raise.
export function readPref(store: PrefStore | null, key: string): string | null {
  if (!store) return null
  try {
    return store.getItem(key)
  } catch {
    return null
  }
}

// An empty value removes the key, so a cleared preference stays cleared.
export function writePref(store: PrefStore | null, key: string, value: string): void {
  if (!store) return
  try {
    if (value) store.setItem(key, value)
    else store.removeItem(key)
  } catch {
    /* storage unavailable: the preference just does not persist */
  }
}

// A shared link wins over what this browser stored.
export function resolveInitial(fromUrl: string | null, stored: string | null): string {
  return fromUrl || stored || ''
}

// A stored engine filter is only usable while the selector that clears it is shown
// (two or more engines offered) and still offers that engine. If the list could not be
// loaded, or no longer includes it, the filter would hide every workspace with no way to undo it.
export function usableEngine(engine: string, offered: readonly string[]): string {
  return offered.length > 1 && offered.includes(engine) ? engine : ''
}

export interface SortPref<K extends string> {
  key: K
  direction: 'asc' | 'desc'
}

export function serializeSort<K extends string>(sort: SortPref<K>): string {
  return `${sort.key}:${sort.direction}`
}

// The key is not checked against the table's columns: the stored value dies with the tab, and a
// key the table no longer sorts by just leaves it unsorted until a header is clicked.
export function parseSort<K extends string>(value: string | null): SortPref<K> | null {
  if (!value) return null
  const i = value.lastIndexOf(':')
  if (i <= 0) return null
  const direction = value.slice(i + 1)
  if (direction !== 'asc' && direction !== 'desc') return null
  return { key: value.slice(0, i) as K, direction }
}
