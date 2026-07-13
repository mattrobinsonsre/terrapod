import { cookies, headers } from 'next/headers'
import { getRequestConfig } from 'next-intl/server'
import { defaultLocale, isSupportedLocale, locales, LOCALE_COOKIE } from './config'

// next-intl WITHOUT i18n routing (#767): the locale is not in the URL — it's
// resolved per-request from the cookie the switcher sets, then Accept-Language,
// then the default. This keeps every existing route/BFF path untouched (no
// `/[locale]/...` segment) and works through the SSR + BFF proxy chain.
//
// Called on the server for each request; the resolved `locale` + `messages`
// are handed to <NextIntlClientProvider> in the root layout.

function fromAcceptLanguage(header: string | null): string | undefined {
  if (!header) return undefined
  // "en-GB,en;q=0.9,cy;q=0.8" -> ["en-GB","en","cy"] in preference order.
  const tags = header
    .split(',')
    .map((part) => part.split(';')[0]?.trim())
    .filter(Boolean) as string[]
  for (const tag of tags) {
    if (isSupportedLocale(tag)) return tag
    // Fall back from a region tag to its base (e.g. "de-AT" -> "de").
    const base = tag.split('-')[0]
    if (isSupportedLocale(base)) return base
  }
  return undefined
}

export default getRequestConfig(async () => {
  const cookieLocale = (await cookies()).get(LOCALE_COOKIE)?.value
  const acceptLocale = fromAcceptLanguage((await headers()).get('accept-language'))

  const locale = isSupportedLocale(cookieLocale)
    ? cookieLocale
    : (acceptLocale ?? defaultLocale)

  // `en` is the source catalog and the fallback for every other locale. We deep
  // merge the active locale OVER `en`, so a key that hasn't been translated yet
  // renders the English string instead of a `MISSING_KEY` placeholder. This is
  // what makes the surface-by-surface rollout (#767) safe: a half-translated
  // locale is always fully renderable, never broken.
  const base = (await import('../../messages/en.json')).default
  const overrides =
    locale === defaultLocale
      ? {}
      : (await import(`../../messages/${locale}.json`)).default
  const messages = deepMerge(base, overrides)

  return { locale: locale as (typeof locales)[number], messages }
})

type Messages = { [key: string]: string | Messages }

function deepMerge(base: Messages, override: Messages): Messages {
  const out: Messages = { ...base }
  for (const [key, value] of Object.entries(override)) {
    const existing = out[key]
    if (
      value &&
      typeof value === 'object' &&
      existing &&
      typeof existing === 'object'
    ) {
      out[key] = deepMerge(existing, value)
    } else {
      out[key] = value
    }
  }
  return out
}
