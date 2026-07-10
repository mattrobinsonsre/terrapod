'use client'

// State resource graph (#765) — a thin wrapper over the shared <ResourceGraph3D>.
// Same graph as the plan/impact graph (module-clustered resources, dependency
// arrows, click-to-highlight blast radius); the only difference is what colour
// encodes — here it's the user-chosen pivot (resource type / module / provider /
// managed-vs-data), since state has no "what's happening" axis to spend colour
// on. Client-only (WebGL): the tab imports this via next/dynamic { ssr: false }.
import { useMemo } from 'react'
import {
  categoryOf,
  PALETTE,
  type StateGraphData,
  type StateNode,
} from '@/lib/state-graph'
import { ResourceGraph3D } from '@/components/resource-graph-3d'

export function StateGraph3D({
  graph,
  groupBy,
  subtitle,
}: {
  graph: StateGraphData
  groupBy: string
  subtitle: string
}) {
  const categories = useMemo(
    () => [...new Set(graph.nodes.map((n) => categoryOf(n, groupBy)))].sort(),
    [graph, groupBy],
  )
  const colorOf = (n: StateNode) =>
    PALETTE[categories.indexOf(categoryOf(n, groupBy)) % PALETTE.length]

  const legend = categories.slice(0, 18).map((c) => (
    <span
      key={c}
      className="flex items-center gap-1.5 text-[11px] font-semibold px-2 py-1 rounded-full bg-slate-700/25"
    >
      <span className="w-2 h-2 rounded-full" style={{ background: PALETTE[categories.indexOf(c) % PALETTE.length] }} />
      {c}
    </span>
  ))

  return (
    <ResourceGraph3D<StateNode>
      nodes={graph.nodes}
      edges={graph.edges}
      title="State graph"
      subtitle={subtitle}
      legend={legend}
      hint={
        <>
          Each sphere is a resource; each arrow points to what it depends on. Click a node → the
          resources that transitively <b>depend on it</b> light up.
        </>
      }
      colorOf={colorOf}
      nodeSize={(n) => 2.5 + Math.min(n.indeg, 10) * 0.35}
      sortNodes={(a, b) => b.indeg - a.indeg || a.id.localeCompare(b.id)}
      renderDetail={(n, downstream) => (
        <>
          <div className="font-mono text-xs text-slate-100 break-all">{n.id}</div>
          <div className="text-[11px] text-slate-400 mt-1">
            {n.mode === 'data' ? 'data source' : 'managed'}
            {n.provider ? ` · ${n.provider}` : ''}
            {n.module ? ` · ${n.module}` : ''}
          </div>
          <div className="text-2xl font-bold mt-1.5">
            {downstream} <span className="text-xs font-medium text-slate-400">depend on this</span>
          </div>
        </>
      )}
    />
  )
}
