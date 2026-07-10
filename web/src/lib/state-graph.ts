// Pure (SSR-safe) types + helpers for the single-workspace state resource graph
// (#765). Kept free of three / react-force-graph so the tab can import them
// without pulling WebGL (which touches `window`) into the server prerender —
// only the dynamic, ssr:false renderer imports those.

export type StateNode = {
  id: string
  kind: 'resource'
  name: string
  type: string
  mode: string // 'managed' | 'data'
  module: string
  provider: string
  indeg: number
  // react-force-graph mutates positions/velocities onto nodes after layout
  x?: number
  y?: number
  z?: number
  vx?: number
  vy?: number
  vz?: number
}
export type StateEdge = { source: string | StateNode; target: string | StateNode; kind: string }
export type StateGraphVersion = {
  id: string
  serial: number
  created_at: string
  is_current: boolean
}
export type StateGraphData = {
  nodes: StateNode[]
  edges: StateEdge[]
  meta: {
    counts: Record<string, number>
    truncated: boolean
    total_resources: number
    max_nodes: number
    versions: StateGraphVersion[]
    state_version: StateGraphVersion | null
  }
}

export const PALETTE = ['#3b82f6','#22c55e','#f59e0b','#ec4899','#a855f7','#14b8a6','#ef4444','#84cc16','#06b6d4','#f97316','#8b5cf6','#eab308','#64748b']
export const EDGE_COLOR = '#64748b'

export const endId = (x: string | StateNode): string => (typeof x === 'string' ? x : x.id)

// Grouping axes for a state graph — derived from the DATA. Resource type is the
// most informative default (colour every aws_subnet the same), with module /
// provider / mode as alternatives.
export function groupAxes(nodes: StateNode[]): { value: string; label: string }[] {
  void nodes
  return [
    { value: 'type', label: 'Resource type' },
    { value: 'module', label: 'Module' },
    { value: 'provider', label: 'Provider' },
    { value: 'mode', label: 'Managed / data' },
    { value: 'none', label: 'Nothing (single color)' },
  ]
}

export function categoryOf(n: StateNode, groupBy: string): string {
  if (groupBy === 'none') return 'resource'
  if (groupBy === 'type') return n.type
  if (groupBy === 'module') return n.module || '(root)'
  if (groupBy === 'provider') return n.provider || '(none)'
  if (groupBy === 'mode') return n.mode
  return 'resource'
}
