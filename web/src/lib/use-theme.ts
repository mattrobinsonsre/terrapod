'use client'

import { useSyncExternalStore } from 'react'
import { setTheme } from './theme-actions'

// The `dark` class on <html> is the source of truth; the layout sets it from the cookie.
function subscribe(onChange: () => void) {
  const observer = new MutationObserver(onChange)
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
  return () => observer.disconnect()
}

const isDark = () => document.documentElement.classList.contains('dark')

export function useDarkTheme(): [boolean, (dark: boolean) => void] {
  const dark = useSyncExternalStore(subscribe, isDark, () => false)
  const set = (next: boolean) => {
    document.documentElement.classList.toggle('dark', next)
    // Revert if the cookie could not be saved, unless a later click has already changed the class.
    setTheme(next ? 'dark' : 'light').catch(() => {
      if (isDark() === next) document.documentElement.classList.toggle('dark', !next)
    })
  }
  return [dark, set]
}
