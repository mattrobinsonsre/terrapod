'use client'

import { Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'next/navigation'

type Status = 'delivering' | 'polling' | 'complete' | 'timeout' | 'fallback'

function CliCompleteInner() {
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

  // Step 1: deliver code to CLI via fetch (no-cors)
  useEffect(() => {
    if (!code || !redirectUri || deliveredRef.current) return
    deliveredRef.current = true

    fetch(localhostUrl, { mode: 'no-cors' })
      .then(() => {
        setStatus('polling')
      })
      .catch(() => {
        setFetchFailed(true)
        setStatus('polling')
      })
  }, [code, redirectUri, localhostUrl])

  // Step 2: poll for completion
  useEffect(() => {
    if (status !== 'polling' || !code) return

    startRef.current = Date.now()

    const check = async () => {
      try {
        const res = await fetch(`/api/v2/auth/cli-login-status?code=${encodeURIComponent(code)}`)
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

      if (Date.now() - startRef.current > 60_000) {
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
    window.location.href = localhostUrl
  }, [localhostUrl])

  if (!code || !redirectUri) {
    return (
      <main className="min-h-screen flex items-center justify-center p-4">
        <div className="w-full max-w-md text-center">
          <h1 className="text-xl font-bold mb-2">Invalid Request</h1>
          <p className="text-slate-400">Missing required parameters.</p>
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
            <h1 className="text-2xl font-bold mb-2">Login Successful</h1>
            <p className="text-slate-400">API token created. You can close this tab.</p>
          </>
        ) : status === 'fallback' ? (
          <>
            <h1 className="text-2xl font-bold mb-2">Login Successful</h1>
            <p className="text-slate-400 mb-4">Could not reach the CLI automatically.</p>
            <button
              onClick={handleManualRedirect}
              className="bg-brand-600 hover:bg-brand-500 text-white font-medium py-2 px-6 rounded-lg transition-colors inline-block btn-smoke"
            >
              Click here to complete manually
            </button>
          </>
        ) : status === 'timeout' ? (
          <>
            <h1 className="text-2xl font-bold mb-2">Login Successful</h1>
            <p className="text-slate-400">
              The CLI may not have completed the token exchange. Check your terminal.
            </p>
          </>
        ) : (
          <>
            <h1 className="text-2xl font-bold mb-2">Login Successful</h1>
            {/* Spinner */}
            <div className="flex items-center justify-center gap-2 text-slate-400">
              <svg className="animate-spin h-5 w-5" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              <span>Completing authentication...</span>
            </div>
          </>
        )}
      </div>
    </main>
  )
}

export default function CliCompletePage() {
  return (
    <Suspense
      fallback={
        <main className="min-h-screen flex items-center justify-center">
          <div className="text-center">
            <p className="text-slate-500">Loading...</p>
          </div>
        </main>
      }
    >
      <CliCompleteInner />
    </Suspense>
  )
}
