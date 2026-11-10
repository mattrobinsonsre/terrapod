'use server'

import { cookies } from 'next/headers'
import { parseTheme, THEME_COOKIE } from './theme'

// Persists the theme server-side, mirroring setLocale; the value is validated.
export async function setTheme(next: string) {
  const store = await cookies()
  store.set(THEME_COOKIE, parseTheme(next), {
    path: '/',
    httpOnly: true,
    maxAge: 60 * 60 * 24 * 365,
    sameSite: 'lax',
  })
}
