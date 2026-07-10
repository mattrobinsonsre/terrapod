'use client'

// Shared 3D resource-graph renderer (#765) — the common engine behind both the
// plan/impact graph (#761) and the single-workspace state graph (#765). Both
// are the *same* graph: resources as nodes, dependency arrows, clustered into
// translucent per-module boxes, with click-to-highlight transitive impact.
//
// The ONLY things a consumer varies are cosmetic and passed as props:
//   - `colorOf(node)`  — the impact graph spends colour on the change action
//     (create/update/delete); the state graph is free to colour by resource
//     type / module / provider / mode, since it has no "what's happening" axis.
//   - the HUD title/subtitle/legend/hint, the list sort, and the selection
//     detail block.
//
// Everything structural — the module-cluster force, the wireframe boxes,
// module-stripped node labels, centroid framing, the resource list + filter,
// and the transitive blast-radius on selection — is shared here so the two
// surfaces can never drift apart. Client-only (WebGL): consumers import this via
// next/dynamic { ssr: false } so three / three-spritetext / react-force-graph-3d
// never touch SSR.
import { useEffect, useMemo, useRef, useState, type ReactElement, type ReactNode } from 'react'
import ForceGraph3D from 'react-force-graph-3d'
import SpriteText from 'three-spritetext'
import * as THREE from 'three'

// A node needs an id + module membership; consumers extend with their own
// metadata (action, type, mode…) which their colorOf/nodeShape closures read.
export interface RGNode {
  id: string
  module: string // '' for root-module resources
  // react-force-graph mutates x/y/z (+ velocities) onto nodes after layout
  x?: number
  y?: number
  z?: number
  vx?: number
  vy?: number
  vz?: number
}
export interface RGLink<T extends RGNode> {
  source: string | T
  target: string | T
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
  cameraPosition: (pos?: Vec3, lookAt?: Vec3, ms?: number) => Vec3
  d3Force: (name: string, force?: unknown) => D3Force | undefined
  scene: () => THREE.Scene
}
type FgProps<T extends RGNode> = {
  ref?: React.Ref<FgMethods>
  width?: number
  height?: number
  backgroundColor?: string
  graphData: { nodes: T[]; links: RGLink<T>[] }
  cooldownTicks?: number
  onEngineStop?: () => void
  nodeVal?: (n: T) => number
  nodeColor?: (n: T) => string
  nodeThreeObjectExtend?: boolean
  nodeThreeObject?: (n: T) => object
  onNodeClick?: (n: T) => void
  linkColor?: (l: RGLink<T>) => string
  linkWidth?: (l: RGLink<T>) => number
  linkDirectionalArrowLength?: number
  linkDirectionalArrowRelPos?: number
  linkDirectionalArrowColor?: (l: RGLink<T>) => string
}
const FG3D = ForceGraph3D as unknown as <T extends RGNode>(props: FgProps<T>) => ReactElement

const DIM_NODE = 'rgba(130,144,166,.55)'

export function localAddr(id: string): string {
  // Full local address with the module path stripped —
  // `module.vpc.aws_subnet.this[0]` → `aws_subnet.this[0]`. Module membership
  // is conveyed by the cluster box + its label, so it's redundant on the node.
  return id.replace(/^(module\.[^.]+\.)+/, '')
}
function endId<T extends RGNode>(x: string | T): string {
  return typeof x === 'string' ? x : x.id
}

function ToolBtn({ onClick, active, children }: { onClick: () => void; active?: boolean; children: ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={`text-[11px] font-medium px-2.5 py-1.5 rounded-lg backdrop-blur ${
        active
          ? 'bg-brand-500/30 text-brand-200 outline outline-1 outline-brand-500/50'
          : 'bg-slate-800/80 text-slate-200 hover:bg-slate-700/90'
      }`}
    >
      {children}
    </button>
  )
}

