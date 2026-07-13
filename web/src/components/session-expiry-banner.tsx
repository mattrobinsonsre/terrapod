'use client'

import { useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { getAuthState, getExpiresAt, updateExpiresAt } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

const WARNING_THRESHOLD_MS = 5 * 60 * 1000 // 5 minutes
const TICK_MS = 30_000 // re-render the countdown every 30s
const POLL_MS = 60_000 // reconcile against the server every 60s

/**
 * Session-expiry warning banner (#726).
 *
 * The warning must reflect the SERVER's true remaining session TTL, not a
 * stale client-cached expiry. SSE-driven views slide the server session
 * (each `authenticate_request`) without emitting the `X-Session-Expires`
 * header, so the local `expiresAt` in localStorage drifts stale and the old
 * banner warned (and redirected) falsely — a reload always cleared it, and
 * the user never actually had to log back in.
 *
 * Fix: poll a lightweight, non-sliding status endpoint and reconcile the
 * local expiry from the server's real `ttl_seconds` (applied to the client's
 * own clock, so absolute clock skew can't matter). Logout is now
 * server-authoritative: `apiFetch` clears auth + redirects on a genuine 401
 * from the poll. The local countdown only DISPLAYS the warning — it never
 * triggers a redirect on its own.
 */
export function SessionExpiryBanner() {
  const t = useTranslations('common')
  const [showWarning, setShowWarning] = useState(false)
  const [remainingMinutes, setRemainingMinutes] = useState(0)

  useEffect(() => {
    let cancelled = false

    // Render the amber warning from the (reconciled) local expiry. Never
    // redirects — the server poll owns logout.
    const render = () => {
      if (cancelled) return
      const exp = getExpiresAt()
      if (!exp) {
        setShowWarning(false)
        return
      }
      const ms = exp.getTime() - Date.now()
      if (ms > 0 && ms < WARNING_THRESHOLD_MS) {
        const mins = Math.ceil(ms / 60_000)
        setShowWarning(true)
        setRemainingMinutes(mins)
      } else {
        setShowWarning(false)
      }
    }

    // Reconcile the local expiry against the server's true remaining TTL.
    // apiFetch handles a genuine 401 (clear auth + redirect) — the only path
    // that logs the user out — and re-syncs on success. A transient network
    // blip is swallowed; the next poll re-syncs, so we never log out on a blip.
    const reconcile = async () => {
      if (cancelled || !getAuthState()) return
      try {
        const res = await apiFetch('/api/terrapod/v1/auth/session')
        if (cancelled || !res.ok) return
        const data = await res.json().catch(() => null)
        if (data && typeof data.ttl_seconds === 'number') {
          updateExpiresAt(new Date(Date.now() + data.ttl_seconds * 1000).toISOString())
        }
      } catch {
        // transient — leave the local expiry as-is; the next poll re-syncs
      }
      render()
    }

    render()
    reconcile()
    const tick = setInterval(render, TICK_MS)
    const poll = setInterval(reconcile, POLL_MS)
    // Re-sync promptly when the user returns to a backgrounded tab — a hidden
    // tab's timers are throttled, so its local expiry may be well out of date.
    const onVisible = () => {
      if (document.visibilityState === 'visible') reconcile()
    }
    document.addEventListener('visibilitychange', onVisible)

    return () => {
      cancelled = true
      clearInterval(tick)
      clearInterval(poll)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])

  if (!showWarning) return null

  return (
    <div className="bg-amber-900/50 border-b border-amber-700/50 px-4 py-2 text-sm text-amber-200 text-center">
      {t.rich('sessionExpiry.banner', {
        minutes: remainingMinutes,
        link: (chunks) => (
          <a href="/login" className="underline hover:text-amber-100">
            {chunks}
          </a>
        ),
      })}
    </div>
  )
}
