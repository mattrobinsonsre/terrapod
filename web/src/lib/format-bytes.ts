/**
 * A byte count as a short human figure — "0 B", "4.1 KiB", "2.3 GiB".
 *
 * One producer because the caches page renders a size in four places (two
 * totals strips, two tables) and a bulk purge quotes one back inside a
 * `confirm()`. Two spellings of the same number is how a strip comes to
 * disagree with the prompt that is about to act on it.
 *
 * Binary units, not decimal: these are object-storage sizes, and every other
 * tool an operator will compare them against (`du`, `aws s3 ls --summarize`,
 * the storage console) reports the same way. The unit symbols are symbols
 * rather than prose, so they are not translated — the NUMBER is, via
 * `Intl.NumberFormat`, because a thousands separator and a decimal point both
 * move by locale.
 */
const UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'] as const

export function formatBytes(bytes: number, locale?: string): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024
    unit++
  }
  // Whole bytes are always whole; anything scaled gets one decimal, which is
  // enough to tell 1.2 GiB from 1.9 GiB and not enough to read as precision
  // the figure does not have.
  const digits = unit === 0 ? 0 : 1
  const number = new Intl.NumberFormat(locale, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value)
  return `${number} ${UNITS[unit]}`
}
