'use client'

// Estate topology 3D renderer (#763). Client-only (WebGL): the estate page
// imports this via next/dynamic { ssr: false }. Pure renderer — the page owns
// the fetch and passes the graph in, so the same data also drives the
// accessible table fallback.
import { useEffect, useMemo, useRef, useState, type ReactElement } from 'react'
import { useTranslations } from 'next-intl'
import ForceGraph3D from 'react-force-graph-3d'
import SpriteText from 'three-spritetext'
import * as THREE from 'three'
import {
  categoryOf,
  endId,
  MODULE_COLOR,
  PALETTE,
  EDGE_COLOR as EDGE,
  type EstateEdge,
  type EstateGraphData,
  type EstateNode as BaseNode,
} from '@/lib/estate-graph'

// The renderer stashes mesh/sprite refs on nodes for live recolouring.
type EstateNode = BaseNode & { __mesh?: THREE.Mesh; __sprite?: SpriteText }

// Minimal imperative API surface we use (published types don't survive dynamic).
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
  controls: () =>
    | {
        noRoll?: boolean
        staticMoving?: boolean
        target?: { set: (x: number, y: number, z: number) => void }
        update?: () => void
      }
    | undefined
}
type FgProps = {
  ref?: React.Ref<FgMethods>
  width?: number
  height?: number
  backgroundColor?: string
  graphData: { nodes: EstateNode[]; links: EstateEdge[] }
  showNavInfo?: boolean
  cooldownTicks?: number
  onEngineStop?: () => void
  nodeThreeObject?: (n: EstateNode) => object
  onNodeClick?: (n: EstateNode) => void
  linkColor?: (l: EstateEdge) => string
  linkWidth?: (l: EstateEdge) => number
  linkDirectionalArrowLength?: number
  linkDirectionalArrowRelPos?: number
  linkDirectionalArrowColor?: (l: EstateEdge) => string
}
const FG3D = ForceGraph3D as unknown as (props: FgProps) => ReactElement

