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
  'it',
  'nl',
  'pt-BR',
  'ja',
  'ko',
  'zh-CN',
  'zh-TW',
  'ru',
  'uk',
  'pl',
  'cs',
  'tr',
  'sv',
  'da',
  'nb',
  'fi',
  'pt-PT',
  'la',
  // Novelty / joke locales. `tlh` (Klingon) is a real ISO 639-2 subtag. The
  // rest use a valid BCP-47 shape — a real base language plus a `-x-` private-use
  // subtag (`en-x-marklar`) — NOT a private-use-only tag (`x-marklar`), which
  // `Intl.Locale`/next-intl reject as an invalid locale identifier. The `en-`
  // base also gives them English date/number formatting for free.
  'tlh',
  'en-x-marklar',
  'en-x-lolcat',
  'en-x-leet',
  'en-x-pirate',
  'en-x-yoda',
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
  it: 'Italiano',
  nl: 'Nederlands',
  'pt-BR': 'Português (Brasil)',
  ja: '日本語',
  ko: '한국어',
  'zh-CN': '简体中文',
  'zh-TW': '繁體中文',
  ru: 'Русский',
  uk: 'Українська',
  pl: 'Polski',
  cs: 'Čeština',
  tr: 'Türkçe',
  sv: 'Svenska',
  da: 'Dansk',
  nb: 'Norsk bokmål',
  fi: 'Suomi',
  'pt-PT': 'Português (Portugal)',
  la: 'Latina',
  tlh: 'tlhIngan Hol',
  'en-x-marklar': 'Marklar',
  'en-x-lolcat': 'LOLCAT',
  'en-x-leet': '1337 5p34k',
  'en-x-pirate': 'Pirate',
  'en-x-yoda': 'Yoda',
}

// Some seed locales are not valid BCP-47 tags that `Intl` understands
// (Klingon `tlh` and the private-use `x-marklar`). For date/number/relative
// formatting we fall back to a real locale so `Intl.*Format` never throws —
// the *prose* is joke-localized, the *number/date shapes* borrow a real locale.
const formattingFallback: Partial<Record<Locale, string>> = {
  // Klingon and Latin are valid subtags but have no CLDR formatting data — pin
  // them to English so Intl.* never lands on the system default. The en-x-*
  // joke locales already format as English via their `en` base, so they need
  // no entry here.
  la: 'en',
  tlh: 'en',
}

export function formattingLocale(locale: string): string {
  return formattingFallback[locale as Locale] ?? locale
}

export function isSupportedLocale(value: string | undefined | null): value is Locale {
  return !!value && (locales as readonly string[]).includes(value)
}

// The cookie the switcher writes and the server layout reads to pick a locale.
export const LOCALE_COOKIE = 'NEXT_LOCALE'
