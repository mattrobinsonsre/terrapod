'use client'

// Impact graph (#761) — interactive plan dependency + blast-radius view.
// Client-only (WebGL): the run page imports this via next/dynamic { ssr: false },
// so three / three-spritetext / react-force-graph-3d never touch SSR.
import { useEffect, useMemo, useRef, useState, type ReactElement } from 'react'
import ForceGraph3D from 'react-force-graph-3d'
import SpriteText from 'three-spritetext'
import * as THREE from 'three'
import { apiFetch } from '@/lib/api'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'

type Action = 'create' | 'update' | 'replace' | 'delete' | 'noop'
interface GNode {
  id: string
  type: string
  name: string
  provider: string
  action: Action
  key: string | null
  module: string
  // react-force-graph mutates x/y/z (+ velocities) onto nodes after layout
  x?: number
  y?: number
  z?: number
  vx?: number
  vy?: number
  vz?: number
}
// After the force sim resolves, link endpoints are node objects; before, ids.
interface GLink {
  source: string | GNode
  target: string | GNode
}
interface Graph {
  nodes: GNode[]
  edges: GLink[]
  meta: { terraform_version?: string; counts: Record<Action, number> }
}

// The bits of the react-force-graph imperative API we use. Its published types
// don't survive next/dynamic, so we type the boundary ourselves.
interface D3Force {
  strength?: (n: number) => D3Force
  distance?: (n: number) => D3Force
  distanceMax?: (n: number) => D3Force
}
interface Vec3 {
  x: number
  y: number
  z: number
}
interface FgMethods {
  zoomToFit: (ms?: number, px?: number) => void
  cameraPosition: (pos?: Vec3, lookAt?: Vec3, ms?: number) => Vec3
  d3Force: (name: string, force?: unknown) => D3Force | undefined
  scene: () => THREE.Scene
}
type FgProps = {
  ref?: React.Ref<FgMethods>
  width?: number
  height?: number
  backgroundColor?: string
  graphData: { nodes: GNode[]; links: GLink[] }
  cooldownTicks?: number
  onEngineStop?: () => void
  nodeVal?: (n: GNode) => number
  nodeColor?: (n: GNode) => string
  nodeThreeObjectExtend?: boolean
  nodeThreeObject?: (n: GNode) => object
  onNodeClick?: (n: GNode) => void
  linkColor?: (l: GLink) => string
  linkWidth?: (l: GLink) => number
  linkDirectionalArrowLength?: number
  linkDirectionalArrowRelPos?: number
  linkDirectionalArrowColor?: (l: GLink) => string
}
const FG3D = ForceGraph3D as unknown as (props: FgProps) => ReactElement

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
const DIM_NODE = 'rgba(130,144,166,.55)'
const ACT_ORDER: Record<Action, number> = { replace: 0, delete: 1, create: 2, update: 3, noop: 4 }

function localAddr(id: string): string {
  // The resource's full local address with the module path stripped —
  // `module.vpc.aws_subnet.this[0]` → `aws_subnet.this[0]`. Module membership
  // is conveyed by the cluster box + its label, so it's redundant on the node.
  return id.replace(/^(module\.[^.]+\.)+/, '')
}
function endId(x: string | GNode): string {
  return typeof x === 'string' ? x : x.id
}

