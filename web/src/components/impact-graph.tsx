'use client'

// Impact graph (#761) — interactive plan dependency + blast-radius view.
// A thin wrapper over the shared <ResourceGraph3D> (#765): the impact graph and
// the state graph are the same graph, differing only in what colour encodes —
// here, the plan change action (create/update/delete). Client-only (WebGL): the
// run page imports this via next/dynamic { ssr: false }.
import { useEffect, useState } from 'react'
import { apiFetch } from '@/lib/api'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { ResourceGraph3D, type RGNode, type RGLink } from '@/components/resource-graph-3d'

type Action = 'create' | 'update' | 'replace' | 'delete' | 'noop'
interface GNode extends RGNode {
  type: string
  name: string
  provider: string
  action: Action // most-severe action across instances → node-level colour
  instance_actions: Action[] // per-instance action → one nucleus pearl each
}
interface Graph {
  nodes: GNode[]
  edges: RGLink<GNode>[]
  meta: { terraform_version?: string; counts: Record<Action, number> }
}

const COLOR: Record<Action, string> = {
  create: '#22c55e',
  update: '#f59e0b',
  replace: '#a855f7',
  delete: '#ef4444',
  noop: '#475569',
}
const LABEL: Record<Action, string> = {
  create: 'create',
  update: 'update',
  replace: 'replace',
  delete: 'destroy',
  noop: 'unchanged',
}
const ACT_ORDER: Record<Action, number> = { replace: 0, delete: 1, create: 2, update: 3, noop: 4 }

export function ImpactGraph({ runId }: { runId: string }) {
  const [graph, setGraph] = useState<Graph | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    apiFetch(`/api/terrapod/v1/runs/${runId}/impact-graph`)
      .then(async (res) => {
        if (!res.ok) throw new Error('Impact graph is not available for this run.')
        const body = await res.json()
        if (!cancelled) setGraph(body.data.attributes as Graph)
      })
      .catch((e: Error) => !cancelled && setError(e.message))
    return () => {
      cancelled = true
    }
  }, [runId])

  if (error) return <ErrorBanner message={error} />
  if (!graph) return <LoadingSpinner />

  const counts = graph.meta.counts
  const legend = (['replace', 'delete', 'create', 'update', 'noop'] as Action[])
    .filter((a) => counts[a] > 0)
    .map((a) => (
      <span
        key={a}
        className="flex items-center gap-1.5 text-[11px] font-semibold px-2 py-1 rounded-full bg-slate-700/25"
      >
        <span className="w-2 h-2 rounded-full" style={{ background: COLOR[a] }} />
        {counts[a]} {LABEL[a]}
      </span>
    ))

  return (
    <ResourceGraph3D<GNode>
      nodes={graph.nodes}
      edges={graph.edges}
      title="Impact graph"
      subtitle={`plan of ${graph.nodes.length} resources${
        graph.meta.terraform_version ? ` · ${graph.meta.terraform_version}` : ''
      }`}
      legend={legend}
      hint={
        <>
          Each sphere is a resource; each arrow points to what it depends on. Click a node → its
          transitive <b>impact</b> (everything downstream) lights up.
        </>
      }
      colorOf={(n) => COLOR[n.action]}
      // A count/for_each resource becomes a nucleus: one pearl per instance,
      // each coloured by its OWN planned action (a single count can be
      // [0] destroy / [1] create / [2] update all at once).
      nucleonColorsOf={(n) => n.instance_actions.map((a) => COLOR[a])}
      nodeSize={(n) => (n.action === 'noop' ? 1.4 : 3)}
      sortNodes={(a, b) => ACT_ORDER[a.action] - ACT_ORDER[b.action] || a.id.localeCompare(b.id)}
      renderDetail={(n, downstream) => (
        <>
          <span
            className="inline-block text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded mb-1.5"
            style={{ background: COLOR[n.action] + '33', color: COLOR[n.action] }}
          >
            {LABEL[n.action]}
          </span>
          <div className="font-mono text-xs text-slate-100 break-all">{n.id}</div>
          <div className="text-2xl font-bold mt-1.5">
            {downstream} <span className="text-xs font-medium text-slate-400">downstream affected</span>
          </div>
        </>
      )}
    />
  )
}
