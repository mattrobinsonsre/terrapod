'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { getAuthState } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { useFormat } from '@/lib/format'

// A Slack binding is a standing ability to act as this account from Slack, and
// until now there was nowhere to see one — which is half of why
// GHSA-5899-fm2p-88x3 was worth a finding rather than a note: a victim phished
// into binding an attacker's Slack identity could not notice it had happened, let
// alone undo it. `/terrapod unlink` only ever removes the CALLER's own binding
// from their own Slack, which is exactly the wrong end for a victim.
//
// Read-only plus revoke, deliberately: there is nothing here to edit, and the
// endpoints have always existed (`GET /slack/links`, `DELETE /slack/links/{id}`).

interface SlackLink {
  id: string
  'slack-team-id': string
  'slack-user-id': string
  email: string
  'linked-via': string
  'linked-at': string
}

export default function SlackLinksPage() {
  const router = useRouter()
  const t = useTranslations('settings')
  const fmt = useFormat()
  const [links, setLinks] = useState<SlackLink[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!getAuthState()) {
      router.push('/login')
      return
    }
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- initial mount load only
  }, [router])

  async function load() {
    try {
      const res = await apiFetch('/api/v1/slack/links')
      if (!res.ok) throw new Error(t('slackLinks.errors.load'))
      const data = await res.json()
      setLinks(data?.data ?? [])
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : t('slackLinks.errors.load'))
    } finally {
      setLoading(false)
    }
  }

  async function revoke(link: SlackLink) {
    // Both pointer modes, not touch only: this deletes a record, and a stray
    // click losing it is a desktop hazard too.
    if (!window.confirm(t('slackLinks.confirmRevoke', { user: link['slack-user-id'] }))) return
    try {
      const res = await apiFetch(`/api/v1/slack/links/${link.id}`, { method: 'DELETE' })
      if (!res.ok && res.status !== 204) throw new Error(t('slackLinks.errors.revoke'))
      setLinks((prev) => prev.filter((l) => l.id !== link.id))
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : t('slackLinks.errors.revoke'))
    }
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-4xl mx-auto">
        <PageHeader title={t('slackLinks.title')} description={t('slackLinks.description')} />

        {error && <ErrorBanner message={error} />}

        {loading ? (
          <LoadingSpinner />
        ) : links.length === 0 ? (
          <EmptyState message={t('slackLinks.empty')} />
        ) : (
          <>
            {/* Desktop: the table. Mobile: cards from the same data — the Slack
                user id is the primary signal here, so it is never the thing that
                gets dropped at a narrow width. */}
            <div className="hidden md:block bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-700/50">
                    <th className="text-start px-4 py-3 text-slate-400 font-medium">
                      {t('slackLinks.columns.slackUser')}
                    </th>
                    <th className="text-start px-4 py-3 text-slate-400 font-medium">
                      {t('slackLinks.columns.team')}
                    </th>
                    <th className="text-start px-4 py-3 text-slate-400 font-medium">
                      {t('slackLinks.columns.linkedAt')}
                    </th>
                    <th className="px-4 py-3" />
                  </tr>
                </thead>
                <tbody>
                  {links.map((l) => (
                    <tr key={l.id} className="border-b border-slate-700/30 last:border-0">
                      <td className="px-4 py-3 text-slate-200 font-mono text-xs">
                        {l['slack-user-id']}
                      </td>
                      <td className="px-4 py-3 text-slate-400 font-mono text-xs">
                        {l['slack-team-id']}
                      </td>
                      <td className="px-4 py-3 text-slate-400 text-xs">
                        {fmt.dateTime(l['linked-at'])}
                      </td>
                      <td className="px-4 py-3 text-end">
                        <button
                          onClick={() => revoke(l)}
                          className="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors"
                        >
                          {t('slackLinks.revoke')}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <ul className="md:hidden space-y-3">
              {links.map((l) => (
                <li
                  key={l.id}
                  className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4"
                >
                  <div className="font-mono text-xs text-slate-200 break-all">
                    {l['slack-user-id']}
                  </div>
                  <div className="mt-1 font-mono text-xs text-slate-400 break-all">
                    {l['slack-team-id']}
                  </div>
                  <div className="mt-1 text-xs text-slate-400">{fmt.dateTime(l['linked-at'])}</div>
                  <button
                    onClick={() => revoke(l)}
                    className="mt-3 w-full px-3 py-2 rounded-lg text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors"
                  >
                    {t('slackLinks.revoke')}
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </main>
    </>
  )
}
