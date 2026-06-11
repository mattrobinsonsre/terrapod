'use client'

import { useEffect, useMemo, useState } from 'react'
import { Search } from 'lucide-react'
import { apiFetch } from '@/lib/api'

export interface WorkspaceOption {
  id: string
  name: string
}

interface WorkspacePickerProps {
  /** Workspace IDs to exclude from results (e.g. already-linked, or self). */
  excludeIds?: string[]
  /** Called when a workspace is clicked. The parent performs the write + refetch. */
  onSelect: (ws: WorkspaceOption) => Promise<void> | void
  placeholder?: string
  /** ID of the workspace currently being acted on — shows an inline spinner on that row. */
  busyId?: string
  /** Disable the whole picker (e.g. while a parent op is in flight). */
  disabled?: boolean
  /** Max rows to show (default 20). */
  limit?: number
}

/**
 * Search-and-click workspace picker. Replaces free-text "type the exact name"
 * inputs (run triggers, remote-state sharing, module links) so a typo can't
 * 404 — you pick from a filtered, clickable list that excludes ineligible
 * workspaces. Resolves to a workspace id, so callers POST by id directly with
 * no name→id lookup.
 */
export function WorkspacePicker({
  excludeIds = [],
  onSelect,
  placeholder = 'Search workspaces…',
  busyId,
  disabled,
  limit = 20,
}: WorkspacePickerProps) {
  const [query, setQuery] = useState('')
  const [all, setAll] = useState<WorkspaceOption[]>([])
  const [loading, setLoading] = useState(true)

  // join() gives a stable dependency so the Set isn't rebuilt every render.
  const excluded = useMemo(() => new Set(excludeIds), [excludeIds.join(',')]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let active = true
    ;(async () => {
      try {
        const res = await apiFetch('/api/v2/organizations/default/workspaces')
        if (res.ok && active) {
          const data = await res.json()
          setAll(
            (data.data || []).map((ws: { id: string; attributes: { name: string } }) => ({
              id: ws.id,
              name: ws.attributes.name,
            }))
          )
        }
      } catch {
        // best-effort; empty list renders "No workspaces found"
      } finally {
        if (active) setLoading(false)
      }
    })()
    return () => {
      active = false
    }
  }, [])

  const q = query.trim().toLowerCase()
  const results = all
    .filter((ws) => !excluded.has(ws.id) && (!q || ws.name.toLowerCase().includes(q)))
    .slice(0, limit)

  return (
    <div className="p-3 bg-slate-900/50 rounded-lg border border-slate-700/30">
      <div className="flex items-center gap-2 mb-2">
        <Search size={14} className="text-slate-400" />
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={placeholder}
          disabled={disabled}
          autoFocus
          className="flex-1 px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:opacity-50"
        />
      </div>
      <div className="max-h-40 overflow-y-auto space-y-1">
        {loading ? (
          <p className="text-xs text-slate-500 py-2 text-center">Loading…</p>
        ) : results.length === 0 ? (
          <p className="text-xs text-slate-500 py-2 text-center">No workspaces found</p>
        ) : (
          results.map((ws) => (
            <button
              key={ws.id}
              type="button"
              onClick={() => onSelect(ws)}
              disabled={disabled || busyId === ws.id}
              className="w-full text-left px-2 py-1.5 rounded text-sm text-slate-300 hover:bg-slate-700/50 transition-colors disabled:opacity-50"
            >
              {ws.name}
              {busyId === ws.id && <span className="text-xs text-slate-500 ml-2">Adding…</span>}
            </button>
          ))
        )}
      </div>
    </div>
  )
}
