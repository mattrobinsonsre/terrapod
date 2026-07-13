'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import { setAuth } from '@/lib/auth'
import { STORAGE_AUTH_STATE, STORAGE_PKCE_VERIFIER, STORAGE_REDIRECT_AFTER_LOGIN } from '@/lib/constants'

// A callback error is either a translatable error key (resolved at render via
// useTranslations) or a verbatim message the IDP returned (error_description) —
// the latter is already localised by the provider and must pass through as-is.
type CallbackError =
  | { errorKey: string; errorMessage?: undefined }
  | { errorKey?: undefined; errorMessage: string }

type CallbackParams =
  | (CallbackError & { code?: undefined; verifier?: undefined })
  | { errorKey?: undefined; errorMessage?: undefined; code: string; verifier: string }

function getCallbackParams(): CallbackParams | null {
  if (typeof window === 'undefined') return null
  const params = new URLSearchParams(window.location.search)
  const code = params.get('code')
  const returnedState = params.get('state')
  const errorParam = params.get('error')

  if (errorParam) {
    return { errorMessage: params.get('error_description') || errorParam }
  }
  if (!code || !returnedState) {
    return { errorKey: 'errors.missingCodeOrState' }
  }

  const savedState = sessionStorage.getItem(STORAGE_AUTH_STATE)
  const verifier = sessionStorage.getItem(STORAGE_PKCE_VERIFIER)

  if (!savedState || returnedState !== savedState) {
    return { errorKey: 'errors.stateMismatch' }
  }
  if (!verifier) {
    return { errorKey: 'errors.missingVerifier' }
  }

  return { code, verifier }
}

export default function CallbackHandler() {
  const t = useTranslations('auth')
  const router = useRouter()
  const [callbackParams] = useState(getCallbackParams)
  const [error, setError] = useState(() =>
    callbackParams?.errorKey
      ? t(callbackParams.errorKey)
      : (callbackParams?.errorMessage ?? ''),
  )

  useEffect(() => {
    if (!callbackParams || callbackParams.errorKey || callbackParams.errorMessage) return
    const { code, verifier } = callbackParams as { code: string; verifier: string }

    const body = new URLSearchParams({
      grant_type: 'authorization_code',
      code,
      code_verifier: verifier,
    })

    fetch('/api/terrapod/v1/auth/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
    })
      .then(async (res) => {
        if (!res.ok) {
          const data = await res.json().catch(() => ({}))
          throw new Error(data.detail || t('errors.tokenExchangeFailed', { status: res.status }))
        }
        return res.json()
      })
      .then((data) => {
        setAuth(data.session_token, data.email, data.roles, data.expires_at)
        sessionStorage.removeItem(STORAGE_PKCE_VERIFIER)
        sessionStorage.removeItem(STORAGE_AUTH_STATE)
        const redirect = sessionStorage.getItem(STORAGE_REDIRECT_AFTER_LOGIN)
        sessionStorage.removeItem(STORAGE_REDIRECT_AFTER_LOGIN)
        if (redirect) {
          window.location.href = redirect
        } else {
          router.push('/')
        }
      })
      .catch((err) => {
        setError(err.message)
      })
  }, [router, callbackParams, t])

  if (error) {
    return (
      <main className="min-h-screen flex items-center justify-center p-4">
        <div className="w-full max-w-md text-center">
          <h1 className="text-xl font-bold mb-2">{t('callbackFailedTitle')}</h1>
          <p className="text-red-400 mb-6">{error}</p>
          <a
            href="/login"
            className="bg-brand-600 hover:bg-brand-500 text-white font-medium py-2 px-6 rounded-lg transition-colors inline-block btn-smoke"
          >
            {t('tryAgain')}
          </a>
        </div>
      </main>
    )
  }

  return (
    <main className="min-h-screen flex items-center justify-center">
      <div className="text-center">
        <p className="text-slate-500">{t('completingSignIn')}</p>
      </div>
    </main>
  )
}
