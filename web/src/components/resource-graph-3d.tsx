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
  instances?: number // count/for_each instance count → drawn as a "nucleus" (#770)
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
  controls: () =>
    | {
        noRoll?: boolean
        staticMoving?: boolean
        target?: { set: (x: number, y: number, z: number) => void }
        update?: () => void
      }
    | undefined
}
type FgProps<T extends RGNode> = {
  ref?: React.Ref<FgMethods>
  width?: number
  height?: number
  backgroundColor?: string
  graphData: { nodes: T[]; links: RGLink<T>[] }
  controlType?: 'trackball' | 'orbit' | 'fly'
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
  showNavInfo?: boolean
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

// A count/for_each resource is drawn as a "nucleus" — a clump of small spheres,
// one pearl per instance (#770). NO cap on the instance count: every instance is
// packed into a ball, but we only instantiate meshes for the outer ~2 pearl
// layers and skip any pearl buried deeper than that — a buried pearl is fully
// occluded, so drawing it is wasted. Mesh count then scales with the clump's
// *surface* (~N^2/3), not its volume, so a count=500 resource stays cheap while
// still reading as a big ball. Positions use a deterministic sunflower/fibonacci
// ball packing (no Math.random) so the clump is stable across renders.
const NUCLEUS_LAYERS = 2 // draw the outer N pearl-layers; cull anything deeper
// Direction for pearl `i`, INDEPENDENT of its radius. The radius uses the
// cbrt(i/n) ordering (so the ball fills at uniform density); if the direction
// also keyed off `i` monotonically the fill would spiral north-pole-inner →
// south-pole-outer and leave the outer shell's top empty (a hole at the pole).
// The R2 low-discrepancy sequence (plastic-number constants) scrambles i into a
// well-spread (latitude, longitude), so every radius band — including the outer
// surviving shell — covers the whole sphere with no gap.
const R2_A1 = 0.7548776662466927 // 1/plastic
const R2_A2 = 0.5698402909980532 // 1/plastic²
function ballDir(i: number): [number, number, number] {
  const z = 1 - 2 * (((i + 0.5) * R2_A1) % 1) // equal-area latitude in (-1,1)
  const theta = 2 * Math.PI * (((i + 0.5) * R2_A2) % 1)
  const rho = Math.sqrt(Math.max(0, 1 - z * z))
  return [rho * Math.cos(theta), z, rho * Math.sin(theta)]
}
// Clump radius for a nucleus of `n` pearls of radius `rp`. The 0.95 factor packs
// the ball a touch tighter than kissing so surface pearls OVERLAP into a solid
// skin (no gaps between them); grows as cbrt(n) so density stays uniform as the
// ball fills. Shared by the builder and the label offset.
function nucleusRadius(n: number, rp: number): number {
  return rp * 0.95 * Math.cbrt(Math.max(1, n))
}
// How many of the `m` *visible* surface pearls each distinct colour gets. When
// nothing is culled (m ≥ n) every instance keeps its own colour. When the
// interior is culled, proportional colouring would make a minority action (a
// handful of destroys among hundreds of creates) ~1% of the surface — easily
// lost or occluded, which is exactly wrong for a blast-radius view where a
// destroy must jump out. So every present colour gets a VISIBILITY FLOOR (a
// small fraction of the surface, enough that several pearls face the camera from
// any angle); the majority absorbs the difference. Returns [{color, count}],
// counts summing to m. The exact instance count still rides on the node label
// and the legend — the nucleus conveys "which actions, roughly how mixed", not a
// faithful ratio.
function apportionColors(colors: string[], m: number): Array<{ color: string; count: number }> {
  const order: string[] = []
  const count = new Map<string, number>()
  for (const c of colors) {
    if (!count.has(c)) order.push(c)
    count.set(c, (count.get(c) ?? 0) + 1)
  }
  const n = colors.length
  if (m >= n) return order.map((c) => ({ color: c, count: count.get(c) as number }))
  const visFloor = Math.max(1, Math.ceil(m * 0.02)) // ≥2% of the surface per present action
  const alloc = order.map((c) => Math.max(visFloor, Math.floor(((count.get(c) as number) / n) * m)))
  let used = alloc.reduce((a, b) => a + b, 0)
  // Reconcile to exactly m by adjusting the current largest bucket (the majority
  // action), so the floored minorities keep their guaranteed presence.
  while (used > m) {
    let mi = 0
    for (let i = 1; i < alloc.length; i++) if (alloc[i] > alloc[mi]) mi = i
    if (alloc[mi] <= 1) break
    alloc[mi]--
    used--
  }
  while (used < m) {
    let mi = 0
    for (let i = 1; i < order.length; i++)
      if ((count.get(order[i]) as number) > (count.get(order[mi]) as number)) mi = i
    alloc[mi]++
    used++
  }
  return order.map((c, i) => ({ color: c, count: alloc[i] }))
}
function buildNucleus(colors: string[], baseSize: number): THREE.Group {
  const g = new THREE.Group()
  const n = colors.length
  const rp = Math.max(1.5, baseSize * 0.55) // pearl radius
  const rBall = nucleusRadius(n, rp)
  const cull = rBall - NUCLEUS_LAYERS * (2 * rp) // keep pearls within 2 diameters of the surface
  const geo = new THREE.SphereGeometry(rp, 10, 10)
  // Equal-volume shells: radius grows as cbrt(index/n), so the ball fills at
  // uniform density. Keep only the outer layers (occlusion cull). Sort the kept
  // pearls OUTERMOST-FIRST: only the outer ~1 layer is actually visible, so
  // colours must be assigned outer-first or a minority action lands on the
  // occluded inner edge of the shell and reads as absent (the "no red at all"
  // bug). Colour ≠ radius: index order here is discarded, only the sort matters.
  const drawn: number[] = []
  for (let i = 0; i < n; i++) {
    if (rBall * Math.cbrt((i + 0.5) / n) >= cull) drawn.push(i)
  }
  drawn.sort((a, b) => b - a) // larger index → larger radius → outermost first
  const m = drawn.length || 1

  // Build the surface-colour list: each colour's allocation placed at evenly
  // spaced slots (minorities first so they get clean spacing), so no colour
  // clumps and every action is spread around the visible surface — some pearls
  // of each always face the camera. slot 0 is the outermost pearl.
  const alloc = apportionColors(colors, m)
  const surface: string[] = new Array(m)
  for (const { color, count } of [...alloc].sort((x, y) => x.count - y.count)) {
    for (let j = 0; j < count; j++) {
      let pos = Math.round((j * m) / count) % m
      let guard = 0
      while (surface[pos] !== undefined && guard++ < m) pos = (pos + 1) % m
      surface[pos] = color
    }
  }
  const fallback = alloc[0]?.color ?? '#475569'

  drawn.forEach((i, k) => {
    const rad = rBall * Math.cbrt((i + 0.5) / n)
    const [dx, dy, dz] = ballDir(i)
    const mesh = new THREE.Mesh(geo, new THREE.MeshLambertMaterial({ color: surface[k] ?? fallback }))
    mesh.position.set(dx * rad, dy * rad, dz * rad)
    g.add(mesh)
  })
  return g
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
  nucleonColorsOf,
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
  // For a count/for_each resource (instances > 1): the per-pearl colours of its
  // nucleus. State passes the node colour × N (uniform); the impact graph passes
  // one colour per instance action. Omit / return ≤1 colour → a single sphere.
  nucleonColorsOf?: (n: T) => string[]
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
  // Once the user has moved the camera (clicked a resource to orbit it, dragged,
  // zoomed…), the auto-framing on engine-settle must STOP — otherwise the next
  // time the layout re-settles it yanks the camera back to the graph centroid,
  // undoing the user's framing (the "click → centres → jumps back" bug). Reset
  // when a new graph loads or on explicit Reset view.
  const userMoved = useRef(false)
  const [size, setSize] = useState({ w: 800, h: 560 })

  useEffect(() => {
    if (!wrapRef.current) return
    const el = wrapRef.current
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }))
    ro.observe(el)
    setSize({ w: el.clientWidth, h: el.clientHeight })
    // Any drag/zoom on the canvas also counts as taking camera control, so a
    // later re-settle won't re-frame over it (Reset view re-enables auto-frame).
    const onDown = () => {
      userMoved.current = true
    }
    el.addEventListener('pointerdown', onDown)
    el.addEventListener('wheel', onDown, { passive: true })
    return () => {
      ro.disconnect()
      el.removeEventListener('pointerdown', onDown)
      el.removeEventListener('wheel', onDown)
    }
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

  // A freshly-loaded graph should auto-frame again (until the user moves it).
  useEffect(() => {
    userMoved.current = false
  }, [data])

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
    userMoved.current = false // re-enable auto-framing (incl. on the next re-settle)
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
    // Keep the default TrackballControls (node-drag needs them — OrbitControls
    // crashes on drag-end in 3d-force-graph), but tame them into an orbit-like
    // feel: noRoll keeps the scene upright (no tumbling), staticMoving drops the
    // inertial drift so rotation stops when you do. Rotation orbits the controls'
    // target, and clicking a resource swings that target onto it (onNodeClick),
    // so you get controlled "rotate around the selected resource" AND node drag.
    const controls = fg.controls?.()
    if (controls) {
      controls.noRoll = true
      controls.staticMoving = true
    }
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

    // Centering gravity: pull every node toward the origin, proportional to its
    // distance. Without it a node (or whole component) with no links — a
    // disconnected resource, or a count/for_each block nothing depends on — is
    // pushed out by charge repulsion to the charge's distanceMax and never comes
    // back, leaving a huge empty gap and wrecking the auto-framing. This bounds
    // the layout so unlinked pieces settle near the rest instead of drifting off.
    // Weaker than the module-cluster pull (0.14) so distinct modules still spread.
    const gravity = (alpha: number) => {
      const k = 0.045 * alpha
      for (const n of nodes) {
        if (n.x == null) continue
        n.vx = (n.vx || 0) - n.x * k
        n.vy = (n.vy || 0) - (n.y || 0) * k
        n.vz = (n.vz || 0) - (n.z || 0) * k
      }
    }
    ;(gravity as unknown as { initialize: () => void }).initialize = () => {}
    fg.d3Force('gravity', gravity)
  }, [nodes])

  const linkHi = (l: RGLink<T>): boolean => {
    const s = endId(l.source)
    const t = endId(l.target)
    return (radius.has(s) || s === sel) && (radius.has(t) || t === sel)
  }

  // The per-pearl colours for a node's nucleus, or [] when it's an ordinary
  // single-instance node (draw the default sphere instead of a clump).
  const nucleons = (n: T): string[] => {
    if ((n.instances ?? 1) <= 1) return []
    const cols = nucleonColorsOf?.(n) ?? []
    return cols.length > 1 ? cols : []
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
          showNavInfo={false}
          cooldownTicks={160}
          onEngineStop={() => {
            // Only auto-frame while the user hasn't taken camera control — a
            // re-settle must not stomp their framing.
            if (!userMoved.current) frame()
            drawClusters()
          }}
          nodeVal={(n) => (nucleons(n).length > 1 ? 0.2 : nodeSize(n))}
          nodeColor={(n) => {
            if (!sel) return colorOf(n)
            if (n.id === sel) return '#ffffff'
            return radius.has(n.id) ? colorOf(n) : DIM_NODE
          }}
          nodeThreeObjectExtend
          nodeThreeObject={(n) => {
            const cols = nucleons(n)
            const isNucleus = cols.length > 1
            // A count/for_each resource is a "nucleus" clump; the default sphere
            // is shrunk to nothing (nodeVal above) and replaced by the pearls.
            // Otherwise `nodeThreeObjectExtend` keeps the default sphere and we
            // only add the module-stripped label sprite above it.
            const group = new THREE.Group()
            let yOffset = 6
            if (isNucleus) {
              group.add(buildNucleus(cols, nodeSize(n)))
              yOffset = Math.max(8, nucleusRadius(cols.length, Math.max(1.5, nodeSize(n) * 0.55)) + 3)
            }
            const label = new SpriteText(isNucleus ? `${localAddr(n.id)} ×${cols.length}` : localAddr(n.id))
            label.color = !sel ? '#e2e8f0' : n.id === sel ? '#fff' : radius.has(n.id) ? '#e2e8f0' : 'rgba(160,174,192,.7)'
            label.textHeight = 2.6
            label.backgroundColor = 'rgba(10,14,23,.66)'
            label.padding = 1.5
            const obj = label as unknown as {
              material: { depthTest: boolean }
              position: { set: (x: number, y: number, z: number) => void }
            }
            obj.material.depthTest = false
            obj.position.set(0, yOffset, 0)
            group.add(label)
            return group
          }}
          onNodeClick={(n) => {
            select(n.id)
            // Make the clicked resource the rotation pivot by setting the
            // controls TARGET directly (camera stays put) so dragging the
            // background orbits THIS node. NOT cameraPosition(): its getter
            // returns a cached position that doesn't track TrackballControls
            // rotation, so feeding it back snaps the camera to the default view.
            // A click ≠ a drag, so grabbing a node to reposition it still works.
            userMoved.current = true // stop auto-frame from stomping this
            const controls = fgRef.current?.controls?.()
            if (controls?.target && n.x != null && n.y != null && n.z != null) {
              controls.target.set(n.x, n.y, n.z)
              controls.update?.()
            }
          }}
          linkColor={(l) => (!sel ? 'rgba(148,163,184,.45)' : linkHi(l) ? 'rgba(226,232,240,.95)' : 'rgba(130,144,166,.4)')}
          linkWidth={(l) => (!sel ? 0.8 : linkHi(l) ? 1.8 : 0.4)}
          linkDirectionalArrowLength={3.2}
          linkDirectionalArrowRelPos={1}
          linkDirectionalArrowColor={(l) => (!sel ? 'rgba(148,163,184,.6)' : linkHi(l) ? 'rgba(226,232,240,.95)' : 'rgba(130,144,166,.4)')}
        />
      </div>

      {/* Our own nav hint (the built-in one is disabled): the click-a-resource-
          to-orbit-around-it behaviour is non-obvious, so it has to be spelled
          out here. */}
      <div className="absolute bottom-1.5 inset-x-0 text-center text-[10px] text-slate-500 pointer-events-none select-none">
        Drag to rotate · scroll to zoom · right-drag to pan ·{' '}
        <span className="text-slate-400">click a resource to orbit around it</span>
      </div>
    </div>
  )
}

export { DIM_NODE }
