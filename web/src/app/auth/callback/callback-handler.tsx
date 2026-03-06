'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { setAuth } from '@/lib/auth'

type CallbackParams =
  | { error: string; code?: undefined; verifier?: undefined }
  | { error?: undefined; code: string; verifier: string }

function getCallbackParams(): CallbackParams | null {
  if (typeof window === 'undefined') return null
  const params = new URLSearchParams(window.location.search)
  const code = params.get('code')
  const returnedState = params.get('state')
  const errorParam = params.get('error')

  if (errorParam) {
    return { error: params.get('error_description') || errorParam }
  }
  if (!code || !returnedState) {
    return { error: 'Missing authorization code or state' }
  }

  const savedState = sessionStorage.getItem('terrapod_auth_state')
  const verifier = sessionStorage.getItem('terrapod_pkce_verifier')

  if (!savedState || returnedState !== savedState) {
    return { error: 'State mismatch — possible CSRF attack' }
  }
  if (!verifier) {
    return { error: 'Missing PKCE verifier — please try logging in again' }
  }

  return { code, verifier }
}

export default function CallbackHandler() {
  const router = useRouter()
  const [callbackParams] = useState(getCallbackParams)
  const [error, setError] = useState(() => callbackParams?.error ?? '')

  useEffect(() => {
    if (!callbackParams || callbackParams.error) return
    const { code, verifier } = callbackParams as { code: string; verifier: string }

    const body = new URLSearchParams({
      grant_type: 'authorization_code',
      code,
      code_verifier: verifier,
    })

    fetch('/api/v2/auth/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
    })
      .then(async (res) => {
        if (!res.ok) {
          const data = await res.json().catch(() => ({}))
          throw new Error(data.detail || `Token exchange failed (${res.status})`)
        }
        return res.json()
      })
      .then((data) => {
        setAuth(data.session_token, data.email, data.roles, data.expires_at)
        sessionStorage.removeItem('terrapod_pkce_verifier')
        sessionStorage.removeItem('terrapod_auth_state')
        const redirect = sessionStorage.getItem('terrapod_redirect_after_login')
        sessionStorage.removeItem('terrapod_redirect_after_login')
        if (redirect) {
          window.location.href = redirect
        } else {
          router.push('/')
        }
      })
      .catch((err) => {
        setError(err.message)
      })
  }, [router, callbackParams])

  if (error) {
    return (
      <main className="min-h-screen flex items-center justify-center p-4">
        <div className="w-full max-w-md text-center">
          <h1 className="text-xl font-bold mb-2">Authentication Failed</h1>
          <p className="text-red-400 mb-6">{error}</p>
          <a
            href="/login"
            className="bg-brand-600 hover:bg-brand-500 text-white font-medium py-2 px-6 rounded-lg transition-colors inline-block btn-smoke"
          >
            Try Again
          </a>
        </div>
      </main>
    )
  }

  return (
    <main className="min-h-screen flex items-center justify-center">
      <div className="text-center">
        <p className="text-slate-500">Completing sign in...</p>
      </div>
    </main>
  )
}
