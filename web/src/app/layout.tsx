import type { Metadata } from 'next'
import { Inter } from 'next/font/google'
import { NextIntlClientProvider } from 'next-intl'
import { getLocale, getMessages } from 'next-intl/server'
import { cookies } from 'next/headers'
import { dirForLocale } from '@/i18n/config'
import { parseTheme, THEME_COOKIE } from '@/lib/theme'
import './globals.css'

// `fallback` is not cosmetic here. `next/font` emits `Inter, "Inter Fallback"`
// and NOTHING after it, and both of those are webfonts -- `Inter Fallback` is a
// generated `@font-face` with `size-adjust`, not a local face. A native control
// drawn by the OS rather than the page cannot use either: a `<select>` popup on
// macOS exhausts that chain and lands on Chrome's default, which is a SERIF, so
// every dropdown rendered in Times while the page around it was Inter.
//
// Ending the chain in a generic family is what stops that. It changes nothing
// where the webfont loads, which is why it is invisible until you open a native
// menu.
const inter = Inter({ subsets: ['latin'], fallback: ['system-ui', 'sans-serif'] })

export const metadata: Metadata = {
  title: 'Terrapod',
  description: 'Open-source Terraform Enterprise platform',
  icons: {
    icon: '/logo.svg',
  },
  // Suppress the browser's built-in machine translation (#906). The UI ships
  // its own translated catalogs + an in-app switcher (#767). Chrome decides
  // whether to OFFER page translation from this Google-specific meta, NOT from
  // the W3C `translate="no"` attribute on <html> (which Chrome only honors for
  // excluding individual elements) — so both are set: the meta stops the
  // "Translate this page?" offer, the attribute marks the content non-translatable.
  other: { google: 'notranslate' },
}

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  // Locale + messages come from src/i18n/request.ts (cookie-resolved, no URL
  // segment). The provider exposes them to every client component via
  // useTranslations/useFormatter; `lang` is set dynamically so assistive tech
  // and the browser know the active language (#767).
  const locale = await getLocale()
  const messages = await getMessages()
  // `dir` mirrors the whole UI for RTL locales (#829). Resolved on the server
  // from the same cookie-driven locale as `lang`, so there is no direction
  // flash on load. Arabic, Hebrew and Persian resolve to `rtl`; every other
  // offered locale renders `dir="ltr"`.
  const dir = dirForLocale(locale)
  // Server-rendered from the cookie the Account menu toggle writes, so dark loads without a flash.
  const theme = parseTheme((await cookies()).get(THEME_COOKIE)?.value)
  // The app ships its own professionally-translated catalogs + a language
  // switcher (#767), so tell the browser's built-in translator (Chrome/Edge/
  // Safari) to stand down: `translate="no"` suppresses the "Translate this
  // page?" prompt and stops a machine-translation layer from fighting — and
  // degrading — our own next-intl strings. Users switch language in-app.
  return (
    <html lang={locale} dir={dir} translate="no" className={theme === 'dark' ? 'dark' : undefined}>
      <body className={inter.className}>
        <NextIntlClientProvider locale={locale} messages={messages}>
          {children}
        </NextIntlClientProvider>
      </body>
    </html>
  )
}
