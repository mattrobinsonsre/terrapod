'use client'

import { Suspense, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { useTranslations } from 'next-intl'
import { getAuthState } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

// The link is a deliberate act, not an automatic bind on page load: we first
// PREVIEW which Slack identity the signed state would bind (without consuming
// it), show it against the logged-in Terrapod account, and only bind when the
// user clicks Confirm. This is the confused-deputy defence — the protection is
// that binding now requires an explicit, deliberate confirm (never a silent
// bind from merely opening a link) and is always to the *acting* user; showing
// the Slack id is a secondary cue.
type Status = 'loading' | 'confirm' | 'linking' | 'success' | 'error'

function SlackLinkInner() {
  const t = useTranslations('slackLink')
  const params = useSearchParams()
  const state = params.get('state') || ''
  const [status, setStatus] = useState<Status>('loading')
  const [message, setMessage] = useState(t('checking'))
  const [email, setEmail] = useState('')
  const [slackTeam, setSlackTeam] = useState('')
  const [slackUser, setSlackUser] = useState('')
  const ran = useRef(false)

  useEffect(() => {
    if (ran.current) return
    ran.current = true

    if (!state) {
      setStatus('error')
      setMessage(t('errors.missingLink'))
      return
    }

    const auth = getAuthState()
    if (!auth?.token) {
      // Send the user through Terrapod's own login, then back here to finish.
      const back = `/slack/link?state=${encodeURIComponent(state)}`
      window.location.href = `/login?redirect=${encodeURIComponent(back)}`
      return
    }

    ;(async () => {
      try {
        const res = await apiFetch('/api/terrapod/v1/slack/link/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ state }),
        })
        if (!res.ok) throw new Error('preview rejected')
        const data = await res.json()
        setSlackTeam(data?.data?.['slack-team-id'] || '')
        setSlackUser(data?.data?.['slack-user-id'] || '')
        setEmail(data?.data?.email || auth.email)
        setStatus('confirm')
      } catch {
        // Any preview failure (invalid signature, expired, already used) is the
        // same to the user — one friendly message, never a raw server detail.
        setStatus('error')
        setMessage(t('errors.invalidLink'))
      }
    })()
  }, [state, t])

  async function confirmLink() {
    setStatus('linking')
    setMessage(t('linking'))
    try {
      const res = await apiFetch('/api/terrapod/v1/slack/link', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ state }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.invalidLink'))
      }
      const data = await res.json()
      setEmail(data?.data?.email || email)
      setStatus('success')
      setMessage(t('success.message'))
    } catch (e) {
      setStatus('error')
      setMessage(e instanceof Error ? e.message : t('errors.linkingFailed'))
    }
  }

  const tone =
    status === 'success'
      ? 'text-green-400 border-green-800/50 bg-green-900/20'
      : status === 'error'
        ? 'text-red-400 border-red-800/50 bg-red-900/20'
        : 'text-slate-300 border-slate-700/50 bg-slate-800/50'

  return (
    <main className="h-dvh flex items-center justify-center px-4">
      <div className="w-full max-w-md">
        <h1 className="text-xl font-semibold text-slate-100 mb-4 text-center">{t('title')}</h1>
        <div className={`rounded-lg border p-6 text-sm ${tone}`}>
          {status === 'confirm' ? (
            <div>
              <p className="text-slate-300">
                {t.rich('confirm.prompt', {
                  em: (chunks) => <span className="font-medium text-slate-100">{chunks}</span>,
                })}
              </p>
              <dl className="mt-4 space-y-2">
                <div className="flex justify-between gap-4">
                  <dt className="text-xs text-slate-500">{t('confirm.slackUser')}</dt>
                  <dd className="text-slate-200 font-mono text-xs">{slackUser || t('unknown')}</dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt className="text-xs text-slate-500">{t('confirm.slackTeam')}</dt>
                  <dd className="text-slate-200 font-mono text-xs">{slackTeam || t('unknown')}</dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt className="text-xs text-slate-500">{t('confirm.terrapodAccount')}</dt>
                  <dd className="text-slate-200 font-medium">{email}</dd>
                </div>
              </dl>
              <p className="mt-4 text-xs text-amber-400/90">
                {t.rich('confirm.warning', {
                  em: (chunks) => <span className="font-medium">{chunks}</span>,
                })}
              </p>
              <button
                onClick={confirmLink}
                className="mt-4 w-full rounded bg-brand-600 hover:bg-brand-500 px-4 py-2 text-sm font-medium text-white focus:outline-none focus:ring-2 focus:ring-brand-500"
              >
                {t('confirm.button')}
              </button>
            </div>
          ) : (
            <>
              <p>{message}</p>
              {status === 'success' && email && (
                <p className="mt-2 text-slate-400">
                  {t.rich('success.linkedAs', {
                    email,
                    em: (chunks) => <span className="font-medium text-slate-200">{chunks}</span>,
                  })}
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </main>
  )
}

function SlackLinkFallback() {
  const t = useTranslations('slackLink')
  return <main className="h-dvh flex items-center justify-center text-slate-400">{t('loading')}</main>
}

export default function SlackLinkPage() {
  return (
    <Suspense fallback={<SlackLinkFallback />}>
      <SlackLinkInner />
    </Suspense>
  )
}
