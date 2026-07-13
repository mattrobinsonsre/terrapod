'use server'

import { cookies } from 'next/headers'
import { defaultLocale, isSupportedLocale, LOCALE_COOKIE } from './config'

// Server action to persist the chosen UI locale (#767). Setting the cookie
// server-side (rather than writing document.cookie in the client) is the
// next-intl "without routing" pattern — and it keeps the client component free
// of direct DOM mutation. After this resolves, the caller runs router.refresh()
// so the server layout re-runs src/i18n/request.ts with the new cookie and
// re-provides messages. The value is validated against the supported set.
export async function setLocale(next: string) {
  const value = isSupportedLocale(next) ? next : defaultLocale
  const store = await cookies()
  store.set(LOCALE_COOKIE, value, {
    path: '/',
    maxAge: 60 * 60 * 24 * 365,
    sameSite: 'lax',
  })
}
