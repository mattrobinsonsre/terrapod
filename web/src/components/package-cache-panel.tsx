'use client'

import { useCallback, useEffect, useState } from 'react'
import { useLocale, useTranslations } from 'next-intl'

import { EmptyState } from '@/components/empty-state'
import { MobileCard, MobileCardList } from '@/components/mobile-card-list'
import { SortableHeader } from '@/components/sortable-header'
import { StatChip } from '@/components/stat-chip'
import { apiFetch, fetchAllPages, parseApiError } from '@/lib/api'
import { formatBytes } from '@/lib/format-bytes'
import { withSealedWarning } from '@/lib/cache-sealed'
import { useConfirm } from '@/lib/use-confirm'
import { useDebouncedValue } from '@/lib/use-debounced-value'
import { useSortable } from '@/lib/use-sortable'

/**
 * The package cache — galaxy, pypi, npm, go, nuget, pulumi — listed, totalled
 * and purgeable (#2474). Until this existed an operator could warm the cache
 * and never see what was in it, how big it had grown, or drop one bad artifact
 * without going to object storage by hand.
 *
 * This list is an ORDINARY paged collection: one row per artifact in
 * `cached_package_files`, so the response carries `meta.pagination` and
 * `fetchAllPages` is the right helper. The sibling archive panel is the
 * opposite case and says so; the two are not inconsistent, they are a table
 * and a storage walk.
 */
export const PACKAGE_ECOSYSTEMS = ['galaxy', 'pypi', 'npm', 'go', 'nuget', 'pulumi'] as const

interface CachedPackageFile {
  id: string
  attributes: {
    ecosystem: string
    name: string
    version: string
    filename: string
    size: number
    digest: string
    'cached-at': string | null
    'last-accessed-at': string | null
  }
}

interface PackageTotal {
  id: string
  attributes: { ecosystem: string; files: number; bytes: number }
}

type SortKey = 'ecosystem' | 'name' | 'version' | 'filename' | 'size' | 'last-accessed-at'