export function EstateGraph3D({
  graph,
  groupBy,
  onSelect,
  selectedId,
}: {
  graph: EstateGraphData
  groupBy: string
  onSelect: (n: EstateNode | null) => void
  selectedId: string | null
}) {
  const t = useTranslations('graphs')
  const fgRef = useRef<FgMethods | null>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  // Once the user takes camera control (click-to-orbit, drag, zoom), stop
  // auto-framing so a re-settle doesn't snap the view back.
  const userMoved = useRef(false)
  const [size, setSize] = useState({ w: 800, h: 600 })

  const data = useMemo(
    () => ({ nodes: graph.nodes, links: graph.edges.map((e) => ({ ...e })) }),
    [graph],
  )
  const adj = useMemo(() => {
    const a: Record<string, Set<string>> = {}
    graph.nodes.forEach((n) => (a[n.id] = new Set()))
    graph.edges.forEach((e) => {
      a[endId(e.source)]?.add(endId(e.target))
      a[endId(e.target)]?.add(endId(e.source))
    })
    return a
  }, [graph])

  const colorMap = useMemo(() => {
    const ws = graph.nodes.filter((n) => n.kind === 'workspace')
    const cats = [...new Set(ws.map((n) => categoryOf(n, groupBy)))].sort()
    const m: Record<string, string> = {}
    cats.forEach((c, i) => (m[c] = PALETTE[i % PALETTE.length]))
    return m
  }, [graph, groupBy])

  useEffect(() => {
    if (!wrapRef.current) return
    userMoved.current = false // a freshly-loaded graph auto-frames again
    const el = wrapRef.current
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }))
    ro.observe(el)
    setSize({ w: el.clientWidth, h: el.clientHeight })
    // CAPTURE phase: react-force-graph's controls call stopPropagation on the
    // canvas's pointerdown, so a bubble-phase listener never fires. Capture fires
    // on the way DOWN (ancestor before target), guaranteeing userMoved is set
    // BEFORE the click reheats the sim → the ensuing onEngineStop won't re-frame
    // (the "click resets to default view" bug). Same for wheel-zoom.
    const onDown = () => {
      userMoved.current = true
    }
    el.addEventListener('pointerdown', onDown, { capture: true })
    el.addEventListener('wheel', onDown, { capture: true, passive: true })
    return () => {
      ro.disconnect()
      el.removeEventListener('pointerdown', onDown, { capture: true } as EventListenerOptions)
      el.removeEventListener('wheel', onDown, { capture: true } as EventListenerOptions)
    }
  }, [graph])

  const near = selectedId ? adj[selectedId] : null
  const baseColor = (n: EstateNode) =>
    n.kind === 'module' ? MODULE_COLOR : colorMap[categoryOf(n, groupBy)] || '#64748b'

  // recolour meshes + labels when pivot or selection changes (no full rebuild)
  useEffect(() => {
    const dim = (id: string) => selectedId && !(id === selectedId || near?.has(id))
    for (const n of graph.nodes as EstateNode[]) {
      if (!n.__mesh) continue
      const c = new THREE.Color(baseColor(n))
      if (dim(n.id)) c.lerp(new THREE.Color('#0a0e17'), 0.72)
      ;(n.__mesh.material as THREE.MeshLambertMaterial).color = c
      if (n.__sprite) n.__sprite.color = dim(n.id) ? 'rgba(150,160,180,.4)' : '#e2e8f0'
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupBy, selectedId, colorMap, graph])

  function nodeObj(n: EstateNode) {
    const grp = new THREE.Group()
    const s = n.kind === 'module' ? 5 : Math.max(3, n.indeg * 1.1 + 3)
    const geo = n.kind === 'module'
      ? new THREE.OctahedronGeometry(s)
      : new THREE.SphereGeometry(s, 16, 16)
    const mesh = new THREE.Mesh(geo, new THREE.MeshLambertMaterial({ color: baseColor(n) }))
    n.__mesh = mesh
    grp.add(mesh)
    const sprite = new SpriteText(n.name)
    sprite.color = '#e2e8f0'
    sprite.textHeight = n.kind === 'module' ? 4 : 3.4
    sprite.backgroundColor = 'rgba(10,14,23,.6)'
    sprite.padding = 1.5
    ;(sprite as unknown as { material: { depthTest: boolean } }).material.depthTest = false
    ;(sprite as unknown as { position: { set: (x: number, y: number, z: number) => void } }).position.set(0, s + 5, 0)
    n.__sprite = sprite
    grp.add(sprite)
    return grp
  }

  // Frame with cameraPosition (NOT zoomToFit): the click-to-orbit recenter also
  // uses cameraPosition, and react-force-graph keeps separate internal camera
  // state for zoomToFit vs cameraPosition — mixing them makes the recenter fall
  // back to the default view instead of centring on the clicked node. Keep the
  // whole camera on one API. (Same framing the shared resource graph uses.)
  function frame() {
    const fg = fgRef.current
    if (!fg) return
    const box = new THREE.Box3()
    for (const n of graph.nodes as EstateNode[]) {
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

  useEffect(() => {
    const fg = fgRef.current
    if (!fg) return
    // Tame the default TrackballControls into an orbit-like feel (upright, no
    // inertial drift), keeping node-drag; clicking a node swings the pivot onto
    // it (onNodeClick) so dragging the background orbits it.
    const controls = fg.controls?.()
    if (controls) {
      controls.noRoll = true
      controls.staticMoving = true
    }
    fg.d3Force('charge')?.strength?.(-130)?.distanceMax?.(220)
    fg.d3Force('link')?.distance?.(52)
    const nodes = graph.nodes
    const centre = (a: number) => {
      for (const n of nodes) {
        if (n.x == null) continue
        n.vx = (n.vx || 0) - n.x * 0.05 * a
        n.vy = (n.vy || 0) - (n.y || 0) * 0.05 * a
        n.vz = (n.vz || 0) - (n.z || 0) * 0.05 * a
      }
    }
    ;(centre as unknown as { initialize: () => void }).initialize = () => {}
    fg.d3Force('centre', centre)
  }, [graph])

  const linkLit = (l: EstateEdge): boolean => {
    if (!selectedId) return true
    const s = endId(l.source), t = endId(l.target)
    return (near?.has(s) || s === selectedId) && (near?.has(t) || t === selectedId)
  }

  return (
    <div ref={wrapRef} className="absolute inset-0">
      <FG3D
        ref={fgRef}
        width={size.w}
        height={size.h}
        backgroundColor="#0a0e17"
        graphData={data}
        showNavInfo={false}
        cooldownTicks={180}
        onEngineStop={() => {
          if (!userMoved.current) frame() // don't stomp a camera the user moved
        }}
        nodeThreeObject={nodeObj}
        onNodeClick={(n) => {
          onSelect(n.id === selectedId ? null : n)
          // Make the clicked node the rotation pivot by setting the controls
          // TARGET directly. NOT cameraPosition(): react-force-graph's
          // cameraPosition() getter returns its own cached position which does
          // NOT track TrackballControls rotation, so passing it back snaps the
          // camera to the last API-set (default) view — the "click resets to
          // default" bug. Setting controls.target re-aims onto the node without
          // touching the camera position, so a subsequent background-drag orbits
          // it. A click ≠ a drag, so node drag still works.
          userMoved.current = true
          const controls = fgRef.current?.controls?.()
          if (controls?.target && n.x != null && n.y != null && n.z != null) {
            controls.target.set(n.x, n.y, n.z)
            controls.update?.()
          }
        }}
        linkColor={(l) => (linkLit(l) ? EDGE[l.kind] || '#94a3b8' : 'rgba(120,130,150,.1)')}
        linkWidth={(l) => (l.kind === 'uses-module' ? 0.5 : 1.2)}
        linkDirectionalArrowLength={3.2}
        linkDirectionalArrowRelPos={1}
        linkDirectionalArrowColor={(l) => EDGE[l.kind] || '#94a3b8'}
      />
      <div className="absolute bottom-1.5 inset-x-0 text-center text-[10px] text-slate-500 pointer-events-none select-none">
        {t.rich('estate.navHint', {
          em: (chunks) => <span className="text-slate-400">{chunks}</span>,
        })}
      </div>
    </div>
  )
}
