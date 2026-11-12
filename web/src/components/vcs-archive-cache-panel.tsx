'use client'

import { useCallback, useEffect, useState } from 'react'
import { useLocale, useTranslations } from 'next-intl'

import { EmptyState } from '@/components/empty-state'
import { MobileCard, MobileCardList } from '@/components/mobile-card-list'
import { StatChip } from '@/components/stat-chip'
import { apiFetch, parseApiError } from '@/lib/api'
import { formatBytes } from '@/lib/format-bytes'
import { withSealedWarning } from '@/lib/cache-sealed'
import { useConfirm } from '@/lib/use-confirm'
import { useDebouncedValue } from '@/lib/use-debounced-value'

/**
 * The VCS archive cache — the repository tarballs a poll, a plan and a drift
 * check all read from — listed, totalled and purgeable (#2474). It had no admin
 * surface at all before this.
 *
 * **This list is CURSOR-paged and must stay that way.** There are no rows: an
 * archive is addressed entirely by its storage key, so a page comes from a
 * storage walk, and `list_prefix` is key-ordered on every backend precisely so
 * a cursor is meaningful. The response therefore carries `meta.next-cursor` and
 * `meta.page-size` and NOT `meta.pagination` — so `fetchAllPages` from
 * `@/lib/api` is the wrong helper here and would loop for ever asking for
 * `page[number]=2` of something that has no page numbers. An empty cursor is
 * how the end is known.
 *
 * The package panel beside it is an ordinary paged table. The two differing is
 * not an inconsistency to tidy; it is a table and a bucket walk.
 */
const PAGE_SIZE = 100

interface CachedArchive {
  id: string
  attributes: {
    key: string
    'connection-id': string
    owner: string
    repo: string
    sha: string
    'paths-hash': string
    size: number
    'last-modified': string
  }
}

interface ArchiveTotal {
  id: string
  attributes: {
    'connection-id': string
    owner: string
    repo: string
    archives: number
    bytes: number
  }
}