export function PackageCachePanel({
  onError,
  onSuccess,
  formatDate,
}: {
  onError: (message: string) => void
  onSuccess: (message: string) => void
  formatDate: (iso: string | null) => string
}) {
  const t = useTranslations('adminBinaryCache')
  const locale = useLocale()
  const { confirmIrreversible } = useConfirm()

  const [entries, setEntries] = useState<CachedPackageFile[]>([])
  const [totals, setTotals] = useState<PackageTotal[]>([])
  const [overall, setOverall] = useState({ files: 0, bytes: 0 })
  // Whether this node can re-fetch what is purged. Read from the TOTALS
  // rather than from an eviction response, because the confirmation needs it
  // before it asks (#2474).
  const [sealed, setSealed] = useState(false)

  // The LIST filters. Both are applied server-side — `name` as a prefix, which
  // the placeholder says, because a practitioner hunting `community.` wants the
  // whole collection family and would otherwise guess at a wildcard syntax.
  const [ecosystem, setEcosystem] = useState('')
  const [name, setName] = useState('')
  // Debounced, because the prefix match is applied by the SERVER: undebounced,
  // every keystroke of `community.` would be a list fetch plus a totals
  // aggregate. The select needs none -- it changes once per choice.
  const appliedName = useDebouncedValue(name)

  // The PURGE filters are deliberately separate inputs rather than the list's.
  // The evict endpoint takes an ecosystem and an age and has no name filter, so
  // a button reading "purge what I am looking at" would purge beyond a name
  // prefix the operator had typed — wider than they could see, which on an
  // irreversible action is the one direction that must not be guessed.
  const [purgeEcosystem, setPurgeEcosystem] = useState('')
  const [purgeDays, setPurgeDays] = useState('')
  const [purging, setPurging] = useState(false)

  const accessor = useCallback((item: CachedPackageFile, key: SortKey) => {
    switch (key) {
      case 'ecosystem': return item.attributes.ecosystem
      case 'name': return item.attributes.name
      case 'version': return item.attributes.version
      case 'filename': return item.attributes.filename
      case 'size': return item.attributes.size
      case 'last-accessed-at': return item.attributes['last-accessed-at']
    }
  }, [])
  const { sortedItems, sortState, toggleSort } = useSortable<CachedPackageFile, SortKey>(
    entries, 'last-accessed-at', 'desc', accessor,
  )

  const load = useCallback(async () => {
    const params = new URLSearchParams()
    if (ecosystem) params.set('ecosystem', ecosystem)
    if (appliedName) params.set('name', appliedName)
    const query = params.toString() ? `?${params.toString()}` : ''
    try {
      const [list, totalsRes] = await Promise.all([
        fetchAllPages<CachedPackageFile>(`/api/v1/admin/package-cache${query}`),
        apiFetch('/api/v1/admin/package-cache/totals'),
      ])
      setEntries(list)
      if (totalsRes.ok) {
        const body = await totalsRes.json()
        setTotals(Array.isArray(body?.data) ? body.data : [])
        // The overall figure comes from `meta`, which the server computed over
        // the WHOLE table — summing the rendered rows would be right today and
        // wrong the moment the totals response is ever paged.
        setOverall({
          files: Number(body?.meta?.files ?? 0),
          bytes: Number(body?.meta?.bytes ?? 0),
        })
        setSealed(body?.meta?.sealed === true)
      }
    } catch (err) {
      onError(err instanceof Error ? err.message : t('errors.loadPackages'))
    }
  }, [ecosystem, appliedName, onError, t])

  useEffect(() => {
    void load()
  }, [load])

  async function purgeOne(entry: CachedPackageFile) {
    const a = entry.attributes
    if (!confirmIrreversible(withSealedWarning(
      t('packages.confirmRow', { name: a.name, version: a.version, filename: a.filename }),
      sealed,
      t('sealedWarning'),
    ))) return
    try {
      const res = await apiFetch(`/api/v1/admin/package-cache/${entry.id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(await parseApiError(res, t('packages.errorRow')))
      onSuccess(t('packages.successRow', { filename: a.filename }))
      await load()
    } catch (err) {
      onError(err instanceof Error ? err.message : t('packages.errorRow'))
    }
  }

  /** The prompt for a bulk purge, naming exactly what the server will match.
   *
   *  Four sentences rather than one composed from fragments: a clause stitched
   *  together reads as machine output in English and ungrammatically in half
   *  the languages this ships in, and this is the last thing an operator sees
   *  before something irreversible.
   *
   *  The figures are appended ONLY when no age filter is set. With one, the
   *  count is genuinely unknown ahead of the sweep, and a total quoted beside
   *  an age-narrowed purge would overstate what is about to go. */
  function purgeScopeMessage(days: number | null): string {
    const eco = purgeEcosystem
    if (days !== null) {
      return withSealedWarning(
        eco
          ? t('packages.confirmEcosystemAge', { ecosystem: eco, days })
          : t('packages.confirmAge', { days }),
        sealed,
        t('sealedWarning'),
      )
    }
    const row = totals.find((x) => x.attributes.ecosystem === eco)
    const figures = eco
      ? t('packages.confirmFigures', {
          files: row?.attributes.files ?? 0,
          size: formatBytes(row?.attributes.bytes ?? 0, locale),
        })
      : t('packages.confirmFigures', {
          files: overall.files,
          size: formatBytes(overall.bytes, locale),
        })
    const scope = eco
      ? t('packages.confirmEcosystem', { ecosystem: eco })
      : t('packages.confirmAll')
    return withSealedWarning(`${scope} ${figures}`, sealed, t('sealedWarning'))
  }

  async function purgeMatching() {
    const days = purgeDays.trim() === '' ? null : Number(purgeDays)
    if (days !== null && (!Number.isInteger(days) || days < 0)) {
      onError(t('packages.errorBulk'))
      return
    }
    if (!confirmIrreversible(purgeScopeMessage(days))) return
    setPurging(true)
    try {
      const res = await apiFetch('/api/v1/admin/package-cache/evict', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ecosystem: purgeEcosystem,
          ...(days === null ? {} : { older_than_days: days }),
        }),
      })
      if (!res.ok) throw new Error(await parseApiError(res, t('packages.errorBulk')))
      const body = await res.json()
      onSuccess(t('packages.successBulk', {
        files: Number(body?.files ?? 0),
        size: formatBytes(Number(body?.bytes ?? 0), locale),
      }))
      await load()
    } catch (err) {
      onError(err instanceof Error ? err.message : t('packages.errorBulk'))
    } finally {
      setPurging(false)
    }
  }

  const ecosystemOptions = (
    <>
      <option value="">{t('packages.ecosystemAll')}</option>
      {PACKAGE_ECOSYSTEMS.map((e) => (
        // An ecosystem id is an identifier, not prose: `pypi` is `pypi` in
        // every language, as the resource addresses elsewhere are.
        <option key={e} value={e}>{e}</option>
      ))}
    </>
  )

  return (
    <section data-testid="package-cache-section" className="mt-8 mb-8">
      <h2 className="text-lg font-semibold text-slate-200 mb-3">{t('sections.packages')}</h2>

      {/* Totals strip: computed over the whole table server-side, so it is a
          total rather than a sum of whatever the page happened to show. */}
      <div className="mb-3">
        <p className="text-xs uppercase tracking-wider text-slate-500 mb-2">{t('packages.totalsHeading')}</p>
        <div className="flex flex-wrap gap-2">
          <StatChip
            label={t('packages.ecosystemAll')}
            value={t('stats.packages', { files: overall.files, size: formatBytes(overall.bytes, locale) })}
          />
          {totals.map((row) => (
            <StatChip
              key={row.id}
              label={row.attributes.ecosystem}
              value={t('stats.packages', {
                files: row.attributes.files,
                size: formatBytes(row.attributes.bytes, locale),
              })}
            />
          ))}
        </div>
      </div>

      {/* Filters */}
      <div className="mb-3 grid gap-3 sm:grid-cols-2">
        <div>
          <label htmlFor="pkg-ecosystem" className="block text-sm text-slate-300 mb-1">{t('columns.ecosystem')}</label>
          <select
            id="pkg-ecosystem"
            value={ecosystem}
            onChange={(e) => setEcosystem(e.target.value)}
            className="w-full min-h-11 px-3 py-2 rounded-lg bg-slate-900/70 border border-slate-700 text-sm text-slate-200"
          >
            {ecosystemOptions}
          </select>
        </div>
        <div>
          <label htmlFor="pkg-name" className="block text-sm text-slate-300 mb-1">{t('columns.name')}</label>
          <input
            id="pkg-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t('packages.namePlaceholder')}
            className="w-full min-h-11 px-3 py-2 rounded-lg bg-slate-900/70 border border-slate-700 text-sm text-slate-200 font-mono"
          />
        </div>
      </div>

      {/* Bulk purge */}
      <div className="mb-4 rounded-lg border border-slate-700/50 bg-slate-800/50 p-3">
        <p className="text-sm font-semibold text-slate-200 mb-2">{t('packages.bulkHeading')}</p>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label htmlFor="pkg-purge-ecosystem" className="block text-sm text-slate-300 mb-1">{t('columns.ecosystem')}</label>
            <select
              id="pkg-purge-ecosystem"
              value={purgeEcosystem}
              onChange={(e) => setPurgeEcosystem(e.target.value)}
              className="w-full min-h-11 px-3 py-2 rounded-lg bg-slate-900/70 border border-slate-700 text-sm text-slate-200"
            >
              {ecosystemOptions}
            </select>
          </div>
          <div>
            <label htmlFor="pkg-purge-days" className="block text-sm text-slate-300 mb-1">{t('packages.bulkDaysLabel')}</label>
            <input
              id="pkg-purge-days"
              type="number"
              min={0}
              inputMode="numeric"
              value={purgeDays}
              onChange={(e) => setPurgeDays(e.target.value)}
              className="w-full min-h-11 px-3 py-2 rounded-lg bg-slate-900/70 border border-slate-700 text-sm text-slate-200"
            />
          </div>
        </div>
        <p className="mt-2 text-xs text-slate-400">{t('packages.bulkDaysHint')}</p>
        <button
          onClick={purgeMatching}
          disabled={purging}
          data-testid="package-bulk-purge"
          className="mt-3 min-h-11 px-4 py-2 rounded-lg text-sm font-medium bg-red-900/40 hover:bg-red-900/60 disabled:opacity-50 text-red-300 transition-colors"
        >
          {purging ? t('batchPurge.purging') : t('bulk.button')}
        </button>
      </div>

      {entries.length === 0 ? (
        <EmptyState message={t('empty.packages')} />
      ) : (
        <>
          <div className="hidden md:block bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <SortableHeader label={t('columns.ecosystem')} sortKey="ecosystem" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label={t('columns.name')} sortKey="name" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label={t('columns.version')} sortKey="version" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label={t('columns.filename')} sortKey="filename" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label={t('columns.size')} sortKey="size" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label={t('columns.lastAccessed')} sortKey="last-accessed-at" sortState={sortState} onSort={toggleSort} />
                  <th className="text-end px-4 py-3 text-slate-400 font-medium">{t('columns.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {sortedItems.map((entry) => (
                  <tr key={entry.id} className="border-b border-slate-700/30 last:border-0">
                    <td className="px-4 py-3 text-slate-400">{entry.attributes.ecosystem}</td>
                    <td className="px-4 py-3 text-slate-200 font-mono break-all">{entry.attributes.name}</td>
                    <td className="px-4 py-3 text-slate-200 font-mono">{entry.attributes.version}</td>
                    <td className="px-4 py-3 text-slate-400 font-mono text-xs break-all">{entry.attributes.filename}</td>
                    <td className="px-4 py-3 text-slate-400">{formatBytes(entry.attributes.size, locale)}</td>
                    <td className="px-4 py-3 text-slate-400 text-xs">{formatDate(entry.attributes['last-accessed-at'])}</td>
                    <td className="px-4 py-3 text-end">
                      <button
                        onClick={() => purgeOne(entry)}
                        className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors"
                      >
                        {t('purge.button')}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* The `< md` half of the same list. The shared primitive, not a second
              card pattern: every admin list reflows the same way. */}
          <MobileCardList>
            {sortedItems.map((entry) => (
              <MobileCard
                key={entry.id}
                title={<span className="font-mono text-sm text-slate-200 break-all">{entry.attributes.name}</span>}
                badge={<span className="shrink-0 rounded px-2 py-0.5 text-xs bg-slate-700/60 text-slate-300">{entry.attributes.ecosystem}</span>}
                fields={[
                  { label: t('columns.version'), value: entry.attributes.version },
                  { label: t('columns.filename'), value: entry.attributes.filename },
                  { label: t('columns.size'), value: formatBytes(entry.attributes.size, locale) },
                  { label: t('columns.lastAccessed'), value: formatDate(entry.attributes['last-accessed-at']) },
                ]}
                actions={
                  <button
                    onClick={() => purgeOne(entry)}
                    className="min-h-11 px-3 py-2 rounded-lg text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors"
                  >
                    {t('purge.button')}
                  </button>
                }
              />
            ))}
          </MobileCardList>
        </>
      )}
    </section>
  )
}
