'use client'

import { Suspense, useEffect, useState } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { setAuth } from '@/lib/auth'
import { generatePKCE, generateState } from '@/lib/pkce'

interface Provider {
  name: string
  type: string
}

export default function LoginPage() {
  return (
    <Suspense fallback={
      <main className="h-screen flex items-center justify-center p-4">
        <div className="text-slate-500">Loading...</div>
      </main>
    }>
      <LoginContent />
    </Suspense>
  )
}

function LoginContent() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const [providers, setProviders] = useState<Provider[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')

  const redirectUrl = searchParams.get('redirect')

  const hasLocalProvider = providers.some((p) => p.type === 'local')
  const externalProviders = providers.filter((p) => p.type !== 'local')

  useEffect(() => {
    fetch('/api/v2/auth/providers')
      .then(async (res) => {
        if (!res.ok) throw new Error('Failed to load providers')
        const data = await res.json()
        setProviders(data.providers || [])
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [])

  const handleLocalLogin = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setSubmitting(true)

    try {
      const { verifier, challenge } = await generatePKCE()
      const state = generateState()

      const authRes = await fetch('/api/v2/auth/local/authorize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email,
          password,
          code_challenge: challenge,
          code_challenge_method: 'S256',
          state,
        }),
      })

      if (!authRes.ok) {
        const data = await authRes.json().catch(() => ({}))
        throw new Error(data.detail || `Authentication failed (${authRes.status})`)
      }

      const { code } = await authRes.json()

      const tokenRes = await fetch('/api/v2/auth/token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        credentials: 'include',
        body: new URLSearchParams({
          grant_type: 'authorization_code',
          code,
          code_verifier: verifier,
        }).toString(),
      })

      if (!tokenRes.ok) {
        const data = await tokenRes.json().catch(() => ({}))
        throw new Error(data.detail || `Token exchange failed (${tokenRes.status})`)
      }

      const tokenData = await tokenRes.json()
      setAuth(tokenData.session_token, tokenData.email, tokenData.roles, tokenData.expires_at)

      if (redirectUrl) {
        window.location.href = redirectUrl
      } else {
        router.push('/')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    } finally {
      setSubmitting(false)
    }
  }

  const startExternalLogin = async (providerName: string) => {
    try {
      const { verifier, challenge } = await generatePKCE()
      const state = generateState()

      sessionStorage.setItem('terrapod_pkce_verifier', verifier)
      sessionStorage.setItem('terrapod_auth_state', state)
      if (redirectUrl) {
        sessionStorage.setItem('terrapod_redirect_after_login', redirectUrl)
      }

      const params = new URLSearchParams({
        provider: providerName,
        redirect_uri: `${window.location.origin}/auth/callback`,
        code_challenge: challenge,
        code_challenge_method: 'S256',
        state,
        response_type: 'code',
      })

      window.location.href = `/api/v2/auth/authorize?${params.toString()}`
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start login')
    }
  }

  return (
    <main className="h-screen flex flex-col items-center justify-center p-4 pb-[20vh]">
      <div className="w-full max-w-md">
        <div className="text-center mb-8">
          <h1 className="text-3xl font-bold">Terrapod</h1>
          <p className="text-slate-400 mt-2">Sign in to manage your infrastructure</p>
        </div>

        <div className="bg-slate-800 rounded-lg shadow-lg p-6 border border-slate-700/50">
          {error && (
            <div className="mb-4 p-3 bg-red-900/30 text-red-400 rounded-lg text-sm border border-red-800/50">
              {error}
            </div>
          )}

          {loading ? (
            <div className="text-center text-slate-500 py-4">Loading...</div>
          ) : (
            <div className="space-y-4">
              {hasLocalProvider && (
                <form onSubmit={handleLocalLogin} className="space-y-3">
                  <div>
                    <label htmlFor="email" className="block text-sm font-medium text-slate-300 mb-1">
                      Email
                    </label>
                    <input
                      id="email"
                      type="text"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      required
                      autoComplete="username"
                      autoFocus
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                    />
                  </div>
                  <div>
                    <label htmlFor="password" className="block text-sm font-medium text-slate-300 mb-1">
                      Password
                    </label>
                    <input
                      id="password"
                      type="password"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      required
                      autoComplete="current-password"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                    />
                  </div>
                  <button
                    type="submit"
                    disabled={submitting}
                    className="w-full font-medium py-3 px-4 rounded-lg transition-colors bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white btn-smoke"
                  >
                    {submitting ? 'Signing in...' : 'Sign in'}
                  </button>
                </form>
              )}

              {hasLocalProvider && externalProviders.length > 0 && (
                <div className="relative">
                  <div className="absolute inset-0 flex items-center">
                    <div className="w-full border-t border-slate-600" />
                  </div>
                  <div className="relative flex justify-center text-sm">
                    <span className="px-2 bg-slate-800 text-slate-500">or</span>
                  </div>
                </div>
              )}

              {externalProviders.map((provider) => (
                <button
                  key={provider.name}
                  onClick={() => startExternalLogin(provider.name)}
                  className="w-full font-medium py-3 px-4 rounded-lg transition-colors bg-brand-700 hover:bg-brand-600 text-white"
                >
                  Sign in with {provider.name}
                </button>
              ))}

              {providers.length === 0 && !error && (
                <p className="text-center text-slate-500">
                  No authentication providers configured
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </main>
  )
}
