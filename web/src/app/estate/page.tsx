'use client'

// Estate topology page (#763). A whole-estate dependency + module-impact view:
// a WebGL graph on desktop, with an equivalent accessible table (the fallback
// required by #736 — 3D is never the only path) that is also the default on a
// phone, where heavy WebGL is a poor fit (#719).
import { useEffect, useMemo, useState } from 'react'
import dynamic from 'next/dynamic'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { apiFetch } from '@/lib/api'
import { useIsMobile } from '@/lib/use-media-query'
import {
  groupAxes,
  categoryOf,
  endId,
  PALETTE,
  MODULE_COLOR,
  type EstateGraphData,
  type EstateNode,
} from '@/lib/estate-graph'

const EstateGraph3D = dynamic(
  () => import('@/components/estate-graph').then((m) => m.EstateGraph3D),
  { ssr: false, loading: () => <LoadingSpinner /> },
)

// Module-level (stable identity). Defining this INSIDE the page component makes
// it a new component type on every render, which unmounts + REMOUNTS the whole
// subtree — including the WebGL <EstateGraph3D> — on every state change. That
// remount is what reset the graph to its default view whenever you selected a
// node (#770).
function Shell({ children }: { children: React.ReactNode }) {
  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-7xl mx-auto">{children}</main>
    </>
  )
}

