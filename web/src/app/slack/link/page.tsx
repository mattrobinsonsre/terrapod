'use client'

import { Suspense, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { getAuthState } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

type Status = 'working' | 'success' | 'error'

function SlackLinkInner() {
  const params = useSearchParams()
  const state = params.get('state') || ''
  const [status, setStatus] = useState<Status>('working')
  const [message, setMessage] = useState('Linking your Slack account…')
  const [email, setEmail] = useState('')
  const ran = useRef(false)

  useEffect(() => {
    if (ran.current) return // link state is single-use — never POST it twice
    ran.current = true

    if (!state) {
      setStatus('error')
      setMessage('Missing or invalid link. Run `/terrapod link` in Slack again.')
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
        const res = await apiFetch('/api/terrapod/v1/slack/link', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ state }),
        })
        if (!res.ok) {
          const data = await res.json().catch(() => ({}))
          throw new Error(
            data.detail ||
              'This link is invalid, expired, or already used. Run `/terrapod link` again.',
          )
        }
        const data = await res.json()
        setEmail(data?.data?.email || auth.email)
        setStatus('success')
        setMessage('Your Slack account is now linked to Terrapod.')
      } catch (e) {
        setStatus('error')
        setMessage(e instanceof Error ? e.message : 'Linking failed.')
      }
    })()
  }, [state])

  const tone =
    status === 'success'
      ? 'text-green-400 border-green-800/50 bg-green-900/20'
      : status === 'error'
        ? 'text-red-400 border-red-800/50 bg-red-900/20'
        : 'text-slate-300 border-slate-700/50 bg-slate-800/50'

  return (
    <main className="h-dvh flex items-center justify-center px-4">
      <div className="w-full max-w-md">
        <h1 className="text-xl font-semibold text-slate-100 mb-4 text-center">
          Connect Slack to Terrapod
        </h1>
        <div className={`rounded-lg border p-6 text-sm ${tone}`}>
          <p>{message}</p>
          {status === 'success' && email && (
            <p className="mt-2 text-slate-400">
              Linked as <span className="font-medium text-slate-200">{email}</span>. You can close
              this tab and return to Slack.
            </p>
          )}
        </div>
      </div>
    </main>
  )
}

export default function SlackLinkPage() {
  return (
    <Suspense
      fallback={
        <main className="h-dvh flex items-center justify-center text-slate-400">Loading…</main>
      }
    >
      <SlackLinkInner />
    </Suspense>
  )
}
