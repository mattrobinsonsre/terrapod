// The selection behind the Workspaces bulk actions, by workspace id.
//
// The header checkbox is also the clear: with nothing selected it selects every
// visible row, and with anything selected, a partial selection included, it
// clears. One click out from any state, so no separate Clear control is needed.

export type HeaderState = 'none' | 'some' | 'all'

export function headerState(selected: ReadonlySet<string>, visibleIds: readonly string[]): HeaderState {
  const n = visibleIds.filter((id) => selected.has(id)).length
  if (n === 0) return 'none'
  return n === visibleIds.length ? 'all' : 'some'
}

export function toggleAll(selected: ReadonlySet<string>, visibleIds: readonly string[]): Set<string> {
  return headerState(selected, visibleIds) === 'none' ? new Set(visibleIds) : new Set()
}

export function toggleOne(selected: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(selected)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  return next
}

// Drops ids no longer in the list, so an action never runs on a workspace the
// user cannot see. Returns the SAME set when nothing was dropped, so a caller that
// sets state from it does not re-render in a loop.
export function pruneSelection(selected: Set<string>, visibleIds: readonly string[]): Set<string> {
  const keep = new Set(visibleIds)
  const kept = [...selected].filter((id) => keep.has(id))
  return kept.length === selected.size ? selected : new Set(kept)
}
