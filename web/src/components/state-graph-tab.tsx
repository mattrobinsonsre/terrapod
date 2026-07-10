'use client'

// State resource graph tab (#765) — rendered inside the workspace detail page.
// A WebGL dependency graph of the workspace's Terraform state on desktop, with
// an equivalent accessible table (the fallback required by #736 — 3D is never
// the only path) that is also the default on a phone (#719). Defaults to the
// current state version; a picker drops back to any older one.
import { useEffect, useMemo, useState } from 'react'
import dynamic from 'next/dynamic'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { apiFetch } from '@/lib/api'
import { useIsMobile } from '@/lib/use-media-query'
import {
  groupAxes,
  categoryOf,
  PALETTE,
  type StateGraphData,
  type StateNode,
} from '@/lib/state-graph'

const StateGraph3D = dynamic(
  () => import('@/components/state-graph-3d').then((m) => m.StateGraph3D),
  { ssr: false, loading: () => <LoadingSpinner /> },
)

type View = 'graph' | 'table'

function ToggleBtn({ v, label, view, onClick }: { v: View; label: string; view: View; onClick: (v: View) => void }) {
  return (
    <button
      onClick={() => onClick(v)}
      className={`px-3 py-1.5 rounded-lg text-xs font-medium ${
        view === v
          ? 'bg-brand-500/25 text-brand-300 outline outline-1 outline-brand-500/50'
          : 'bg-slate-800 text-slate-300 hover:bg-slate-700'
      }`}
    >
      {label}
    </button>
  )
}