export function ImpactGraph({ runId }: { runId: string }) {
  const [graph, setGraph] = useState<Graph | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [sel, setSel] = useState<string | null>(null)
  const [radius, setRadius] = useState<Set<string>>(new Set())
  const [filter, setFilter] = useState('')

  const fgRef = useRef<FgMethods | null>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const clusterObjs = useRef<THREE.Object3D[]>([])
  const [size, setSize] = useState({ w: 800, h: 560 })

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

  useEffect(() => {
    if (!wrapRef.current) return
    const el = wrapRef.current
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }))
    ro.observe(el)
    setSize({ w: el.clientWidth, h: el.clientHeight })
    return () => ro.disconnect()
  }, [graph])

  // reverse adjacency: dependents[X] = nodes that depend on X.
  const { data, dependents, byId, sorted } = useMemo(() => {
    const dependents: Record<string, string[]> = {}
    const byId: Record<string, GNode> = {}
    if (!graph) {
      return { data: { nodes: [] as GNode[], links: [] as GLink[] }, dependents, byId, sorted: [] as GNode[] }
    }
    for (const n of graph.nodes) {
      dependents[n.id] = []
      byId[n.id] = n
    }
    for (const e of graph.edges) (dependents[endId(e.target)] ||= []).push(endId(e.source))
    const sorted = [...graph.nodes].sort(
      (a, b) => ACT_ORDER[a.action] - ACT_ORDER[b.action] || a.id.localeCompare(b.id),
    )
    return { data: { nodes: graph.nodes, links: graph.edges.map((e) => ({ ...e })) }, dependents, byId, sorted }
  }, [graph])

  function blastFrom(id: string): Set<string> {
    const seen = new Set<string>()
    const stack = [id]
    while (stack.length) {
      const cur = stack.pop() as string
      for (const dep of dependents[cur] || []) {
        if (!seen.has(dep)) {
          seen.add(dep)
          stack.push(dep)
        }
      }
    }
    return seen
  }
  function select(id: string) {
    setSel(id)
    setRadius(blastFrom(id))
  }
  function clear() {
    setSel(null)
    setRadius(new Set())
  }
  // Frame the whole graph from its own centroid — pull the camera back to a
  // distance proportional to the node cloud's bounding-sphere radius, along the
  // current view direction. Anchoring to the origin (as zoomToFit-plus-clamp
  // did) over-zooms whenever the layout centroid drifts away from (0,0,0).
  function frame() {
    const fg = fgRef.current
    if (!fg || !graph) return
    const box = new THREE.Box3()
    for (const n of graph.nodes) {
      if (n.x != null && n.y != null && n.z != null) {
        box.expandByPoint(new THREE.Vector3(n.x, n.y, n.z))
      }
    }
    if (box.isEmpty()) return
    const c = box.getCenter(new THREE.Vector3())
    const radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 1)
    // Fill the viewport comfortably: ~1.9× the bounding-sphere radius frames the
    // node cloud with a little margin. The floor keeps a tiny plan (a handful of
    // nodes) from zooming uncomfortably close.
    const dist = Math.max(radius * 1.9, 150)
    const cur = fg.cameraPosition()
    const dir = new THREE.Vector3(cur.x - c.x, cur.y - c.y, cur.z - c.z)
    if (dir.length() < 1) dir.set(0, 0, 1)
    dir.normalize().multiplyScalar(dist)
    fg.cameraPosition({ x: c.x + dir.x, y: c.y + dir.y, z: c.z + dir.z }, c, 400)
  }
  function resetView() {
    frame()
    const ca = sorted.find((n) => n.action === 'replace') || sorted[0]
    if (ca) select(ca.id)
  }

  // Draw a translucent wireframe box + label around each module's node cluster,
  // so large multi-module plans read as grouped regions. Rebuilt every settle
  // (node positions have moved); root-module resources (module === '') get no box.
  function drawClusters() {
    const fg = fgRef.current
    if (!fg || !graph) return
    const scene = fg.scene()
    for (const o of clusterObjs.current) scene.remove(o)
    clusterObjs.current = []

    const byModule = new Map<string, GNode[]>()
    for (const n of graph.nodes) {
      if (!n.module || n.x == null) continue
      ;(byModule.get(n.module) ?? byModule.set(n.module, []).get(n.module)!).push(n)
    }
    for (const [mod, ns] of byModule) {
      if (ns.length < 2) continue
      const box = new THREE.Box3()
      for (const n of ns) box.expandByPoint(new THREE.Vector3(n.x!, n.y!, n.z!))
      box.expandByScalar(9)
      const helper = new THREE.Box3Helper(box, new THREE.Color('#64748b'))
      const hm = helper.material as THREE.Material
      hm.transparent = true
      hm.opacity = 0.35
      hm.depthWrite = false
      scene.add(helper)
      clusterObjs.current.push(helper)

      const label = new SpriteText(`module.${mod}`)
      label.color = '#94a3b8'
      label.textHeight = 4
      label.backgroundColor = 'rgba(10,14,23,.7)'
      label.padding = 2
      const lm = label as unknown as {
        material: { depthTest: boolean; depthWrite: boolean }
        position: { set: (x: number, y: number, z: number) => void }
      }
      lm.material.depthTest = false
      lm.material.depthWrite = false
      const c = box.getCenter(new THREE.Vector3())
      label.position.set(c.x, box.max.y + 6, c.z)
      scene.add(label)
      clusterObjs.current.push(label)
    }
  }

  useEffect(() => {
    const fg = fgRef.current
    if (!fg || !graph) return
    fg.d3Force('charge')?.strength?.(-140)?.distanceMax?.(220)
    fg.d3Force('link')?.distance?.(26)

    // Custom force: pull same-module nodes toward their module's centroid so
    // modules settle into visually distinct clusters. Reads x/vx only (no octree
    // dependency), so it's compatible with the bundled sim's force contract.
    const nodes = graph.nodes
    const cluster = (alpha: number) => {
      const cx: Record<string, number> = {}
      const cy: Record<string, number> = {}
      const cz: Record<string, number> = {}
      const cn: Record<string, number> = {}
      for (const n of nodes) {
        if (!n.module || n.x == null) continue
        cx[n.module] = (cx[n.module] || 0) + n.x
        cy[n.module] = (cy[n.module] || 0) + (n.y || 0)
        cz[n.module] = (cz[n.module] || 0) + (n.z || 0)
        cn[n.module] = (cn[n.module] || 0) + 1
      }
      const k = 0.14 * alpha
      for (const n of nodes) {
        if (!n.module || n.x == null || cn[n.module] < 2) continue
        n.vx = (n.vx || 0) + (cx[n.module] / cn[n.module] - n.x) * k
        n.vy = (n.vy || 0) + (cy[n.module] / cn[n.module] - (n.y || 0)) * k
        n.vz = (n.vz || 0) + (cz[n.module] / cn[n.module] - (n.z || 0)) * k
      }
    }
    ;(cluster as unknown as { initialize: (n: GNode[]) => void }).initialize = () => {}
    fg.d3Force('cluster', cluster)
  }, [graph])

  if (error) return <ErrorBanner message={error} />
  if (!graph) return <LoadingSpinner />

  const counts = graph.meta.counts
  const linkHi = (l: GLink): boolean => {
    const s = endId(l.source)
    const t = endId(l.target)
    return (radius.has(s) || s === sel) && (radius.has(t) || t === sel)
  }

  return (
    <div className="relative w-full h-[70vh] min-h-[420px] rounded-xl overflow-hidden border border-slate-800 bg-[#0a0e17]">
      {/* HUD */}
      <div className="absolute z-10 top-3 left-3 max-w-[min(320px,52vw)] sm:max-w-[320px] rounded-xl border border-slate-700/40 bg-slate-900/80 backdrop-blur px-4 py-3">
        <div className="text-sm font-semibold">Impact graph</div>
        <div className="text-[11px] text-slate-400 mb-2">
          plan of {graph.nodes.length} resources
          {graph.meta.terraform_version ? ` · ${graph.meta.terraform_version}` : ''}
        </div>
        <div className="flex flex-wrap gap-1.5 mb-2">
          {(['replace', 'delete', 'create', 'update', 'noop'] as Action[])
            .filter((a) => counts[a] > 0)
            .map((a) => (
              <span
                key={a}
                className="flex items-center gap-1.5 text-[11px] font-semibold px-2 py-1 rounded-full bg-slate-700/25"
              >
                <span className="w-2 h-2 rounded-full" style={{ background: COLOR[a] }} />
                {counts[a]} {LABEL[a]}
              </span>
            ))}
        </div>
        <p className="text-[11px] text-slate-400 leading-relaxed">
          Each sphere is a resource; each arrow points to what it depends on. Click a node → its
          transitive <b>impact</b> (everything downstream) lights up.
        </p>
        <div className="flex gap-2 mt-2.5">
          <button onClick={resetView} className="text-[11px] font-medium px-2.5 py-1.5 rounded-lg bg-slate-700 hover:bg-slate-600">
            Reset view
          </button>
          <button onClick={clear} className="text-[11px] font-medium px-2.5 py-1.5 rounded-lg bg-slate-700 hover:bg-slate-600">
            Clear
          </button>
        </div>
      </div>

      {/* Resource list. Narrows on small screens so it and the HUD coexist
          without pushing the page into horizontal scroll; desktop width unchanged. */}
      <div className="absolute z-10 top-3 right-3 w-[min(300px,44vw)] sm:w-[300px] max-h-[calc(70vh-24px)] flex flex-col gap-2 rounded-xl border border-slate-700/40 bg-slate-900/80 backdrop-blur p-3">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter resources…"
          className="text-xs bg-slate-700/20 border border-slate-600/40 rounded-lg px-2.5 py-1.5 outline-none focus:border-brand-500"
        />
        <div className="overflow-y-auto flex flex-col gap-px pr-0.5">
          {sorted
            .filter((n) => !filter || n.id.toLowerCase().includes(filter.toLowerCase()))
            .map((n) => (
              <button
                key={n.id}
                onClick={() => select(n.id)}
                title={`${n.id} · ${LABEL[n.action]}`}
                className={`flex items-center gap-2 px-2 py-1.5 rounded-lg text-left ${
                  n.id === sel ? 'bg-brand-500/25 outline outline-1 outline-brand-500/50' : 'hover:bg-slate-700/30'
                }`}
              >
                <span className="w-2 h-2 rounded-full shrink-0" style={{ background: COLOR[n.action] }} />
                <span className="text-[11.5px] font-mono truncate">{n.id}</span>
              </button>
            ))}
        </div>
      </div>

      {/* Selection detail */}
      {sel && byId[sel] && (
        <div className="absolute z-10 bottom-3 left-3 max-w-[min(380px,52vw)] sm:max-w-[380px] rounded-xl border border-slate-700/40 bg-slate-900/80 backdrop-blur px-4 py-3">
          <span
            className="inline-block text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded mb-1.5"
            style={{ background: COLOR[byId[sel].action] + '33', color: COLOR[byId[sel].action] }}
          >
            {LABEL[byId[sel].action]}
          </span>
          <div className="font-mono text-xs text-slate-100 break-all">{sel}</div>
          <div className="text-2xl font-bold mt-1.5">
            {radius.size} <span className="text-xs font-medium text-slate-400">downstream affected</span>
          </div>
        </div>
      )}

      <div ref={wrapRef} className="absolute inset-0">
        <FG3D
          ref={fgRef}
          width={size.w}
          height={size.h}
          backgroundColor="#0a0e17"
          graphData={data}
          cooldownTicks={160}
          onEngineStop={() => {
            frame()
            drawClusters()
          }}
          nodeVal={(n) => (n.action === 'noop' ? 1.4 : 3)}
          nodeColor={(n) => {
            if (!sel) return COLOR[n.action]
            if (n.id === sel) return '#ffffff'
            if (radius.has(n.id)) return COLOR[n.action]
            return DIM_NODE
          }}
          nodeThreeObjectExtend
          nodeThreeObject={(n) => {
            const s = new SpriteText(localAddr(n.id))
            s.color = !sel
              ? n.action === 'noop'
                ? '#64748b'
                : '#e2e8f0'
              : n.id === sel
                ? '#fff'
                : radius.has(n.id)
                  ? '#e2e8f0'
                  : 'rgba(160,174,192,.7)'
            s.textHeight = n.action === 'noop' ? 2 : 2.6
            s.backgroundColor = 'rgba(10,14,23,.66)'
            s.padding = 1.5
            const obj = s as unknown as {
              material: { depthTest: boolean }
              position: { set: (x: number, y: number, z: number) => void }
            }
            obj.material.depthTest = false
            obj.position.set(0, 6, 0)
            return s
          }}
          onNodeClick={(n) => select(n.id)}
          linkColor={(l) => (!sel ? 'rgba(148,163,184,.45)' : linkHi(l) ? 'rgba(226,232,240,.95)' : 'rgba(130,144,166,.4)')}
          linkWidth={(l) => (!sel ? 0.8 : linkHi(l) ? 1.8 : 0.4)}
          linkDirectionalArrowLength={3.2}
          linkDirectionalArrowRelPos={1}
          linkDirectionalArrowColor={(l) => (!sel ? 'rgba(148,163,184,.6)' : linkHi(l) ? 'rgba(226,232,240,.95)' : 'rgba(130,144,166,.4)')}
        />
      </div>
    </div>
  )
}
