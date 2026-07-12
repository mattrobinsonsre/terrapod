'use client'

import { useMemo } from 'react'
import { useLocale } from 'next-intl'
import { formattingLocale } from '@/i18n/config'

// Locale-aware formatters (#767). All ad-hoc `toLocaleString()` /
// `toLocaleDateString()` calls in the UI should go through `useFormat()` so
// dates, relative times, numbers, and byte sizes render in the active locale.
//
// The formatting locale is resolved via `formattingLocale()`, so joke locales
// with no CLDR data (Klingon, the en-x-* novelties, Latin) format via a real
// fallback (English) instead of throwing or landing on the system default.
//
// Timestamps are RFC3339-with-Z from the API; `new Date(value)` parses them.

const RELATIVE_UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['year', 60 * 60 * 24 * 365],
  ['month', 60 * 60 * 24 * 30],
  ['day', 60 * 60 * 24],
  ['hour', 60 * 60],
  ['minute', 60],
  ['second', 1],
]

export function useFormat() {
  const locale = useLocale()
  const fmt = formattingLocale(locale)

  return useMemo(() => {
    const toDate = (value: string | number | Date | null | undefined) =>
      value === null || value === undefined || value === '' ? null : new Date(value)

    return {
      /** Medium date, e.g. "5 Jul 2026". Empty string for missing input. */
      date(value: string | number | Date | null | undefined, opts?: Intl.DateTimeFormatOptions) {
        const d = toDate(value)
        return d ? new Intl.DateTimeFormat(fmt, opts ?? { dateStyle: 'medium' }).format(d) : ''
      },
      /** Date + short time, e.g. "5 Jul 2026, 14:03". */
      dateTime(value: string | number | Date | null | undefined) {
        const d = toDate(value)
        return d
          ? new Intl.DateTimeFormat(fmt, { dateStyle: 'medium', timeStyle: 'short' }).format(d)
          : ''
      },
      /** Relative time, e.g. "2 minutes ago" / "in 3 days". */
      relativeTime(value: string | number | Date | null | undefined) {
        const d = toDate(value)
        if (!d) return ''
        const diffSeconds = Math.round((d.getTime() - Date.now()) / 1000)
        const abs = Math.abs(diffSeconds)
        const rtf = new Intl.RelativeTimeFormat(fmt, { numeric: 'auto' })
        for (const [unit, secondsInUnit] of RELATIVE_UNITS) {
          if (abs >= secondsInUnit || unit === 'second') {
            return rtf.format(Math.round(diffSeconds / secondsInUnit), unit)
          }
        }
        return ''
      },
      /** Locale-grouped number, e.g. "1,234" / "1.234". */
      number(value: number, opts?: Intl.NumberFormatOptions) {
        return new Intl.NumberFormat(fmt, opts).format(value)
      },
      /** Human byte size, e.g. "1.2 MB", locale-grouped. */
      bytes(value: number) {
        const units = ['B', 'KB', 'MB', 'GB', 'TB']
        let n = value
        let i = 0
        while (n >= 1024 && i < units.length - 1) {
          n /= 1024
          i++
        }
        const num = new Intl.NumberFormat(fmt, {
          maximumFractionDigits: i === 0 ? 0 : 1,
        }).format(n)
        return `${num} ${units[i]}`
      },
    }
  }, [fmt])
}
