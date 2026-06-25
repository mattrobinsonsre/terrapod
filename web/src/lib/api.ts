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

/**
 * Retry contract (mirrors the backend's method-aware safety rule so we never double-write).
 *
 * Bounded exponential backoff: up to MAX_RETRIES extra attempts after the first, with
 * delays ~300ms → 600ms → 1200ms (capped at BACKOFF_CAP_MS).
 *
 *   - Idempotent methods (GET, HEAD, OPTIONS, PUT, DELETE) retry on BOTH a thrown network
 *     error (fetch rejects with a TypeError) AND a 5xx response.
 *   - Non-idempotent methods (POST, PATCH) are NOT retried: the browser can't tell whether
 *     a failed request was delivered, so a retry could double-write. They surface the error.
 *   - A 4xx is never retried (it's a client-side problem; retrying won't help).
 *
 * The retry happens BEFORE the 401 handling: a transient network blip is retried, but a
 * genuine 401 response still clears auth and redirects.
 *
 * Coverage note: the frontend has no unit-test runner (no vitest/jest), so this contract
 * is exercised via the e2e suite and the Next.js build rather than a dedicated unit test.
 */
const MAX_RETRIES = 3
const BACKOFF_BASE_MS = 300
const BACKOFF_CAP_MS = 1200

const IDEMPOTENT_METHODS = new Set(['GET', 'HEAD', 'OPTIONS', 'PUT', 'DELETE'])

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const auth = getAuthState()
  const headers = new Headers(init?.headers)

  if (auth?.token) {
    headers.set('Authorization', `Bearer ${auth.token}`)
  }

  const method = (init?.method ?? 'GET').toUpperCase()
  const idempotent = IDEMPOTENT_METHODS.has(method)

  let res: Response | undefined
  for (let attempt = 0; ; attempt++) {
    try {
      res = await fetch(path, { ...init, headers })
      // Idempotent methods retry transient 5xx; non-idempotent never do (may have applied).
      if (idempotent && res.status >= 500 && res.status <= 599 && attempt < MAX_RETRIES) {
        await sleep(Math.min(BACKOFF_BASE_MS * 2 ** attempt, BACKOFF_CAP_MS))
        continue
      }
      break
    } catch (err) {
      // fetch rejects (TypeError) on a network failure. The browser can't tell us whether
      // the request was never sent or was delivered but the reply was lost — so only retry
      // IDEMPOTENT methods. A non-idempotent POST/PATCH might have applied server-side, and
      // retrying could double-write, so it surfaces the error instead of retrying.
      if (idempotent && attempt < MAX_RETRIES) {
        await sleep(Math.min(BACKOFF_BASE_MS * 2 ** attempt, BACKOFF_CAP_MS))
        continue
      }
      throw err
    }
  }

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
