#!/usr/bin/env node
/**
 * Assert that every message key the code asks for exists in every catalog.
 *
 * This is not what `check-i18n-completeness.mjs` does. That one checks key
 * *parity across locales* — every catalog holds the same set. Parity is
 * necessary and not sufficient: thirty-two locales holding the same wrong key
 * are indistinguishable from thirty-two holding the right one, because parity
 * has no view of what the components request. That gap shipped a namespace
 * typo (#1334) which every gate passed and which only a human opening the page
 * found, as a MISSING_MESSAGE thrown at render.
 *
 * So this walks the other direction: extract `useTranslations('ns')` +
 * `t('key')` pairs from the source, and resolve each against every catalog.
 *
 * It is deliberately conservative. Only statically-analysable literals are
 * checked — a key built at runtime (`t(\`status.${x}\`)`) is skipped rather
 * than guessed at, because a false failure here would train people to ignore
 * the gate, which is worse than the narrower coverage.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

const SRC = 'src'
const MESSAGES = 'messages'

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (p.endsWith('.tsx') || p.endsWith('.ts')) out.push(p)
  }
  return out
}

function resolve(obj, dotted) {
  let cur = obj
  for (const part of dotted.split('.')) {
    if (cur == null || typeof cur !== 'object' || !(part in cur)) return false
    cur = cur[part]
  }
  return typeof cur === 'string'
}

const catalogs = new Map()
for (const f of readdirSync(MESSAGES).filter((f) => f.endsWith('.json'))) {
  catalogs.set(f.replace(/\.json$/, ''), JSON.parse(readFileSync(join(MESSAGES, f), 'utf8')))
}

// en-GB is a deliberate spelling-delta subset, so it is excluded here: it holds
// only the strings that differ from American English by design, and checking it
// would report thousands of intentional absences.
//
// That exclusion is uncomfortable, because next-intl does NOT deep-merge en
// beneath it at runtime — those absences really do raise MISSING_MESSAGE in the
// browser. That is a pre-existing defect tracked separately, not something this
// gate should paper over or be blocked by. Excluding it costs nothing for the
// bug this gate exists to catch: a wrong namespace is wrong in every catalog,
// so the other thirty-one still fail it.
catalogs.delete('en-GB')
const requested = []
for (const file of walk(SRC)) {
  const src = readFileSync(file, 'utf8')
  // One namespace per module in this codebase; take them all and try each.
  const namespaces = [...src.matchAll(/useTranslations\(\s*['"]([^'"]+)['"]\s*\)/g)].map((m) => m[1])
  if (namespaces.length === 0) continue
  for (const m of src.matchAll(/\bt\(\s*['"]([^'"${}]+)['"]/g)) {
    requested.push({ file, key: m[1], namespaces })
  }
}

const failures = []
for (const { file, key, namespaces } of requested) {
  for (const [locale, catalog] of catalogs) {
    // A key is satisfied if it resolves under ANY namespace the module uses.
    const ok = namespaces.some((ns) => resolve(catalog, `${ns}.${key}`))
    if (!ok) failures.push(`${locale}: ${namespaces.join('|')}.${key}  (${file})`)
  }
}

const checked = requested.length
if (failures.length) {
  console.error(`\nFAIL — ${failures.length} requested key(s) do not resolve:\n`)
  for (const f of failures.slice(0, 40)) console.error('  ' + f)
  if (failures.length > 40) console.error(`  ... and ${failures.length - 40} more`)
  console.error('\nA key the code asks for is missing, or is filed under the wrong namespace.\n')
  process.exit(1)
}

console.log(
  `\nPASS — ${checked} requested key(s) resolve in all ${catalogs.size} catalogs.`
)
