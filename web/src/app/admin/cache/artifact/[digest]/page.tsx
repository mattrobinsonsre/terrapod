'use client'

import { useTranslations } from 'next-intl'
import Link from 'next/link'
import { useParams } from 'next/navigation'
import { useEffect, useState } from 'react'
import type { JSX } from 'react'

import { VerificationBadge } from '@/components/supply-chain-indicators'
import { apiFetch } from '@/lib/api'

/** One artifact's full record (#2589 step 2e).
 *
 * **A route, not a modal.** A reviewer wants to send a colleague at one
 * artifact, so this has to survive reload, back and deep-link — tab and view
 * state is the URL's job here. Drilling down to a route is also the
 * mobile-first answer: prefer a route over cramming a panel into a dense row.
 *
 * **Addressed by digest**, which is the artifact's own identity rather than a
 * row id that differs between the three caches holding the same bytes.
 */

interface LedgerEntry {
  attributes: {
    digest: string
    kind: string
    coordinates: Record<string, string>[]
    attestation: string | null
    attestor: Record<string, string>
    verification: string
    'admitted-at': string
    'supply-chain': {
      state: string
      reason: string | null
      findings: number
      critical: number
      malicious: number
      verdict: Record<string, unknown> | null
    }
    packages: number
    sbom: Record<string, unknown> | null
    matches: {
      id: string
      package: string
      installed: string
      fixed: string | null
      severity: string
      malicious: boolean
    }[]
    'verdict-provenance': Record<string, unknown>
    'updated-at': string
  }
}

export default function ArtifactDetailPage(): JSX.Element {
  // The digest comes from the URL, which is what makes this linkable at all.
  const params = useParams<{ digest: string }>()
  const digest = decodeURIComponent(String(params?.digest ?? ''))
  const t = useTranslations('supplyChain')

  const [entry, setEntry] = useState<LedgerEntry | null>(null)
  const [missing, setMissing] = useState(false)

  useEffect(() => {
    // The ignore flag is the canonical shape: a digest changed in the URL while
    // the first request is still in flight must not have its answer painted
    // over the second one's.
    let ignore = false
    async function load(): Promise<void> {
      const res = await apiFetch(
        `/api/v1/admin/artifact-ledger/${encodeURIComponent(digest)}`,
      )
      if (ignore) return
      if (!res.ok) {
        setMissing(true)
        return
      }
      setEntry(((await res.json()) as { data: LedgerEntry }).data)
    }
    void load()
    return () => {
      ignore = true
    }
  }, [digest])

  if (missing || !entry) {
    return (
      <main className="mx-auto max-w-5xl px-4 py-8">
        <Link href="/admin/binary-cache" className="text-sm text-brand-400 hover:underline">
          {t('detail.back')}
        </Link>
        {missing ? <p className="mt-6 text-slate-300">{t('detail.notFound')}</p> : null}
      </main>
    )
  }

  const a = entry.attributes
  const supply = a['supply-chain']
  const prov = a['verdict-provenance'] as Record<string, string | Record<string, string>>

  return (
    <main className="mx-auto max-w-5xl px-4 py-8">
      <Link href="/admin/binary-cache" className="text-sm text-brand-400 hover:underline">
        {t('detail.back')}
      </Link>

      <h1 className="mt-4 text-xl font-semibold text-slate-100 break-all">
        {t('detail.title')}
      </h1>
      <p className="mt-1 font-mono text-xs text-slate-400 break-all">{a.digest}</p>

      <section className="mt-6 flex flex-wrap items-center gap-3">
        <VerificationBadge
          state={a.verification}
          label={t(`verification.${a.verification}` as 'verification.verified')}
        />
        <span className="text-xs text-slate-400">
          {t('detail.admitted')}: {a['admitted-at']}
        </span>
        {a.attestor?.uploaded_by ? (
          <span className="text-xs text-slate-400">
            {t('detail.attestor')}: {a.attestor.uploaded_by}
          </span>
        ) : null}
        {a.attestor?.publisher ? (
          <span className="text-xs text-slate-400">
            {t('detail.attestor')}: {a.attestor.publisher}
          </span>
        ) : null}
      </section>

      {a.coordinates.length > 0 && (
        <section className="mt-6">
          <h2 className="text-sm font-medium text-slate-300">{t('detail.coordinates')}</h2>
          <ul className="mt-2 space-y-1">
            {a.coordinates.map((c, i) => (
              <li key={i} className="font-mono text-xs text-slate-400 break-all">
                {Object.entries(c)
                  .filter(([k]) => k !== 'kind')
                  .map(([k, v]) => `${k}=${v}`)
                  .join('  ')}
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Every verdict is dated. A clean result with no date is worthless; with
          the epoch and the tool versions that produced it, it is evidence. */}
      <section className="mt-6">
        <h2 className="text-sm font-medium text-slate-300">{t('detail.verdict')}</h2>
        {prov?.matcher ? (
          <p className="mt-2 text-xs text-slate-400">
            {t('detail.verdictLine', {
              matcher: String(prov.matcher),
              matcherVersion: String(prov.matcher_version ?? ''),
              epoch: String(
                (prov.db as Record<string, string>)?.updated_at ??
                  (prov.db as Record<string, string>)?.version ??
                  '-',
              ),
              generator: String(prov.sbom_generator ?? ''),
              generatorVersion: String(prov.sbom_generator_version ?? ''),
            })}
          </p>
        ) : (
          /* Three states, never two: this is the "we did not look" case, and it
             must never render as a blank that reads like "nothing found". */
          <p className="mt-2 text-xs text-slate-400">{t('detail.verdictNone')}</p>
        )}
      </section>

      <section className="mt-6">
        <h2 className="text-sm font-medium text-slate-300">
          {t('detail.sbom')} · {t('detail.packages', { count: a.packages })}
        </h2>
        {a.sbom ? (
          <pre className="mt-2 max-h-96 overflow-auto rounded-lg bg-slate-900/60 p-3 text-[0.7rem] text-slate-300">
            {JSON.stringify(a.sbom, null, 2)}
          </pre>
        ) : (
          <p className="mt-2 text-xs text-slate-400">
            {t('detail.sbomNone')}
            {supply.reason ? ` — ${t(`reason.${supply.reason === 'awaiting-inventory' ? 'awaiting' : 'notAdmitted'}` as 'reason.awaiting')}` : ''}
          </p>
        )}
      </section>

      <section className="mt-6">
        <h2 className="text-sm font-medium text-slate-300">{t('detail.findings')}</h2>
        {/* Context over severity. A wall of unactionable criticals makes the
            platform look as though it does not understand its own output, so
            the page says where a finding sits and why it is advisory. */}
        <p className="mt-1 text-xs text-slate-500">{t('detail.advisory')}</p>
        {supply.malicious > 0 && (
          <p className="mt-1 text-xs text-red-300">{t('detail.maliciousNote')}</p>
        )}
        {a.matches.length === 0 ? (
          <p className="mt-2 text-xs text-slate-400">{t('detail.findingsNone')}</p>
        ) : (
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-start text-xs">
              <tbody>
                {a.matches.map((m) => (
                  <tr key={`${m.id}-${m.package}`} className="border-b border-slate-700/30 last:border-0">
                    <td className="py-2 pe-3 font-mono text-slate-300">{m.id}</td>
                    <td className="py-2 pe-3 text-slate-400">{m.severity}</td>
                    <td className="py-2 pe-3 font-mono text-slate-400 break-all">
                      {m.package} {m.installed}
                    </td>
                    <td className="py-2 text-slate-400">{m.fixed ?? '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </main>
  )
}
