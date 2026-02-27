/**
 * Authenticated fetch wrapper.
 *
 * Automatically sets Bearer token from auth state. On 401, clears auth
 * and redirects to login.
 */

import { clearAuth, getAuthState, loginRedirectUrl } from '@/lib/auth'

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const auth = getAuthState()
  const headers = new Headers(init?.headers)

  if (auth?.token) {
    headers.set('Authorization', `Bearer ${auth.token}`)
  }

  const res = await fetch(path, { ...init, headers })

  if (res.status === 401) {
    clearAuth()
    if (typeof window !== 'undefined') {
      window.location.href = loginRedirectUrl()
    }
  }

  return res
}
