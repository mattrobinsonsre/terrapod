// Pure (SSR-safe) types + helpers for the estate topology (#763). Kept free of
// three / react-force-graph so the page can import them without pulling WebGL
// (which touches `window`) into the server prerender — only the dynamic,
// ssr:false renderer imports those.

export type EstateNode = {
  id: string
  kind: 'workspace' | 'module'
  name: string
  labels: Record<string, string>
  pool: string
  indeg: number
  // react-force-graph mutates positions/velocities onto nodes after layout
  x?: number
  y?: number
  z?: number
  vx?: number
  vy?: number
  vz?: number
}
export type EstateEdge = { source: string | EstateNode; target: string | EstateNode; kind: string }
export type EstateGraphData = {
  nodes: EstateNode[]
  edges: EstateEdge[]
  meta: { counts: Record<string, number> }
}

export const PALETTE = ['#3b82f6','#22c55e','#f59e0b','#ec4899','#a855f7','#14b8a6','#ef4444','#84cc16','#06b6d4','#f97316','#8b5cf6','#eab308','#64748b']
export const MODULE_COLOR = '#fbbf24'
export const EDGE_COLOR: Record<string, string> = {
  'remote-state': '#22d3ee',
  'run-trigger': '#f97316',
  'uses-module': '#a78bfa',
}

export const endId = (x: string | EstateNode): string => (typeof x === 'string' ? x : x.id)

// The grouping axes for an estate — derived from the DATA, never assumed (the
// platform enforces no labelling convention).
export function groupAxes(nodes: EstateNode[]): { value: string; label: string }[] {
  const ws = nodes.filter((n) => n.kind === 'workspace')
  const keys = [...new Set(ws.flatMap((n) => Object.keys(n.labels)))].sort()
  return [
    { value: 'none', label: 'Nothing (single colour)' },
    ...keys.map((k) => ({ value: 'label:' + k, label: 'label: ' + k })),
    { value: 'pool', label: 'Agent pool' },
    { value: 'prefix', label: 'Name prefix' },
  ]
}

export function categoryOf(n: EstateNode, groupBy: string): string {
  if (groupBy === 'none') return 'workspace'
  if (groupBy === 'pool') return n.pool
  if (groupBy === 'prefix') return n.name.split('-')[0]
  const k = groupBy.slice(6)
  return k in n.labels ? n.labels[k] : '(unset)'
}
