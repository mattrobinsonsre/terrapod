/**
 * Authenticated fetch wrapper.
 *
 * Automatically sets Bearer token from auth state. On 401, clears auth
 * and redirects to login.
 *
 * API path conventions: callers will see two prefixes mixed throughout
 * the frontend.
 *   - `/api/v2/...`         — the TFE V2 API surface that terraform/tofu
 *                             and tfci consume directly. Permanent.
 *   - `/api/terrapod/v1/...` — Terrapod-native management surface
 *                             (auth, labels, listeners, audit, drift,
 *                             etc.). Canonical home from v0.23.0.
 *
 * The split is documented in `docs/tfe-cli-surface.md`. If you're
 * adding a new API call from the frontend: if it's CLI-consumed, hit
 * `/api/v2/`; otherwise hit `/api/terrapod/v1/`.
 */

import { clearAuth, getAuthState, loginRedirectUrl, updateExpiresAt } from '@/lib/auth'

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

  // Update local session expiry when server refreshes it (sliding window)
  const newExpiry = res.headers.get('X-Session-Expires')
  if (newExpiry) {
    updateExpiresAt(newExpiry)
  }

  return res
}
