'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { apiFetch } from '@/lib/api'
import { getAuthState } from '@/lib/auth'

interface ExpiringToken {
  id: string
  attributes: {
    description: string
    kind: string
    'bound-to': string | null
    'expires-at': string | null
  }
}

// Dismissed per browser-tab session — deliberately not persisted across
// sessions so a genuinely-expiring token re-surfaces next login, but the
// operator isn't nagged on every navigation.
const DISMISS_KEY = 'tp:token_expiry_dismissed'

/**
 * Warns about service tokens nearing expiry. The list is scoped server-side
 * (`/authentication-tokens/expiring`): every user sees their own bound service
 * tokens; admins additionally see unbound (detached) ones. Nobody is warned
 * about other users' bound tokens — so the banner stays signal, not noise.
 */
export function TokenExpiryBanner() {
  const [count, setCount] = useState(0)
  const [days, setDays] = useState<number | null>(null)
  const [dismissed, setDismissed] = useState(false)

  useEffect(() => {
    if (!getAuthState()) return
    // Already dismissed this session — don't even fetch; count stays 0 so the
    // banner renders nothing (no synchronous setState in the effect).
    if (sessionStorage.getItem(DISMISS_KEY)) return
    apiFetch('/api/terrapod/v1/authentication-tokens/expiring')
      .then((r) => (r.ok ? r.json() : { data: [] }))
      .then((d) => {
        const tokens: ExpiringToken[] = d.data || []
        setCount(tokens.length)
        // Compute the soonest-expiry countdown here (in the effect, not during
        // render) so the component stays pure across re-renders.
        const soonest = tokens
          .map((t) => t.attributes['expires-at'])
          .filter((v): v is string => Boolean(v))
          .sort()[0]
        setDays(
          soonest
            ? Math.max(0, Math.ceil((new Date(soonest).getTime() - Date.now()) / 86_400_000))
            : null,
        )
      })
      .catch(() => {})
  }, [])

  if (dismissed || count === 0) return null

  const dismiss = () => {
    sessionStorage.setItem(DISMISS_KEY, '1')
    setDismissed(true)
  }

  return (
    <div className="bg-amber-900/50 border-b border-amber-700/50 px-4 py-2 text-sm text-amber-200 flex items-center justify-center gap-3">
      <span>
        {count} service token{count !== 1 ? 's' : ''} expiring
        {days !== null ? ` within ${days} day${days !== 1 ? 's' : ''}` : ' soon'} —{' '}
        <Link href="/settings/tokens" className="underline hover:text-amber-100">
          review
        </Link>
      </span>
      <button
        onClick={dismiss}
        className="text-amber-400 hover:text-amber-200 transition-colors"
        aria-label="Dismiss token expiry warning"
      >
        ✕
      </button>
    </div>
  )
}
