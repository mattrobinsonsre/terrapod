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

/**
 * Extract a human-readable message from a failed API response.
 *
 * Handles the shapes the API actually returns — JSON:API `{ errors: [{ detail }] }`,
 * FastAPI `{ detail }` (string or validation array), and `{ message }` — falling
 * back to the status code. Use at call sites instead of hand-rolling error
 * extraction (or, worse, swallowing the failure): `setError(await parseApiError(res))`.
 */
export async function parseApiError(res: Response, fallback = 'Request failed'): Promise<string> {
  try {
    const body = await res.clone().json()
    if (Array.isArray(body?.errors) && body.errors[0]?.detail) return String(body.errors[0].detail)
    if (typeof body?.detail === 'string') return body.detail
    if (Array.isArray(body?.detail) && body.detail[0]?.msg) {
      // FastAPI validation error: [{ loc, msg, type }]
      const first = body.detail[0]
      const field = Array.isArray(first.loc) ? first.loc[first.loc.length - 1] : ''
      return field ? `${field}: ${first.msg}` : String(first.msg)
    }
    if (typeof body?.message === 'string') return body.message
  } catch {
    // not JSON
  }
  return `${fallback} (${res.status})`
}
