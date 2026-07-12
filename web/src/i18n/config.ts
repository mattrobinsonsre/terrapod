// Supported UI locales for Terrapod (#767).
//
// `en` is the source locale — the message catalog's base. Every other locale
// is translated from it. Adding a locale = add the code here + a matching
// `web/messages/<code>.json`.
//
// IMPORTANT: only *UI text* is localized. Terraform resource names, addresses,
// provider/type identifiers, HCL, and other code are NEVER translated — they
// are stable identifiers, not prose.
//
// The AI plan-summary IS prose and IS translated, but on a different axis from
// the UI chrome (see #767): it is *generated once* in the deployment-default
// language (`ai.summary_language`) — that canonical copy is what ships to Slack
// and GitHub/GitLab, where there's no viewer to translate for — and in the UI
// it is *translated on view* into the reader's chosen language and cached
// per-locale (cheap: a short follow-up turn against the already-cached summary
// context). Resource addresses stay verbatim in every language.

export const defaultLocale = 'en' as const

// Ordered as shown in the switcher: source + en-GB, then real languages, then
// the two joke locales at the end.
export const locales = [
  // Real languages (source + translations).
  'en',
  'en-GB',
  'cy',
  'de',
  'es',
  'fr',
  'la',
  // Novelty / joke locales — full renderings, real (or private-use) tags so the
  // tooling accepts them; they format dates/numbers via a real fallback locale.
  'tlh',
  'x-marklar',
  'x-lolcat',
  'x-leet',
  'x-pirate',
  'x-yoda',
] as const

export type Locale = (typeof locales)[number]

// Native display names for the switcher (a language is best shown in its own
// tongue). Joke locales get a playful-but-recognisable label.
export const localeNames: Record<Locale, string> = {
  en: 'English',
  'en-GB': 'English (UK)',
  cy: 'Cymraeg',
  de: 'Deutsch',
  es: 'Español',
  fr: 'Français',
  la: 'Latina',
  tlh: 'tlhIngan Hol',
  'x-marklar': 'Marklar',
  'x-lolcat': 'LOLCAT',
  'x-leet': '1337 5p34k',
  'x-pirate': 'Pirate',
  'x-yoda': 'Yoda',
}

// Some seed locales are not valid BCP-47 tags that `Intl` understands
// (Klingon `tlh` and the private-use `x-marklar`). For date/number/relative
// formatting we fall back to a real locale so `Intl.*Format` never throws —
// the *prose* is joke-localized, the *number/date shapes* borrow a real locale.
const formattingFallback: Partial<Record<Locale, string>> = {
  la: 'en',
  tlh: 'en',
  'x-marklar': 'en',
  'x-lolcat': 'en',
  'x-leet': 'en',
  'x-pirate': 'en',
  'x-yoda': 'en',
}

export function formattingLocale(locale: string): string {
  return formattingFallback[locale as Locale] ?? locale
}

export function isSupportedLocale(value: string | undefined | null): value is Locale {
  return !!value && (locales as readonly string[]).includes(value)
}

// The cookie the switcher writes and the server layout reads to pick a locale.
export const LOCALE_COOKIE = 'NEXT_LOCALE'
