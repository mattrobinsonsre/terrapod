// The cookie the theme toggle writes and the root layout reads, so the server
// renders `<html class="dark">` up front and there is no light flash on load.
export const THEME_COOKIE = 'terrapod-theme'

export type Theme = 'light' | 'dark'

export function parseTheme(value: string | undefined | null): Theme {
  return value === 'dark' ? 'dark' : 'light'
}
