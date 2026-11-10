// The cookie the theme toggle writes and the root layout reads, so the server
// renders `<html class="dark">` up front and there is no light flash on load.
export const THEME_COOKIE = 'terrapod-theme'

export type Theme = 'light' | 'dark'

// What a user sees until they pick one. A deployment that wants dark for everyone
// changes this one line; an explicit choice in the cookie still wins.
export const DEFAULT_THEME: Theme = 'light'

export function parseTheme(value: string | undefined | null): Theme {
  return value === 'dark' || value === 'light' ? value : DEFAULT_THEME
}