export function VcsArchiveCachePanel({
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

  const [archives, setArchives] = useState<CachedArchive[]>([])
  /** The key to resume the walk from. `''` means "no more pages" once a page
   *  has been fetched, which is why `loaded` is tracked separately — before the
   *  first fetch an empty cursor means "start at the beginning". */
  const [cursor, setCursor] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [loadingPage, setLoadingPage] = useState(false)

  const [totals, setTotals] = useState<ArchiveTotal[]>([])
  const [overall, setOverall] = useState({ archives: 0, bytes: 0 })
  /** The walk has a ceiling, so the totals can be a FLOOR rather than a total.
   *  Shown in words below: an operator hunting what is filling a bucket must
   *  not be handed a number smaller than the problem with nothing saying so. */
  const [truncated, setTruncated] = useState(false)
  // Whether this node can re-fetch what is purged. Read from the TOTALS
  // rather than from an eviction response, because the confirmation needs it
  // before it asks (#2474).
  const [sealed, setSealed] = useState(false)
  const [scanLimit, setScanLimit] = useState(0)

  const [connectionId, setConnectionId] = useState('')
  // Debounced for the reason the package panel's name filter is, and more so:
  // narrowing restarts the walk, so an undebounced keystroke is a storage walk
  // against a bucket the operator came here because it is large.
  const appliedConnectionId = useDebouncedValue(connectionId)
  const [purgeConnectionId, setPurgeConnectionId] = useState('')
  const [purgeDays, setPurgeDays] = useState('')
  const [purging, setPurging] = useState(false)
  /** Where an unfinished purge stopped, and how much it has taken so far.
   *
   *  A bulk eviction walks a BOUNDED window of keys, so one call can leave more
   *  behind -- and unlike the package cache, repeating the call without this
   *  cursor re-examines the same window, which is what made a large archive
   *  cache unpurgeable however many times the button was pressed (#2491). So
   *  "press it again" is not a true instruction here, and the Continue control
   *  below is what makes it one. */
  const [purgeCursor, setPurgeCursor] = useState('')
  const [purgedSoFar, setPurgedSoFar] = useState({ archives: 0, bytes: 0 })
  /** EVERY filter the sweep in progress was STARTED with, which is not
   *  necessarily what the inputs hold now. Continuing with a different one
   *  would make the second window a different question from the first, so an
   *  operator who edits a field between windows would purge two different sets
   *  and be told it was one.
   *
   *  Both of them, not just the age: holding one and reading the other live is
   *  the asymmetry that makes a comment promising "the filters it started with"
   *  false for half of them. */
  const [purgedScope, setPurgedScope] = useState<{ connectionId: string; days: number | null }>(
    { connectionId: '', days: null },
  )

  /** One page of the walk. `after` empty restarts it; anything else resumes. */
  const fetchPage = useCallback(
    async (after: string, append: boolean) => {
      setLoadingPage(true)
      try {
        const params = new URLSearchParams({ limit: String(PAGE_SIZE) })
        if (appliedConnectionId) params.set('connection_id', appliedConnectionId)
        if (after) params.set('after', after)
        const res = await apiFetch(`/api/v1/admin/vcs-archive-cache?${params.toString()}`)
        if (!res.ok) throw new Error(await parseApiError(res, t('errors.loadArchives')))
        const body = await res.json()
        const page: CachedArchive[] = Array.isArray(body?.data) ? body.data : []
        setArchives((prev) => (append ? [...prev, ...page] : page))
        setCursor(String(body?.meta?.['next-cursor'] ?? ''))
        setLoaded(true)
      } catch (err) {
        onError(err instanceof Error ? err.message : t('errors.loadArchives'))
      } finally {
        setLoadingPage(false)
      }
    },
    [appliedConnectionId, onError, t],
  )

  const loadTotals = useCallback(async () => {
    try {
      const res = await apiFetch('/api/v1/admin/vcs-archive-cache/totals')
      if (!res.ok) throw new Error(await parseApiError(res, t('errors.loadArchives')))
      const body = await res.json()
      setTotals(Array.isArray(body?.data) ? body.data : [])
      setOverall({
        archives: Number(body?.meta?.archives ?? 0),
        bytes: Number(body?.meta?.bytes ?? 0),
      })
      setTruncated(Boolean(body?.meta?.truncated))
      setSealed(body?.meta?.sealed === true)
      setScanLimit(Number(body?.meta?.['scan-limit'] ?? 0))
    } catch (err) {
      onError(err instanceof Error ? err.message : t('errors.loadArchives'))
    }
  }, [onError, t])

  /** Restart the walk and re-read the totals. Called on mount, when the
   *  connection filter changes, and after anything is purged — never on a
   *  timer, because a tick that reset the cursor would silently throw away
   *  every page the operator had just loaded. */
  const reload = useCallback(async () => {
    setLoaded(false)
    await Promise.all([fetchPage('', false), loadTotals()])
  }, [fetchPage, loadTotals])

  useEffect(() => {
    void reload()
  }, [reload])

  async function purgeOne(entry: CachedArchive) {
    const a = entry.attributes
    const repo = `${a.owner}/${a.repo}`
    const sha = a.sha.slice(0, 8)
    if (!confirmIrreversible(withSealedWarning(
      t('archives.confirmRow', { repo, sha }),
      sealed,
      t('sealedWarning'),
    ))) return
    try {
      // The key is a QUERY parameter, not a path segment: it contains slashes,
      // and the server refuses anything not under the archive prefix.
      const res = await apiFetch(
        `/api/v1/admin/vcs-archive-cache?key=${encodeURIComponent(a.key)}`,
        { method: 'DELETE' },
      )
      if (!res.ok) throw new Error(await parseApiError(res, t('archives.errorRow')))
      onSuccess(t('archives.successRow', { repo, sha }))
      await reload()
    } catch (err) {
      onError(err instanceof Error ? err.message : t('archives.errorRow'))
    }
  }

  /** What a bulk purge is about to remove, in words.
   *
   *  The figures are appended only when they are BOTH exact and complete: no
   *  age filter (with one the count is unknown until the sweep runs) and an
   *  untruncated walk (otherwise the number is a floor, and quoting a floor as
   *  a total in the last prompt before an irreversible action is the one
   *  mistake this whole panel exists to avoid). */
  function purgeScopeMessage(days: number | null): string {
    const conn = purgeConnectionId
    if (days !== null) {
      return withSealedWarning(
        conn
          ? t('archives.confirmConnectionAge', { connection: conn, days })
          : t('archives.confirmAge', { days }),
        sealed,
        t('sealedWarning'),
      )
    }
    const scope = conn
      ? t('archives.confirmConnection', { connection: conn })
      : t('archives.confirmAll')
    if (truncated) return withSealedWarning(scope, sealed, t('sealedWarning'))
    const rows = conn
      ? totals.filter((x) => x.attributes['connection-id'] === conn)
      : totals
    const count = conn
      ? rows.reduce((n, x) => n + x.attributes.archives, 0)
      : overall.archives
    const bytes = conn
      ? rows.reduce((n, x) => n + x.attributes.bytes, 0)
      : overall.bytes
    return withSealedWarning(
      `${scope} ${t('archives.confirmFigures', { archives: count, size: formatBytes(bytes, locale) })}`,
      sealed,
      t('sealedWarning'),
    )
  }

  /** One bounded window of the sweep. `after` empty starts it; anything else
   *  continues the one in progress, with the SAME filters -- changing one
   *  mid-sweep would make the second window a different question from the
   *  first, which is why Continue re-sends the stored values rather than
   *  whatever is in the inputs now. */
  async function purgeWindow(after: string, scope: { connectionId: string; days: number | null }) {
    setPurging(true)
    try {
      const res = await apiFetch('/api/v1/admin/vcs-archive-cache/evict', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          connection_id: scope.connectionId,
          ...(scope.days === null ? {} : { older_than_days: scope.days }),
          ...(after ? { after } : {}),
        }),
      })
      if (!res.ok) throw new Error(await parseApiError(res, t('archives.errorBulk')))
      const body = await res.json()
      const archives = (after ? purgedSoFar.archives : 0) + Number(body?.archives ?? 0)
      const bytes = (after ? purgedSoFar.bytes : 0) + Number(body?.bytes ?? 0)
      setPurgedSoFar({ archives, bytes })
      const truncated = body?.truncated === true
      // The cursor IS the Continue affordance, exactly as it is for the
      // listing. Cleared on a finished sweep so the control goes away rather
      // than offering to continue something that has ended.
      setPurgeCursor(truncated ? String(body?.['next-cursor'] ?? '') : '')
      const args = { archives, size: formatBytes(bytes, locale) }
      onSuccess(
        truncated
          ? t('archives.successBulkTruncated', args)
          : t('archives.successBulk', args),
      )
      await reload()
    } catch (err) {
      onError(err instanceof Error ? err.message : t('archives.errorBulk'))
    } finally {
      setPurging(false)
    }
  }

  async function purgeMatching() {
    const days = purgeDays.trim() === '' ? null : Number(purgeDays)
    if (days !== null && (!Number.isInteger(days) || days < 0)) {
      onError(t('archives.errorBulk'))
      return
    }
    if (!confirmIrreversible(purgeScopeMessage(days))) return
    // A fresh sweep: snapshot the scope it is being started with, and restart
    // the running tally from this call.
    const scope = { connectionId: purgeConnectionId, days }
    setPurgedScope(scope)
    await purgeWindow('', scope)
  }

  /** Continue an unfinished sweep. No second confirm(): the operator confirmed
   *  this scope when they started it, and re-prompting per window on a purge
   *  that may need many of them trains people to click through the prompt --
   *  which is the one thing a confirmation must not become. */
  async function continuePurge() {
    await purgeWindow(purgeCursor, purgedScope)
  }

  /** `full` is a whole-repo archive, not a hash — rendered as the word, because
   *  a reader who sees four hex-looking values and one `full` has to go and
   *  find out which of them means "everything". */
  function paths(hash: string) {
    return hash === 'full'
      ? <span className="text-slate-300">{t('archives.pathsFull')}</span>
      : <span className="font-mono text-xs text-slate-400">{hash}</span>
  }

  return (
    <section data-testid="vcs-archive-cache-section">
      <h2 className="text-lg font-semibold text-slate-200 mb-3">{t('sections.vcsArchives')}</h2>

      <div className="mb-3">
        <p className="text-xs uppercase tracking-wider text-slate-500 mb-2">{t('archives.totalsHeading')}</p>
        <div className="flex flex-wrap gap-2">
          <StatChip
            label={t('archives.totalsAll')}
            value={t('stats.archives', { archives: overall.archives, size: formatBytes(overall.bytes, locale) })}
          />
          {totals.map((row) => (
            <StatChip
              key={row.id}
              label={`${row.attributes.owner}/${row.attributes.repo}`}
              value={t('stats.archives', {
                archives: row.attributes.archives,
                size: formatBytes(row.attributes.bytes, locale),
              })}
            />
          ))}
        </div>
        {truncated && (
          // In WORDS, beside the figures, not as an icon: the whole point is
          // that the numbers above understate the thing being looked for.
          <p data-testid="archive-totals-truncated" className="mt-2 text-xs text-amber-300">
            {t('archives.truncated', { limit: scanLimit })}
          </p>
        )}
      </div>

      <div className="mb-3">
        <label htmlFor="arch-connection" className="block text-sm text-slate-300 mb-1">{t('archives.connectionLabel')}</label>
        <input
          id="arch-connection"
          value={connectionId}
          onChange={(e) => setConnectionId(e.target.value)}
          placeholder={t('archives.connectionPlaceholder')}
          className="w-full min-h-11 px-3 py-2 rounded-lg bg-slate-900/70 border border-slate-700 text-sm text-slate-200 font-mono"
        />
      </div>

      <div className="mb-4 rounded-lg border border-slate-700/50 bg-slate-800/50 p-3">
        <p className="text-sm font-semibold text-slate-200 mb-2">{t('archives.bulkHeading')}</p>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label htmlFor="arch-purge-connection" className="block text-sm text-slate-300 mb-1">{t('archives.connectionLabel')}</label>
            <input
              id="arch-purge-connection"
              value={purgeConnectionId}
              onChange={(e) => setPurgeConnectionId(e.target.value)}
              placeholder={t('archives.connectionPlaceholder')}
              className="w-full min-h-11 px-3 py-2 rounded-lg bg-slate-900/70 border border-slate-700 text-sm text-slate-200 font-mono"
            />
          </div>
          <div>
            <label htmlFor="arch-purge-days" className="block text-sm text-slate-300 mb-1">{t('archives.bulkDaysLabel')}</label>
            <input
              id="arch-purge-days"
              type="number"
              min={0}
              inputMode="numeric"
              value={purgeDays}
              onChange={(e) => setPurgeDays(e.target.value)}
              className="w-full min-h-11 px-3 py-2 rounded-lg bg-slate-900/70 border border-slate-700 text-sm text-slate-200"
            />
          </div>
        </div>
        <p className="mt-2 text-xs text-slate-400">{t('archives.bulkDaysHint')}</p>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <button
            onClick={purgeMatching}
            disabled={purging}
            data-testid="archive-bulk-purge"
            className="min-h-11 px-4 py-2 rounded-lg text-sm font-medium bg-red-900/40 hover:bg-red-900/60 disabled:opacity-50 text-red-300 transition-colors"
          >
            {purging ? t('batchPurge.purging') : t('bulk.button')}
          </button>
          {/* Present only while a sweep is unfinished. Without it the operator
              is told more remains and has no way to reach it -- pressing Purge
              again restarts the walk at the same place (#2491). */}
          {purgeCursor && (
            <button
              onClick={continuePurge}
              disabled={purging}
              data-testid="archive-continue-purge"
              className="min-h-11 px-4 py-2 rounded-lg text-sm font-medium bg-amber-900/40 hover:bg-amber-900/60 disabled:opacity-50 text-amber-200 transition-colors"
            >
              {purging ? t('archives.continuing') : t('archives.continuePurge')}
            </button>
          )}
        </div>
      </div>

      {loaded && archives.length === 0 ? (
        <EmptyState message={t('empty.archives')} />
      ) : (
        <>
          <div className="hidden md:block bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                {/* No sortable headers here, unlike the package table: sorting
                    would only order the pages already walked, which reads as
                    "sorted" and is not. The walk is key-ordered. */}
                <tr className="border-b border-slate-700/50">
                  <th className="px-4 py-3 text-start text-xs font-medium uppercase tracking-wider text-slate-400">{t('columns.repository')}</th>
                  <th className="px-4 py-3 text-start text-xs font-medium uppercase tracking-wider text-slate-400">{t('columns.commit')}</th>
                  <th className="px-4 py-3 text-start text-xs font-medium uppercase tracking-wider text-slate-400">{t('columns.paths')}</th>
                  <th className="px-4 py-3 text-start text-xs font-medium uppercase tracking-wider text-slate-400">{t('columns.size')}</th>
                  <th className="px-4 py-3 text-start text-xs font-medium uppercase tracking-wider text-slate-400">{t('columns.written')}</th>
                  <th className="text-end px-4 py-3 text-slate-400 font-medium">{t('columns.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {archives.map((entry) => (
                  <tr key={entry.id} className="border-b border-slate-700/30 last:border-0">
                    <td className="px-4 py-3 text-slate-200 font-mono break-all">{entry.attributes.owner}/{entry.attributes.repo}</td>
                    <td className="px-4 py-3 text-slate-400 font-mono text-xs">{entry.attributes.sha.slice(0, 8)}</td>
                    <td className="px-4 py-3">{paths(entry.attributes['paths-hash'])}</td>
                    <td className="px-4 py-3 text-slate-400">{formatBytes(entry.attributes.size, locale)}</td>
                    <td className="px-4 py-3 text-slate-400 text-xs">{formatDate(entry.attributes['last-modified'] || null)}</td>
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

          <MobileCardList>
            {archives.map((entry) => (
              <MobileCard
                key={entry.id}
                title={<span className="font-mono text-sm text-slate-200 break-all">{entry.attributes.owner}/{entry.attributes.repo}</span>}
                fields={[
                  { label: t('columns.commit'), value: entry.attributes.sha.slice(0, 8) },
                  { label: t('columns.paths'), value: paths(entry.attributes['paths-hash']) },
                  { label: t('columns.size'), value: formatBytes(entry.attributes.size, locale) },
                  { label: t('columns.written'), value: formatDate(entry.attributes['last-modified'] || null) },
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

          {/* The cursor IS the next-page affordance. A non-empty one means the
              walk stopped early; an empty one means this is the whole cache. */}
          <div className="mt-3">
            {cursor ? (
              <button
                onClick={() => fetchPage(cursor, true)}
                disabled={loadingPage}
                data-testid="archive-load-more"
                className="min-h-11 px-4 py-2 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 disabled:opacity-50 text-slate-200 transition-colors"
              >
                {loadingPage ? t('archives.loadingMore') : t('archives.loadMore')}
              </button>
            ) : (
              loaded && (
                <p data-testid="archive-end-of-list" className="text-xs text-slate-500">
                  {t('archives.listedAll', { count: archives.length })}
                </p>
              )
            )}
          </div>
        </>
      )}
    </section>
  )
}
