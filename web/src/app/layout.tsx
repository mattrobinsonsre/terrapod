import type { Metadata } from 'next'
import { Inter } from 'next/font/google'
import { NextIntlClientProvider } from 'next-intl'
import { getLocale, getMessages } from 'next-intl/server'
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
  return (
    <html lang={locale} className="dark">
      <body className={inter.className}>
        <NextIntlClientProvider locale={locale} messages={messages}>
          {children}
        </NextIntlClientProvider>
      </body>
    </html>
  )
}
