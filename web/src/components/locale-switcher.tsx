'use client'

import { useTransition } from 'react'
import { useLocale, useTranslations } from 'next-intl'
import { useRouter } from 'next/navigation'
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import { Globe, Check, ChevronDown } from 'lucide-react'
import { locales, localeNames, type Locale } from '@/i18n/config'
import { setLocale } from '@/i18n/locale-actions'

// Short chips shown in the collapsed trigger (the dropdown lists full native
// names). Kept tiny so the globe control doesn't crowd the nav.
const shortLabel: Record<Locale, string> = {
  en: 'EN',
  'en-GB': 'GB',
  cy: 'CY',
  de: 'DE',
  es: 'ES',
  fr: 'FR',
  it: 'IT',
  nl: 'NL',
  'pt-BR': 'BR',
  ja: 'JA',
  ko: 'KO',
  'zh-CN': 'ZH',
  'zh-TW': 'TW',
  ru: 'RU',
  uk: 'UK',
  pl: 'PL',
  cs: 'CS',
  tr: 'TR',
  sv: 'SV',
  da: 'DA',
  nb: 'NB',
  fi: 'FI',
  'pt-PT': 'PT',
  la: 'LA',
  tlh: 'tlh',
  'en-x-marklar': 'MAR',
  'en-x-lolcat': 'LOL',
  'en-x-leet': '1337',
  'en-x-pirate': 'ARR',
  'en-x-yoda': 'YOD',
}

/**
 * The language switcher (#767). Writes the `NEXT_LOCALE` cookie and calls
 * router.refresh() so the server layout re-runs src/i18n/request.ts with the
 * new cookie and re-provides messages — no full reload, no URL change. Lives in
 * the nav beside Help/Account.
 */
export function LocaleSwitcher() {
  const locale = useLocale()
  const t = useTranslations('switcher')
  const router = useRouter()
  const [pending, startTransition] = useTransition()

  const select = (next: string) => {
    if (next === locale) return
    startTransition(async () => {
      await setLocale(next)
      router.refresh()
    })
  }

  const current = shortLabel[locale as Locale] ?? locale.toUpperCase()

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label={t('changeLanguage')}
          disabled={pending}
          className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors outline-none focus-visible:ring-2 focus-visible:ring-brand-500 text-slate-400 hover:text-slate-200 hover:bg-slate-800 data-[state=open]:text-slate-200 data-[state=open]:bg-slate-800 disabled:opacity-60"
        >
          <Globe size={16} />
          <span className="tabular-nums">{current}</span>
          <ChevronDown size={14} className="opacity-70" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          className="z-50 max-h-[70vh] min-w-[11rem] overflow-y-auto rounded-lg border border-slate-700 bg-slate-800 p-1 shadow-xl"
        >
          <DropdownMenu.Label className="px-3 py-1.5 text-xs font-semibold uppercase tracking-wider text-slate-500">
            {t('language')}
          </DropdownMenu.Label>
          {locales.map((code) => {
            const active = code === locale
            return (
              <DropdownMenu.Item
                key={code}
                onSelect={() => select(code)}
                className={`flex items-center justify-between gap-3 px-3 py-2 rounded-md text-sm cursor-pointer outline-none transition-colors data-[highlighted]:bg-slate-700 data-[highlighted]:text-slate-100 ${
                  active ? 'text-brand-400' : 'text-slate-300'
                }`}
              >
                <span>{localeNames[code]}</span>
                {active && <Check size={15} />}
              </DropdownMenu.Item>
            )
          })}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  )
}
