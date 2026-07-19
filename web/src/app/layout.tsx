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
  // flash on load. LTR locales (every one currently offered) render `dir="ltr"`
  // — identical to before — until an RTL locale is added to `locales`.
  const dir = dirForLocale(locale)
  return (
    <html lang={locale} dir={dir} className="dark">
      <body className={inter.className}>
        <NextIntlClientProvider locale={locale} messages={messages}>
          {children}
        </NextIntlClientProvider>
      </body>
    </html>
  )
}