export function StateGraphTab({ workspaceId }: { workspaceId: string }) {
  const [graph, setGraph] = useState<StateGraphData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState<string>('') // '' = current
  const [groupBy, setGroupBy] = useState('type')
  const [selected, setSelected] = useState<StateNode | null>(null)
  const isMobile = useIsMobile()
  const [viewOverride, setViewOverride] = useState<View | null>(null)
  // Phones default to the table (WebGL is heavy + the graph is desktop-oriented);
  // the toggle is one tap away. Derived — no setState-in-effect.
  const view: View = viewOverride ?? (isMobile ? 'table' : 'graph')

  useEffect(() => {
    let cancelled = false
    const q = version ? `?state_version=${encodeURIComponent(version)}` : ''
    apiFetch(`/api/terrapod/v1/workspaces/${workspaceId}/state-graph${q}`)
      .then(async (r) => {
        if (!r.ok) throw new Error('Failed to load the state graph.')
        const b = await r.json()
        if (!cancelled) {
          setGraph(b.data.attributes as StateGraphData)
          setError(null)
        }
      })
      .catch((e: Error) => !cancelled && setError(e.message))
    return () => {
      cancelled = true
    }
  }, [workspaceId, version])

  const axes = useMemo(() => (graph ? groupAxes(graph.nodes) : []), [graph])
  const categories = useMemo(() => {
    if (!graph) return []
    return [...new Set(graph.nodes.map((n) => categoryOf(n, groupBy)))].sort()
  }, [graph, groupBy])
  const colorFor = (c: string) => PALETTE[categories.indexOf(c) % PALETTE.length]

  const sortedNodes = useMemo(
    () => (graph ? [...graph.nodes].sort((a, b) => b.indeg - a.indeg || a.id.localeCompare(b.id)) : []),
    [graph],
  )

  if (error) return <ErrorBanner message={error} />
  if (!graph) return <LoadingSpinner />

  const versions = graph.meta.versions
  if (versions.length === 0) {
    return <EmptyState message="No state yet — run a plan and apply, or upload state, then this graph will populate." />
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <label className="text-xs text-slate-400">
          State version{' '}
          <select
            value={version}
            onChange={(e) => {
              setSelected(null)
              setVersion(e.target.value)
            }}
            className="ml-1 text-sm bg-slate-800 border border-slate-700 rounded-lg px-2 py-1.5 text-slate-100"
          >
            {versions.map((v) => (
              <option key={v.id} value={v.is_current ? '' : v.id}>
                v{v.serial}
                {v.is_current ? ' (current)' : ''} · {new Date(v.created_at).toLocaleString()}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-slate-400">
          Group by{' '}
          <select
            value={groupBy}
            onChange={(e) => setGroupBy(e.target.value)}
            className="ml-1 text-sm bg-slate-800 border border-slate-700 rounded-lg px-2 py-1.5 text-slate-100"
          >
            {axes.map((a) => (
              <option key={a.value} value={a.value}>
                {a.label}
              </option>
            ))}
          </select>
        </label>
        <div className="flex gap-1">
          <ToggleBtn v="graph" label="Graph" view={view} onClick={setViewOverride} />
          <ToggleBtn v="table" label="Table" view={view} onClick={setViewOverride} />
        </div>
        <span className="text-xs text-slate-500">
          {graph.meta.counts.resources} resources · {graph.meta.counts.edges} dependencies
        </span>
      </div>

      {graph.meta.truncated && (
        <p className="mb-3 text-xs text-amber-400">
          Showing the first {graph.meta.max_nodes} of {graph.meta.total_resources} resources — the
          graph is capped for legibility.
        </p>
      )}

      {/* legend — shared by both views */}
      <div className="flex flex-wrap gap-x-4 gap-y-1.5 mb-4 text-[11px] text-slate-300">
        {categories.slice(0, 20).map((c) => (
          <span key={c} className="flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-full" style={{ background: colorFor(c) }} />
            {c}
          </span>
        ))}
        <span className="flex items-center gap-1.5 text-slate-400">
          <span className="w-2.5 h-2.5 rounded-sm border border-slate-500" />
          data source
        </span>
      </div>

      {view === 'graph' ? (
        <div className="relative w-full h-[70vh] min-h-[420px] rounded-xl overflow-hidden border border-slate-800 bg-[#0a0e17]">
          <StateGraph3D
            graph={graph}
            groupBy={groupBy}
            selectedId={selected?.id ?? null}
            onSelect={setSelected}
          />
          {selected && (
            <div className="absolute z-10 bottom-3 left-3 max-w-[min(360px,80vw)] rounded-xl border border-slate-700/40 bg-slate-900/85 backdrop-blur px-4 py-3">
              <div className="font-mono text-xs text-slate-100 break-all">{selected.name}</div>
              <div className="text-[11px] text-slate-400 mt-1">
                {selected.mode === 'data' ? 'data source' : 'managed'}
                {selected.provider ? ` · ${selected.provider}` : ''} · {selected.module}
              </div>
              <div className="text-lg font-bold mt-1.5">
                {selected.indeg}{' '}
                <span className="text-xs font-medium text-slate-400">resources depend on this</span>
              </div>
            </div>
          )}
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full text-sm">
            <thead className="bg-slate-800/50 text-slate-400 text-xs">
              <tr>
                <th scope="col" className="text-left px-3 py-2">Resource</th>
                <th scope="col" className="text-left px-3 py-2">Type</th>
                <th scope="col" className="text-left px-3 py-2">Mode</th>
                <th scope="col" className="text-left px-3 py-2">Module</th>
                <th scope="col" className="text-right px-3 py-2">Depended on by</th>
              </tr>
            </thead>
            <tbody>
              {sortedNodes.map((n) => (
                <tr key={n.id} className="border-t border-slate-800/70">
                  <th scope="row" className="text-left px-3 py-2 font-mono text-xs text-slate-100 font-normal break-all">
                    {n.name}
                  </th>
                  <td className="px-3 py-2 text-slate-300">{n.type}</td>
                  <td className="px-3 py-2 text-slate-400">{n.mode}</td>
                  <td className="px-3 py-2 text-slate-400 text-xs">{n.module}</td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-300">{n.indeg}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
