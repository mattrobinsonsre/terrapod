#!/usr/bin/env node
// i18n completeness gate (#767).
//
// A locale is COMPLETE or it is not offered — there is no "partial, falls back
// to English" middle ground for a shipped language. This script enforces that:
// every locale in the OFFERED set (`locales` in src/i18n/config.ts) must have
// 100% key parity with the `en` source catalog AND every string must parse as
// valid ICU. The English deep-merge in request.ts remains only as a crash guard
// (never render MISSING_KEY), NOT as a licence to ship a half-translated locale.
//
// Exception: `en-GB` is a dialect OVERRIDE (British ⊂ American — it only carries
// the spelling deltas; the shared strings are genuinely identical, not a gap),
// so it is checked as a valid subset (keys ⊆ en) + ICU-valid, not full parity.
//
// Run: node scripts/check-i18n-completeness.mjs   (also `npm run i18n:check`)
// Exit non-zero on any incomplete/ invalid offered locale.

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { parse } from '@formatjs/icu-messageformat-parser'

const here = dirname(fileURLToPath(import.meta.url))
const web = join(here, '..')
const messagesDir = join(web, 'messages')

// The dialect-override locale: checked as a subset, not full parity.
const OVERRIDE_LOCALES = new Set(['en-GB'])

function flatten(obj, prefix, out) {
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object') flatten(v, key, out)
    else out[key] = v
  }
  return out
}

function loadFlat(locale) {
  return flatten(JSON.parse(readFileSync(join(messagesDir, `${locale}.json`), 'utf8')), '', {})
}

// Extract the OFFERED locale set from config.ts (the single source of truth —
// the switcher and request.ts read the same array). We only need the quoted
// codes inside `export const locales = [ ... ] as const`.
function offeredLocales() {
  const src = readFileSync(join(web, 'src/i18n/config.ts'), 'utf8')
  const m = src.match(/export const locales = \[([\s\S]*?)\] as const/)
  if (!m) throw new Error('could not find `locales` array in src/i18n/config.ts')
  return [...m[1].matchAll(/'([^']+)'/g)].map((x) => x[1])
}

const en = loadFlat('en')
const enKeys = new Set(Object.keys(en))
const offered = offeredLocales()

let failed = false
console.log(`i18n completeness gate — ${offered.length} offered locales, ${enKeys.size} source keys\n`)

for (const locale of offered) {
  if (locale === 'en') {
    // Source: only ICU-validate.
    let icu = 0
    for (const [k, v] of Object.entries(en)) {
      try {
        parse(v)
      } catch {
        icu++
        console.log(`  en ICU error: ${k}`)
      }
    }
    console.log(`${locale.padEnd(14)} source        ${icu ? `ICU_ERRORS=${icu} FAIL` : 'ok'}`)
    if (icu) failed = true
    continue
  }

  let d
  try {
    d = loadFlat(locale)
  } catch (e) {
    console.log(`${locale.padEnd(14)} MISSING/INVALID FILE: ${e.message}`)
    failed = true
    continue
  }

  const keys = new Set(Object.keys(d))
  const extra = [...keys].filter((k) => !enKeys.has(k))
  const override = OVERRIDE_LOCALES.has(locale)
  const missing = override ? [] : [...enKeys].filter((k) => !keys.has(k))

  let icu = 0
  for (const v of Object.values(d)) {
    try {
      parse(v)
    } catch {
      icu++
    }
  }

  const ok = extra.length === 0 && missing.length === 0 && icu === 0
  const kind = override ? 'override' : 'full    '
  console.log(
    `${locale.padEnd(14)} ${kind} keys=${keys.size} missing=${missing.length} extra=${extra.length} icuErr=${icu} ${ok ? 'ok' : 'FAIL'}`,
  )
  if (!ok) {
    failed = true
    if (missing.length) console.log(`    missing (${missing.length}): ${missing.slice(0, 8).join(', ')}${missing.length > 8 ? ' …' : ''}`)
    if (extra.length) console.log(`    extra (${extra.length}): ${extra.slice(0, 8).join(', ')}`)
    if (icu) console.log(`    ${icu} string(s) fail ICU parse`)
  }
}

if (failed) {
  console.log(
    '\nFAIL — an offered locale is incomplete or invalid. A locale is complete or it is not offered.\n' +
      'Fix: fill the missing keys (translate them) or remove the locale from `locales` in src/i18n/config.ts.',
  )
  process.exit(1)
}
console.log('\nPASS — every offered locale is complete and ICU-valid.')
