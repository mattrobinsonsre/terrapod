'use client'

// State resource graph tab (#765) — rendered inside the workspace detail page.
// A WebGL dependency graph of the workspace's Terraform state on desktop, with
// an equivalent accessible table (the fallback required by #736 — 3D is never
// the only path) that is also the default on a phone (#719). Defaults to the
// current state version; a picker drops back to any older one.
import { useEffect, useMemo, useState } from 'react'
import dynamic from 'next/dynamic'
import { useTranslations } from 'next-intl'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { apiFetch } from '@/lib/api'
import { useIsMobile } from '@/lib/use-media-query'
import { groupAxes, type StateGraphData } from '@/lib/state-graph'

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
  const t = useTranslations('graphs')
  const [graph, setGraph] = useState<StateGraphData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState<string>('') // '' = current
  const [groupBy, setGroupBy] = useState('type')
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
        if (!r.ok) throw new Error(t('stateTab.loadError'))
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
  }, [workspaceId, version, t])

  const axes = useMemo(() => (graph ? groupAxes(graph.nodes) : []), [graph])

  const sortedNodes = useMemo(
    () => (graph ? [...graph.nodes].sort((a, b) => b.indeg - a.indeg || a.id.localeCompare(b.id)) : []),
    [graph],
  )

  if (error) return <ErrorBanner message={error} />
  if (!graph) return <LoadingSpinner />

  const versions = graph.meta.versions
  if (versions.length === 0) {
    return <EmptyState message={t('stateTab.emptyState')} />
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <label className="text-xs text-slate-400">
          {t('stateTab.stateVersionLabel')}{' '}
          <select
            value={version}
            onChange={(e) => setVersion(e.target.value)}
            className="ml-1 text-sm bg-slate-800 border border-slate-700 rounded-lg px-2 py-1.5 text-slate-100"
          >
            {versions.map((v) => (
              <option key={v.id} value={v.is_current ? '' : v.id}>
                v{v.serial}
                {v.is_current ? ` ${t('stateTab.currentSuffix')}` : ''} · {new Date(v.created_at).toLocaleString()}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-slate-400">
          {t('stateTab.colorByLabel')}{' '}
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
          <ToggleBtn v="graph" label={t('stateTab.viewGraph')} view={view} onClick={setViewOverride} />
          <ToggleBtn v="table" label={t('stateTab.viewTable')} view={view} onClick={setViewOverride} />
        </div>
        <span className="text-xs text-slate-500">
          {t('stateTab.resourceCount', { count: graph.meta.counts.resources })} ·{' '}
          {t('stateTab.dependencyCount', { count: graph.meta.counts.edges })}
        </span>
      </div>

      {graph.meta.truncated && (
        <p className="mb-3 text-xs text-amber-400">
          {t('stateTab.truncated', { shown: graph.meta.max_nodes, total: graph.meta.total_resources })}
        </p>
      )}

      {view === 'graph' ? (
        <StateGraph3D
          graph={graph}
          groupBy={groupBy}
          subtitle={`${graph.meta.state_version ? `v${graph.meta.state_version.serial}${graph.meta.state_version.is_current ? ` ${t('stateTab.currentSuffix')}` : ''} · ` : ''}${t('stateTab.resourceCount', { count: graph.meta.counts.resources })}`}
        />
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full text-sm">
            <thead className="bg-slate-800/50 text-slate-400 text-xs">
              <tr>
                <th scope="col" className="text-left px-3 py-2">{t('stateTab.colResource')}</th>
                <th scope="col" className="text-left px-3 py-2">{t('stateTab.colType')}</th>
                <th scope="col" className="text-left px-3 py-2">{t('stateTab.colMode')}</th>
                <th scope="col" className="text-left px-3 py-2">{t('stateTab.colModule')}</th>
                <th scope="col" className="text-right px-3 py-2">{t('stateTab.colDependedOnBy')}</th>
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
                  <td className="px-3 py-2 text-slate-400 text-xs">{n.module || t('stateTab.rootModule')}</td>
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