export function ResourceGraph3D<T extends RGNode>({
  nodes,
  edges,
  title,
  subtitle,
  legend,
  hint,
  colorOf,
  nodeSize,
  sortNodes,
  renderDetail,
}: {
  nodes: T[]
  edges: RGLink<T>[]
  title: string
  subtitle: string
  legend: ReactNode
  hint: ReactNode
  colorOf: (n: T) => string
  nodeSize: (n: T) => number
  sortNodes?: (a: T, b: T) => number
  // The consumer's selection-detail block: gets the node + its transitive
  // downstream (blast-radius) size.
  renderDetail: (n: T, downstream: number) => ReactNode
}) {
  const [sel, setSel] = useState<string | null>(null)
  const [radius, setRadius] = useState<Set<string>>(new Set())
  const [filter, setFilter] = useState('')
  // The graph keeps the canvas: only a small always-on toolbar sits over it
  // (Reset view / Clear / Key / Resources). The key (legend) and resource list
  // are opt-in overlays — hidden by default, each dismissable — so they never
  // crowd the graph, on desktop or mobile.
  const [keyOpen, setKeyOpen] = useState(false)
  const [listOpen, setListOpen] = useState(false)

  const fgRef = useRef<FgMethods | null>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const clusterObjs = useRef<THREE.Object3D[]>([])
  const [size, setSize] = useState({ w: 800, h: 560 })

  useEffect(() => {
    if (!wrapRef.current) return
    const el = wrapRef.current
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }))
    ro.observe(el)
    setSize({ w: el.clientWidth, h: el.clientHeight })
    return () => ro.disconnect()
  }, [nodes])

  // reverse adjacency: dependents[X] = nodes that depend on X.
  const { data, dependents, byId, sorted } = useMemo(() => {
    const dependents: Record<string, string[]> = {}
    const byId: Record<string, T> = {}
    for (const n of nodes) {
      dependents[n.id] = []
      byId[n.id] = n
    }
    for (const e of edges) (dependents[endId(e.target)] ||= []).push(endId(e.source))
    const sorted = [...nodes].sort(sortNodes ?? ((a, b) => a.id.localeCompare(b.id)))
    return { data: { nodes, links: edges.map((e) => ({ ...e })) }, dependents, byId, sorted }
  }, [nodes, edges, sortNodes])

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
  // distance proportional to the node cloud's bounding-sphere radius. Anchoring
  // to the origin over-zooms whenever the layout centroid drifts from (0,0,0).
  function frame() {
    const fg = fgRef.current
    if (!fg) return
    const box = new THREE.Box3()
    for (const n of nodes) {
      if (n.x != null && n.y != null && n.z != null) {
        box.expandByPoint(new THREE.Vector3(n.x, n.y, n.z))
      }
    }
    if (box.isEmpty()) return
    const c = box.getCenter(new THREE.Vector3())
    const r = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 1)
    const dist = Math.max(r * 1.9, 150)
    const cur = fg.cameraPosition()
    const dir = new THREE.Vector3(cur.x - c.x, cur.y - c.y, cur.z - c.z)
    if (dir.length() < 1) dir.set(0, 0, 1)
    dir.normalize().multiplyScalar(dist)
    fg.cameraPosition({ x: c.x + dir.x, y: c.y + dir.y, z: c.z + dir.z }, c, 400)
  }
  function resetView() {
    frame()
    clear()
  }

  // Draw a translucent wireframe box + label around each module's node cluster.
  // Rebuilt every settle (positions moved); root-module nodes get no box.
  function drawClusters() {
    const fg = fgRef.current
    if (!fg) return
    const scene = fg.scene()
    for (const o of clusterObjs.current) scene.remove(o)
    clusterObjs.current = []

    const byModule = new Map<string, T[]>()
    for (const n of nodes) {
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

      const label = new SpriteText(mod.startsWith('module.') ? mod : `module.${mod}`)
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
    if (!fg) return
    fg.d3Force('charge')?.strength?.(-140)?.distanceMax?.(220)
    fg.d3Force('link')?.distance?.(26)

    // Custom force: pull same-module nodes toward their module's centroid so
    // modules settle into visually distinct clusters. Reads x/vx only, so it's
    // compatible with the bundled sim's force contract.
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
    ;(cluster as unknown as { initialize: () => void }).initialize = () => {}
    fg.d3Force('cluster', cluster)
  }, [nodes])

  const linkHi = (l: RGLink<T>): boolean => {
    const s = endId(l.source)
    const t = endId(l.target)
    return (radius.has(s) || s === sel) && (radius.has(t) || t === sel)
  }

  return (
    <div className="relative w-full h-[70vh] min-h-[420px] rounded-xl overflow-hidden border border-slate-800 bg-[#0a0e17]">
      {/* Always-on toolbar — small, so the graph keeps the canvas. Key + Resources
          toggle their (dismissable) overlay panels; Reset view + Clear are direct. */}
      <div className="absolute z-20 top-3 left-3 flex flex-wrap gap-1.5">
        <ToolBtn onClick={resetView}>Reset view</ToolBtn>
        <ToolBtn onClick={clear}>Clear</ToolBtn>
        <ToolBtn onClick={() => setKeyOpen((v) => !v)} active={keyOpen}>Key</ToolBtn>
        <ToolBtn onClick={() => setListOpen((v) => !v)} active={listOpen}>Resources</ToolBtn>
      </div>

      {/* Key panel (legend + title + hint) — opt-in, dismissable */}
      {keyOpen && (
        <div className="absolute z-10 top-14 left-3 max-w-[min(320px,80vw)] rounded-xl border border-slate-700/40 bg-slate-900/85 backdrop-blur px-4 py-3">
          <div className="flex items-start justify-between gap-2">
            <div>
              <div className="text-sm font-semibold">{title}</div>
              <div className="text-[11px] text-slate-400">{subtitle}</div>
            </div>
            <button
              onClick={() => setKeyOpen(false)}
              aria-label="Hide key"
              className="text-slate-400 hover:text-slate-100 text-lg leading-none -mt-0.5 px-1"
            >
              ×
            </button>
          </div>
          <div className="flex flex-wrap gap-1.5 my-2">{legend}</div>
          <p className="text-[11px] text-slate-400 leading-relaxed">{hint}</p>
        </div>
      )}

      {/* Resource list — opt-in, dismissable */}
      {listOpen && (
        <div className="absolute z-10 top-14 right-3 w-[min(300px,80vw)] max-h-[calc(70vh-64px)] flex flex-col gap-2 rounded-xl border border-slate-700/40 bg-slate-900/85 backdrop-blur p-3">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-semibold text-slate-200">Resources ({nodes.length})</span>
            <button
              onClick={() => setListOpen(false)}
              aria-label="Hide resources"
              className="text-slate-400 hover:text-slate-100 text-lg leading-none px-1"
            >
              ×
            </button>
          </div>
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
                  title={n.id}
                  className={`flex items-center gap-2 px-2 py-1.5 rounded-lg text-left ${
                    n.id === sel ? 'bg-brand-500/25 outline outline-1 outline-brand-500/50' : 'hover:bg-slate-700/30'
                  }`}
                >
                  <span className="w-2 h-2 rounded-full shrink-0" style={{ background: colorOf(n) }} />
                  <span className="text-[11.5px] font-mono truncate">{n.id}</span>
                </button>
              ))}
          </div>
        </div>
      )}

      {/* Selection detail */}
      {sel && byId[sel] && (
        <div className="absolute z-10 bottom-3 left-3 max-w-[min(380px,52vw)] sm:max-w-[380px] rounded-xl border border-slate-700/40 bg-slate-900/80 backdrop-blur px-4 py-3">
          {renderDetail(byId[sel], radius.size)}
        </div>
      )}

      <div ref={wrapRef} className="absolute inset-0">
        <FG3D<T>
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
          nodeVal={(n) => nodeSize(n)}
          nodeColor={(n) => {
            if (!sel) return colorOf(n)
            if (n.id === sel) return '#ffffff'
            return radius.has(n.id) ? colorOf(n) : DIM_NODE
          }}
          nodeThreeObjectExtend
          nodeThreeObject={(n) => {
            // `nodeThreeObjectExtend` keeps the default sphere; we only add the
            // module-stripped label sprite above it.
            const label = new SpriteText(localAddr(n.id))
            label.color = !sel ? '#e2e8f0' : n.id === sel ? '#fff' : radius.has(n.id) ? '#e2e8f0' : 'rgba(160,174,192,.7)'
            label.textHeight = 2.6
            label.backgroundColor = 'rgba(10,14,23,.66)'
            label.padding = 1.5
            const obj = label as unknown as {
              material: { depthTest: boolean }
              position: { set: (x: number, y: number, z: number) => void }
            }
            obj.material.depthTest = false
            obj.position.set(0, 6, 0)
            return label
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

export { DIM_NODE }
