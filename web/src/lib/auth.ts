/**
 * Auth helpers for client-side session state.
 *
 * Session metadata (token, email, roles, userId) is stored in sessionStorage.
 * Survives page navigation/refreshes within the same tab but cleared when
 * the tab is closed.
 */

const AUTH_KEY = 'terrapod_auth'

interface AuthState {
  token: string
  email: string
  roles: string[]
  userId: string    // email prefix, used as default org for TFE V2 paths
  expiresAt?: string  // ISO 8601
}

function loadAuth(): AuthState | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = sessionStorage.getItem(AUTH_KEY)
    if (!raw) return null
    return JSON.parse(raw) as AuthState
  } catch {
    return null
  }
}

export function setAuth(token: string, email: string, roles: string[], expiresAt?: string): void {
  const userId = email.split('@')[0] || email
  const state: AuthState = { token, email, roles, userId, expiresAt }
  if (typeof window !== 'undefined') {
    sessionStorage.setItem(AUTH_KEY, JSON.stringify(state))
  }
}

export function getAuthState(): AuthState | null {
  return loadAuth()
}

export function getUserId(): string {
  return loadAuth()?.userId ?? ''
}

export function isAdmin(): boolean {
  return loadAuth()?.roles.includes('admin') ?? false
}

export function isAdminOrAudit(): boolean {
  const auth = loadAuth()
  if (!auth) return false
  return auth.roles.includes('admin') || auth.roles.includes('audit')
}

export function getExpiresAt(): Date | null {
  const auth = loadAuth()
  if (!auth?.expiresAt) return null
  try {
    return new Date(auth.expiresAt)
  } catch {
    return null
  }
}

export function clearAuth(): void {
  if (typeof window !== 'undefined') {
    sessionStorage.removeItem(AUTH_KEY)
  }
}

export function loginRedirectUrl(): string {
  if (typeof window === 'undefined') return '/login'
  const current = window.location.pathname + window.location.search
  if (current === '/' || current === '/login') return '/login'
  return `/login?redirect=${encodeURIComponent(current)}`
}
