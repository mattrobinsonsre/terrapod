// The Workspaces list keeps its filter, engine filter and sort until the user
// clears them, the way the grouping mode already does: URL first, then storage.

export const FILTER_KEY = 'terrapod:workspace-filter'
export const ENGINE_KEY = 'terrapod:workspace-engine'
export const SORT_KEY = 'terrapod:workspace-sort'

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
// (two or more engines) and still offers that engine; otherwise it would hide
// every workspace with no way to undo it.
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

export function parseSort<K extends string>(value: string | null, keys: readonly K[]): SortPref<K> | null {
  if (!value) return null
  const [key, direction] = value.split(':')
  if (direction !== 'asc' && direction !== 'desc') return null
  const match = keys.find((k) => k === key)
  return match ? { key: match, direction } : null
}