export default function EstatePage() {
  const t = useTranslations('estate')
  const [graph, setGraph] = useState<EstateGraphData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [groupBy, setGroupBy] = useState('none')
  const [selected, setSelected] = useState<EstateNode | null>(null)
  const isMobile = useIsMobile()
  const [view, setView] = useState<'graph' | 'table'>('graph')

  // Phones default to the table (WebGL is heavy + the graph is desktop-oriented);
  // the graph stays one tap away. Desktop defaults to the graph.
  useEffect(() => setView(isMobile ? 'table' : 'graph'), [isMobile])

  useEffect(() => {
    let cancelled = false
    apiFetch('/api/terrapod/v1/estate-graph')
      .then(async (r) => {
        if (!r.ok) throw new Error(t('errors.load'))
        const b = await r.json()
        if (!cancelled) setGraph(b.data.attributes as EstateGraphData)
      })
      .catch((e: Error) => !cancelled && setError(e.message))
    return () => {
      cancelled = true
    }
  }, [])

  const axes = useMemo(() => (graph ? groupAxes(graph.nodes) : []), [graph])
  const categories = useMemo(() => {
    if (!graph) return []
    const ws = graph.nodes.filter((n) => n.kind === 'workspace')
    return [...new Set(ws.map((n) => categoryOf(n, groupBy)))].sort()
  }, [graph, groupBy])
  const colorFor = (c: string) => PALETTE[categories.indexOf(c) % PALETTE.length]

  // per-workspace module usage, for the table + selection detail
  const modulesByWs = useMemo(() => {
    const m: Record<string, string[]> = {}
    if (!graph) return m
    const nameById = Object.fromEntries(graph.nodes.map((n) => [n.id, n.name]))
    for (const e of graph.edges) {
      if (e.kind === 'uses-module') {
        const ws = endId(e.target)
        ;(m[ws] ||= []).push(nameById[endId(e.source)])
      }
    }
    return m
  }, [graph])

  if (error) return <Shell><ErrorBanner message={error} /></Shell>
  if (!graph) return <Shell><LoadingSpinner /></Shell>

  const workspaces = graph.nodes.filter((n) => n.kind === 'workspace')
  const modules = graph.nodes.filter((n) => n.kind === 'module')
  const usedBy = (modId: string) =>
    graph.edges.filter((e) => e.kind === 'uses-module' && endId(e.source) === modId).length

  const ToggleBtn = ({ v, label }: { v: 'graph' | 'table'; label: string }) => (
    <button
      onClick={() => setView(v)}
      className={`px-3 py-1.5 rounded-lg text-xs font-medium ${
        view === v ? 'bg-brand-500/25 text-brand-300 outline outline-1 outline-brand-500/50' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'
      }`}
    >
      {label}
    </button>
  )

  return (
    <Shell>
      <PageHeader
        title={t('title')}
        description={t('description')}
      />

      <div className="flex flex-wrap items-center gap-3 mb-4">
        <label className="text-xs text-slate-400">
          {t('groupBy.label')}{' '}
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
          <ToggleBtn v="graph" label={t('view.graph')} />
          <ToggleBtn v="table" label={t('view.table')} />
        </div>
        <span className="text-xs text-slate-500">
          {t('counts.workspaces', { count: graph.meta.counts.workspaces })} ·{' '}
          {t('counts.modules', { count: graph.meta.counts.modules })} ·{' '}
          {t('counts.dependencies', { count: graph.meta.counts.edges })}
        </span>
      </div>

      {/* legend — shared by both views */}
      <div className="flex flex-wrap gap-x-4 gap-y-1.5 mb-4 text-[11px] text-slate-300">
        {categories.map((c) => (
          <span key={c} className="flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-full" style={{ background: colorFor(c) }} />
            {c}
          </span>
        ))}
        <span className="flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rotate-45" style={{ background: MODULE_COLOR }} />
          {t('legend.registryModule')}
        </span>
      </div>

      {view === 'graph' ? (
        <div className="relative w-full h-[70vh] min-h-[420px] rounded-xl overflow-hidden border border-slate-800 bg-[#0a0e17]">
          <EstateGraph3D
            graph={graph}
            groupBy={groupBy}
            selectedId={selected?.id ?? null}
            onSelect={setSelected}
          />
          {selected && (
            <div className="absolute z-10 bottom-3 left-3 max-w-[min(360px,80vw)] rounded-xl border border-slate-700/40 bg-slate-900/85 backdrop-blur px-4 py-3">
              <div className="font-mono text-xs text-slate-100 break-all">{selected.name}</div>
              {selected.kind === 'module' ? (
                <div className="text-sm mt-1.5">
                  <b>{usedBy(selected.id)}</b>{' '}
                  <span className="text-slate-400">{t('detail.moduleImpact', { count: usedBy(selected.id) })}</span>
                </div>
              ) : (
                <>
                  <div className="text-[11px] text-slate-400 mt-1">
                    {selected.pool}
                    {Object.entries(selected.labels).map(([k, v]) => ` · ${k}=${v}`).join('')}
                  </div>
                  <div className="text-lg font-bold mt-1.5">
                    {selected.indeg}{' '}
                    <span className="text-xs font-medium text-slate-400">{t('detail.dependOnThis', { count: selected.indeg })}</span>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      ) : (
        <div className="flex flex-col gap-6">
          <section>
            <h2 className="text-sm font-semibold text-slate-200 mb-2">{t('table.workspaces.heading')}</h2>
            <div className="overflow-x-auto rounded-xl border border-slate-800">
              <table className="w-full text-sm">
                <thead className="bg-slate-800/50 text-slate-400 text-xs">
                  <tr>
                    <th scope="col" className="text-left px-3 py-2">{t('table.workspaces.col.workspace')}</th>
                    <th scope="col" className="text-left px-3 py-2">
                      {groupBy === 'none' ? t('table.workspaces.col.group') : axes.find((a) => a.value === groupBy)?.label}
                    </th>
                    <th scope="col" className="text-left px-3 py-2">{t('table.workspaces.col.agentPool')}</th>
                    <th scope="col" className="text-right px-3 py-2">{t('table.workspaces.col.dependedOnBy')}</th>
                    <th scope="col" className="text-left px-3 py-2">{t('table.workspaces.col.modulesUsed')}</th>
                  </tr>
                </thead>
                <tbody>
                  {workspaces.map((n) => (
                    <tr key={n.id} className="border-t border-slate-800/70">
                      <th scope="row" className="text-left px-3 py-2 font-mono text-xs text-slate-100 font-normal">
                        {n.name}
                      </th>
                      <td className="px-3 py-2 text-slate-300">{categoryOf(n, groupBy)}</td>
                      <td className="px-3 py-2 text-slate-400">{n.pool}</td>
                      <td className="px-3 py-2 text-right tabular-nums text-slate-300">{n.indeg}</td>
                      <td className="px-3 py-2 text-slate-400 text-xs">
                        {(modulesByWs[n.id] || []).join(', ') || t('table.workspaces.noModules')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          {modules.length > 0 && (
            <section>
              <h2 className="text-sm font-semibold text-slate-200 mb-2">{t('table.modules.heading')}</h2>
              <div className="overflow-x-auto rounded-xl border border-slate-800">
                <table className="w-full text-sm">
                  <thead className="bg-slate-800/50 text-slate-400 text-xs">
                    <tr>
                      <th scope="col" className="text-left px-3 py-2">{t('table.modules.col.module')}</th>
                      <th scope="col" className="text-right px-3 py-2">{t('table.modules.col.usedBy')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {modules.map((m) => (
                      <tr key={m.id} className="border-t border-slate-800/70">
                        <th scope="row" className="text-left px-3 py-2 font-mono text-xs text-slate-100 font-normal">
                          {m.name}
                        </th>
                        <td className="px-3 py-2 text-right tabular-nums text-slate-300">
                          {usedBy(m.id)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </div>
      )}
    </Shell>
  )
}
