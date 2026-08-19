import type { Metadata } from 'next'
import { Inter } from 'next/font/google'
import { NextIntlClientProvider } from 'next-intl'
import { getLocale, getMessages } from 'next-intl/server'
import { dirForLocale } from '@/i18n/config'
import './globals.css'

const inter = Inter({ subsets: ['latin'] })

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
  // The app ships its own professionally-translated catalogs + a language
  // switcher (#767), so tell the browser's built-in translator (Chrome/Edge/
  // Safari) to stand down: `translate="no"` suppresses the "Translate this
  // page?" prompt and stops a machine-translation layer from fighting — and
  // degrading — our own next-intl strings. Users switch language in-app.
  return (
    <html lang={locale} dir={dir} translate="no" className="dark">
      <body className={inter.className}>
        <NextIntlClientProvider locale={locale} messages={messages}>
          {children}
        </NextIntlClientProvider>
      </body>
    </html>
  )
}
