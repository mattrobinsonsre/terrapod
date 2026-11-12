'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslations } from 'next-intl'

import { apiFetch, parseApiError } from '@/lib/api'
import { HintWithDetail } from '@/components/hint-with-detail'
import { useConfirm } from '@/lib/use-confirm'
import { parseWarmLine, type ParsedWarmItem } from '@/lib/warm-line'

/**
 * Warming — filling a cache before anything asks it for something — for every
 * artifact kind Terrapod caches (#2486).
 *
 * The two binary caches have had a warm control since they existed, at the top
 * of this page. The package caches and the container mirror did not: warming
 * them was an API call, and for four of the six ecosystems there was no warm
 * adapter at all. That is the gap this closes, and it matters most in exactly
 * the topology the feature exists for — you fill the caches while you can still
 * reach upstream, then seal the node.
 *
 * Three properties the issue asks for, and each shapes the markup more than it
 * looks like it should:
 *
 * **Explicit items at explicit versions.** The textarea takes one artifact per
 * line and the preview below it says, before anything is submitted, how many
 * lines carry a version and how many do not. An unversioned line stays legal
 * where the registry has a meaningful newest, because that is a real
 * convenience — but it is never the only way to ask, and the preview makes a
 * submission of them visibly different from a pinned one rather than silently
 * equivalent.
 *
 * **Where it will pull from.** `GET /admin/package-cache/sources` is loaded
 * before the form is usable, so the effective upstream is shown as a URL rather
 * than implied, and any mirror the operator has declared is selectable per
 * submission. Without this the whole of the feedback was that bytes arrived
 * from somewhere.
 *
 * **Say what will happen, then what happened.** The job is asynchronous, so the
 * submit button hands back a job id and this polls it. The result list
 * distinguishes three outcomes that a success count collapses into one:
 * fetched, already cached, and failed with the reason.
 */

/** The six package ecosystems plus the container mirror, which is warmable and
 *  is not a package cache — it takes an image reference rather than a name and
 *  a version, which is why the form branches on it. */
const OCI = 'oci'

interface SourceInfo {
  ecosystem: string
  enabled: boolean
  upstream: string
  mirrors: Record<string, string>
  'resolves-latest': boolean
}

interface Outcome {
  ecosystem: string
  ref: string
  ok: boolean
  detail: string
  files: number
  fetched: number
  source: string
}

interface JobState {
  status: string
  total: number
  completed: number
  succeeded: number
  failed: number
  outcomes: Outcome[]
}

