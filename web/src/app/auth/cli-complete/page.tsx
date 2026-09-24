'use client'

import { Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { useTranslations } from 'next-intl'

type Status = 'delivering' | 'polling' | 'complete' | 'timeout' | 'fallback'

// How long to wait for the CLI to confirm it has the code.
//
// Only reached when the fetch RESOLVED but no confirmation followed -- a
// browser that blocks the call to 127.0.0.1 rejects immediately, and that path
// navigates instead of waiting. So this is the "delivered, but the CLI has not
// said so" case, where some patience is warranted and 20s is generous for a
// call to the user's own machine.
//
// The old value was 60s against a 60s AUTH_CODE_TTL, so anyone who reached the
// fallback was handed a code that had already expired -- the manual path could
// not work at all, by arithmetic rather than by race. Keep this well under
// that TTL; `test_cli_login_timing.py` enforces the margin.
const POLL_TIMEOUT_MS = 20_000

function CliCompleteInner() {
  const t = useTranslations('cliComplete')
  const params = useSearchParams()
  const code = params.get('code') ?? ''
  const state = params.get('state') ?? ''
  const redirectUri = params.get('redirect_uri') ?? ''

  const [status, setStatus] = useState<Status>('delivering')
  const [fetchFailed, setFetchFailed] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const startRef = useRef(0)
  const deliveredRef = useRef(false)

  const localhostUrl = `${redirectUri}?code=${code}&state=${state}`

  // Step 1: deliver the code to the CLI's local listener.
  //
  // This is a subresource request from an HTTPS page to http://127.0.0.1, so
  // it is mixed content. Chromium exempts it -- the Secure Contexts spec makes
  // 127.0.0.1 and localhost "potentially trustworthy origins" -- but WebKit
  // does not implement that carve-out, so on Safari the fetch is blocked and
  // rejects. A top-level NAVIGATION to the same URL is not subresource
  // content and is allowed in every browser, which is why the manual link
  // worked where the automatic delivery did not.
  //
  // So a rejection is not a dead end: navigate, and the CLI gets its code with
  // no user action at all. The fetch stays as the fast path because it keeps
  // the user on this page for the success state; the navigation is the
  // fallback that always works.
  useEffect(() => {
    if (!code || !redirectUri || deliveredRef.current) return
    deliveredRef.current = true

    fetch(localhostUrl, { mode: 'no-cors' })
      .then(() => {
        setStatus('polling')
      })
      .catch(() => {
        // Blocked or unreachable. We know delivery failed -- an opaque
        // success resolves -- so there is nothing to poll for. Hand off
        // immediately rather than spending the auth code's whole lifetime
        // waiting for something that cannot arrive.
        setFetchFailed(true)
        // Keep polling as well. The navigation below is what actually
        // delivers, but if the browser declines or defers it the user must
        // still end up somewhere useful rather than stranded on "delivering"
        // forever -- after POLL_TIMEOUT_MS this becomes the manual link.
        setStatus('polling')
        // eslint-disable-next-line @next/next/no-location-assign-relative-destination -- localhostUrl is EXTERNAL (the terraform CLI's local callback listener), not an internal route. A router push would not leave the origin, and leaving it is the point: a top-level navigation is not subresource content, so it is not mixed-content blocked.
        window.location.href = localhostUrl
      })
  }, [code, redirectUri, localhostUrl])

  // Step 2: poll for completion
  useEffect(() => {
    if (status !== 'polling' || !code) return

    startRef.current = Date.now()

    const check = async () => {
      try {
        const res = await fetch(`/api/terrapod/v1/auth/cli-login-status?code=${encodeURIComponent(code)}`)
        if (res.ok) {
          const data = await res.json()
          if (data.complete) {
            setStatus('complete')
            if (pollRef.current) clearInterval(pollRef.current)
            return
          }
        }
      } catch {
        // ignore poll errors
      }

      if (Date.now() - startRef.current > POLL_TIMEOUT_MS) {
        setStatus(fetchFailed ? 'fallback' : 'timeout')
        if (pollRef.current) clearInterval(pollRef.current)
      }
    }

    check()
    pollRef.current = setInterval(check, 2000)

    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [status, code, fetchFailed])

  const handleManualRedirect = useCallback(() => {
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination -- localhostUrl is EXTERNAL (the terraform CLI's local callback listener), not an internal route.
    window.location.href = localhostUrl
  }, [localhostUrl])

  if (!code || !redirectUri) {
    return (
      <main className="min-h-screen flex items-center justify-center p-4">
        <div className="w-full max-w-md text-center">
          <h1 className="text-xl font-bold mb-2">{t('invalid.title')}</h1>
          <p className="text-slate-400">{t('invalid.description')}</p>
        </div>
      </main>
    )
  }

  return (
    <main className="min-h-screen flex items-center justify-center p-4">
      <div className="w-full max-w-md text-center">
        {/* Logo */}
        <div className="mb-6">
          <svg className="mx-auto h-12 w-12 text-brand-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 2L2 7l10 5 10-5-10-5z" />
            <path d="M2 17l10 5 10-5" />
            <path d="M2 12l10 5 10-5" />
          </svg>
        </div>

        {status === 'complete' ? (
          <>
            {/* Green checkmark */}
            <div className="mb-4">
              <svg className="mx-auto h-16 w-16 text-green-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
                <polyline points="22 4 12 14.01 9 11.01" />
              </svg>
            </div>
            <h1 className="text-2xl font-bold mb-2">{t('success.title')}</h1>
            <p className="text-slate-400">{t('success.complete')}</p>
          </>
        ) : status === 'fallback' ? (
          <>
            <h1 className="text-2xl font-bold mb-2">{t('success.title')}</h1>
            <p className="text-slate-400 mb-4">{t('fallback.description')}</p>
            <button
              onClick={handleManualRedirect}
              className="bg-brand-600 hover:bg-brand-500 text-white font-medium py-2 px-6 rounded-lg transition-colors inline-block btn-smoke"
            >
              {t('fallback.button')}
            </button>
          </>
        ) : status === 'timeout' ? (
          <>
            <h1 className="text-2xl font-bold mb-2">{t('success.title')}</h1>
            <p className="text-slate-400">{t('timeout.description')}</p>
          </>
        ) : (
          <>
            <h1 className="text-2xl font-bold mb-2">{t('success.title')}</h1>
            {/* Spinner */}
            <div className="flex items-center justify-center gap-2 text-slate-400">
              <svg className="animate-spin h-5 w-5" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              <span>{t('completing')}</span>
            </div>
          </>
        )}
      </div>
    </main>
  )
}

function CliCompleteFallback() {
  const t = useTranslations('cliComplete')
  return (
    <main className="min-h-screen flex items-center justify-center">
      <div className="text-center">
        <p className="text-slate-500">{t('loading')}</p>
      </div>
    </main>
  )
}

export default function CliCompletePage() {
  return (
    <Suspense fallback={<CliCompleteFallback />}>
      <CliCompleteInner />
    </Suspense>
  )
}
