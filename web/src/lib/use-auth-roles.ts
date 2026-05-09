import { useEffect, useState } from 'react'
import { getAuthState } from '@/lib/auth'

/**
 * Hook returning the authenticated user's roles, with a stable first
 * render.
 *
 * Auth state lives in localStorage, which is unavailable during SSR.
 * Calling `getAuthState()`/`isAdmin()` directly in render produces
 * different output on the server (no roles) than on the first client
 * render (real roles), which Next.js dev mode reports as a hydration
 * error and then layers a blocking modal on top of the page.
 *
 * This hook returns `[]` on the server and on the very first client
 * render, then updates to the real roles in a `useEffect`. The
 * second render — strictly after hydration — is the one that shows
 * admin/audit-gated UI. Callers should derive `isAdmin` /
 * `isAdminOrAudit` from the returned array.
 */
export function useAuthRoles(): string[] {
  const [roles, setRoles] = useState<string[]>([])
  useEffect(() => {
    // The deferred-update is the entire point of this hook — pre-hydration
    // render returns []; post-hydration render returns the real roles.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setRoles(getAuthState()?.roles ?? [])
  }, [])
  return roles
}

export function useIsAdmin(): boolean {
  const roles = useAuthRoles()
  return roles.includes('admin')
}

export function useIsAdminOrAudit(): boolean {
  const roles = useAuthRoles()
  return roles.includes('admin') || roles.includes('audit')
}