export function CacheWarmPanel({
  onError,
  onSuccess,
}: {
  onError: (message: string) => void
  onSuccess: (message: string) => void
}) {
  const t = useTranslations('adminBinaryCache')
  const { confirmTouchMutation } = useConfirm()

  const [open, setOpen] = useState(false)
  const [sources, setSources] = useState<SourceInfo[]>([])
  const [maxItems, setMaxItems] = useState(0)
  const [sealed, setSealed] = useState(false)
  // Whether the surface exists at all. The package cache is an optional
  // capability and its routes go with it, so a 404 here means it is switched
  // off rather than that something is broken.
  const [absent, setAbsent] = useState(false)

  const [ecosystem, setEcosystem] = useState('galaxy')
  const [source, setSource] = useState('')
  const [text, setText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [job, setJob] = useState<JobState | null>(null)
  const [jobId, setJobId] = useState('')

  const loadSources = useCallback(async () => {
    try {
      const res = await apiFetch('/api/v1/admin/package-cache/sources')
      if (res.status === 404) {
        setAbsent(true)
        return
      }
      if (!res.ok) throw new Error(await parseApiError(res, t('packages.errorBulk')))
      const data = await res.json()
      setSources(data.data.map((e: { attributes: SourceInfo }) => e.attributes))
      setMaxItems(data.meta?.['max-items'] ?? 0)
      setSealed(Boolean(data.meta?.sealed))
    } catch (err) {
      onError(err instanceof Error ? err.message : t('warmAhead.errorSources'))
    }
  }, [onError, t])

  useEffect(() => {
    if (open && sources.length === 0 && !absent) void loadSources()
  }, [open, sources.length, absent, loadSources])

  const selected = useMemo(
    () => sources.find((s) => s.ecosystem === ecosystem),
    [sources, ecosystem],
  )

  // Reset the source whenever the ecosystem changes: a label belongs to one
  // ecosystem's mirror map, so carrying it across would submit a label the new
  // ecosystem has never declared and earn a 422 the operator did not ask for.
  useEffect(() => setSource(''), [ecosystem])

  const items = useMemo(
    () =>
      text
        .split('\n')
        .map(parseWarmLine)
        .filter((i): i is ParsedWarmItem => i !== null),
    [text],
  )

  const isOci = ecosystem === OCI
  const needsVersion = selected ? !selected['resolves-latest'] : false
  const unversioned = isOci ? [] : items.filter((i) => !i.version)
  const blocked = needsVersion && unversioned.length > 0
  const tooMany = maxItems > 0 && items.length > maxItems

  /**
   * The URL this submission will actually pull from.
   *
   * Shown rather than implied. For `oci` there is no single answer — each
   * reference's own first path component names its host — so the form says that
   * instead of showing a blank.
   */
  const effectiveUpstream = isOci
    ? ''
    : source
      ? (selected?.mirrors?.[source] ?? '')
      : (selected?.upstream ?? '')

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  useEffect(() => () => void (pollRef.current && clearInterval(pollRef.current)), [])

  const poll = useCallback(
    async (id: string) => {
      try {
        const res = await apiFetch(`/api/v1/admin/warm-jobs/${encodeURIComponent(id)}`)
        if (!res.ok) throw new Error(await parseApiError(res, t('warmAhead.errorStatus')))
        const data = await res.json()
        const state: JobState = data.data.attributes
        setJob(state)
        if (state.status === 'finished' && pollRef.current) {
          clearInterval(pollRef.current)
          pollRef.current = null
          setSubmitting(false)
          onSuccess(
            t('warmAhead.finished', {
              succeeded: state.succeeded,
              total: state.total,
              failed: state.failed,
            }),
          )
        }
      } catch (err) {
        if (pollRef.current) clearInterval(pollRef.current)
        pollRef.current = null
        setSubmitting(false)
        onError(err instanceof Error ? err.message : t('warmAhead.errorStatus'))
      }
    },
    [onError, onSuccess, t],
  )

  async function handleWarm() {
    if (items.length === 0) {
      onError(t('warmAhead.errorEmpty'))
      return
    }
    if (!confirmTouchMutation(t('warmAhead.confirm', { count: items.length }))) return

    setJob(null)
    setJobId('')
    setSubmitting(true)
    try {
      const path = isOci ? '/api/v1/admin/oci/warm' : '/api/v1/admin/package-cache/warm'
      const body = isOci
        ? { images: items.map((i) => (i.version ? `${i.name}:${i.version}` : i.name)) }
        : {
            packages: items.map((i) => ({
              ecosystem,
              name: i.name,
              version: i.version,
              source,
              platforms: i.platforms,
            })),
          }
      const res = await apiFetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (res.status === 409) {
        // The one refusal worth distinguishing by hand. "0 succeeded" on a
        // sealed node looks exactly like a set of missing packages, and would
        // send an operator looking upstream for a problem on this side.
        setSealed(true)
        throw new Error(await parseApiError(res, t('warmAhead.sealed')))
      }
      if (!res.ok) throw new Error(await parseApiError(res, t('warmAhead.errorSubmit')))
      const data = await res.json()
      const id: string = data.data.id
      setJobId(id)
      await poll(id)
      pollRef.current = setInterval(() => void poll(id), 2000)
    } catch (err) {
      setSubmitting(false)
      onError(err instanceof Error ? err.message : t('warmAhead.errorSubmit'))
    }
  }

  return (
    <div
      data-testid="cache-warm-section"
      className="mt-8 bg-slate-800/50 rounded-lg border border-slate-700/50"
    >
      <button
        data-testid="cache-warm-toggle"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between px-4 py-3 text-start text-slate-200 font-semibold"
        aria-expanded={open}
      >
        <span>{t('warmAhead.heading')}</span>
        <span className="text-slate-400 text-sm">
          {open ? t('warm.hide') : t('warm.prePopulate')}
        </span>
      </button>

      {open && (
        <div className="px-4 pb-4 space-y-4 border-t border-slate-700/50 pt-4">
          {absent ? (
            <p className="text-sm text-slate-400">{t('packages.capabilityOff')}</p>
          ) : (
            <>
              {/* Two-tier, per the house rule for a hint at or over 250
                  characters (#2413 F15): what the control does stays on
                  screen, and the reasoning — why a version is worth naming,
                  and the one ecosystem whose artifacts are per-platform —
                  sits behind the disclosure rather than as a wall of text
                  above the form. `HintWithDetail` directly rather than
                  `tieredHint`, because the detail carries `<code>` spans and
                  that helper takes a plain-string `t`. */}
              <HintWithDetail
                lead={t('warmAhead.helpLead')}
                detail={t.rich('warmAhead.help', {
                  code: (chunks) => <code className="text-slate-300">{chunks}</code>,
                })}
              />

              {sealed && (
                <p className="p-3 rounded-lg bg-amber-900/30 border border-amber-800/50 text-sm text-amber-300">
                  {t('warmAhead.sealed')}
                </p>
              )}

              <div className="grid gap-4 sm:grid-cols-2">
                <div className="min-w-0">
                  <label
                    htmlFor="warm-ecosystem"
                    className="block text-sm text-slate-300 mb-1"
                  >
                    {t('warmAhead.ecosystemLabel')}
                  </label>
                  <select
                    id="warm-ecosystem"
                    value={ecosystem}
                    onChange={(e) => setEcosystem(e.target.value)}
                    className="w-full px-3 py-2 rounded-lg bg-slate-900/70 border border-slate-700 text-sm text-slate-200"
                  >
                    {sources.map((s) => (
                      <option key={s.ecosystem} value={s.ecosystem} disabled={!s.enabled}>
                        {s.enabled
                          ? s.ecosystem
                          : t('warmAhead.ecosystemOff', { ecosystem: s.ecosystem })}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="min-w-0">
                  <label htmlFor="warm-source" className="block text-sm text-slate-300 mb-1">
                    {t('warmAhead.sourceLabel')}
                  </label>
                  <select
                    id="warm-source"
                    value={source}
                    onChange={(e) => setSource(e.target.value)}
                    disabled={isOci}
                    className="w-full px-3 py-2 rounded-lg bg-slate-900/70 border border-slate-700 text-sm text-slate-200 disabled:opacity-50"
                  >
                    <option value="">{t('warmAhead.sourceDefault')}</option>
                    {Object.keys(selected?.mirrors ?? {}).map((label) => (
                      <option key={label} value={label}>
                        {label}
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              {/* The whole point of requirement two: an operator warming from
                  here could not see which upstream a submission would reach. */}
              <p data-testid="cache-warm-upstream" className="text-xs text-slate-500 break-all">
                {isOci
                  ? t('warmAhead.upstreamPerReference')
                  : t.rich('warmAhead.upstreamIs', {
                      url: effectiveUpstream,
                      code: (chunks) => <code className="text-slate-400">{chunks}</code>,
                    })}
              </p>

              <div>
                <label htmlFor="warm-items" className="block text-sm text-slate-300 mb-1">
                  {isOci ? t('warmAhead.imagesLabel') : t('warmAhead.itemsLabel')}
                </label>
                <textarea
                  data-testid="cache-warm-items"
                  id="warm-items"
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  rows={5}
                  spellCheck={false}
                  placeholder={
                    isOci
                      ? 'quay.io/ansible/awx-ee:24.6.1'
                      : ecosystem === 'pulumi'
                        ? 'aws 6.0.0 linux/amd64 linux/arm64'
                        : 'community.general 9.2.0'
                  }
                  className="w-full px-3 py-2 rounded-lg bg-slate-900/70 border border-slate-700 text-sm text-slate-200 font-mono"
                />
              </div>

              {/* Say what will happen before it happens. */}
              {items.length > 0 && (
                <div data-testid="cache-warm-preview" className="text-sm space-y-1">
                  <p className="text-slate-300">
                    {t('warmAhead.willWarm', { count: items.length })}
                  </p>
                  {!isOci && unversioned.length > 0 && (
                    <p className={blocked ? 'text-red-400' : 'text-amber-400'}>
                      {blocked
                        ? t('warmAhead.versionRequired', {
                            ecosystem,
                            count: unversioned.length,
                          })
                        : t('warmAhead.unversioned', { count: unversioned.length })}
                    </p>
                  )}
                  {tooMany && (
                    <p className="text-red-400">{t('warmAhead.tooMany', { max: maxItems })}</p>
                  )}
                </div>
              )}

              <button
                data-testid="cache-warm-submit"
                onClick={handleWarm}
                disabled={submitting || sealed || blocked || tooMany || items.length === 0}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-violet-900/40 hover:bg-violet-900/60 disabled:opacity-50 text-violet-300 transition-colors"
              >
                {submitting ? t('warmAhead.warming') : t('warmAhead.warm')}
              </button>

              {/* …and what happened. */}
              {job && (
                <div className="space-y-2">
                  {/* The counts only. `job.status` is a SERVER ENUM --
                      `queued`, `running`, `finished` -- so interpolating it
                      here put an English word inside an otherwise-translated
                      sentence, and no catalogue could fix it because the word
                      never passed through next-intl at all. The state is
                      already said twice in words that ARE translated: the
                      button below reads "Warming…" while this runs, and the
                      success banner says what landed when it finishes. */}
                  <p className="text-sm text-slate-300">
                    {t('warmAhead.progress', {
                      completed: job.completed,
                      total: job.total,
                    })}
                    {jobId ? ` — ${jobId}` : ''}
                  </p>
                  {job.outcomes.length > 0 && (
                    <ul className="space-y-1 text-sm">
                      {job.outcomes.map((o, i) => (
                        <li key={i} className={o.ok ? 'text-green-400' : 'text-red-400'}>
                          {o.ok ? '✓' : '✗'} <span className="text-slate-300">{o.ref}</span>
                          {o.ok ? (
                            <span className="text-slate-500">
                              {' — '}
                              {/* Three outcomes, not two. "Already cached" is
                                  what tells an operator a re-run converged. */}
                              {o.fetched === 0 && o.files > 0
                                ? t('warmAhead.alreadyCached', { files: o.files })
                                : t('warmAhead.fetched', { fetched: o.fetched })}
                              {o.detail ? ` — ${o.detail}` : ''}
                            </span>
                          ) : (
                            <span>{o.detail ? ` — ${o.detail}` : ''}</span>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}
