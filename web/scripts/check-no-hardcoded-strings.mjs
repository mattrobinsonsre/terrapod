#!/usr/bin/env node
// Hardcoded user-facing-string guard (#767 / i18n).
//
// The completeness gate proves every *offered locale* matches the `en` source
// catalog — but it cannot catch a brand-new English string that was typed
// directly into JSX and never extracted into the catalog at all. This guard is
// that missing half: it fails when a `.tsx` component contains visible English
// text (JSX text nodes, or user-facing string attributes like `placeholder` /
// `aria-label`) that isn't routed through `next-intl` (`t(...)` /
// `getTranslations`).
//
// It is AST-based (via the TypeScript compiler) so it's precise, not a noisy
// regex. It ratchets against a committed baseline allowlist
// (`i18n-hardcoded-allowlist.json`): the gate fails on any NEW literal not in
// the baseline. Regenerate the baseline (only when you've consciously reviewed
// the additions) with:
//
//     UPDATE_I18N_ALLOWLIST=1 node scripts/check-no-hardcoded-strings.mjs
//
// Suppress a genuine false positive inline by putting `i18n-ignore` in a comment
// on the same line.
//
// Run: node scripts/check-no-hardcoded-strings.mjs   (also `npm run i18n:lint`)

import { readFileSync, writeFileSync, readdirSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, relative } from 'node:path'
import ts from 'typescript'

const here = dirname(fileURLToPath(import.meta.url))
const web = join(here, '..')
const srcDir = join(web, 'src')
const ALLOWLIST = join(here, 'i18n-hardcoded-allowlist.json')

// User-facing JSX attributes whose string-literal values are shown to the user.
const TEXT_ATTRS = new Set(['placeholder', 'title', 'aria-label', 'alt', 'label'])

// A JSX text/attr counts as "prose" (must be translated) only if it has a run
// of ≥2 letters — this skips punctuation, separators (·, ×, —), single glyphs,
// and pure numbers, which are not translatable copy.
const hasWord = (s) => /[A-Za-z]{2,}/.test(s)

function walkTsxFiles(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    const st = statSync(p)
    if (st.isDirectory()) walkTsxFiles(p, out)
    else if (name.endsWith('.tsx')) out.push(p)
  }
  return out
}

// Line text (for inline `i18n-ignore` suppression).
function lineContainsIgnore(sourceText, lineStarts, pos) {
  let line = 0
  for (let i = 1; i < lineStarts.length; i++) {
    if (lineStarts[i] > pos) break
    line = i
  }
  const start = lineStarts[line]
  const end = line + 1 < lineStarts.length ? lineStarts[line + 1] : sourceText.length
  return sourceText.slice(start, end).includes('i18n-ignore')
}

function findOffenders(file) {
  const text = readFileSync(file, 'utf8')
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const lineStarts = sf.getLineStarts()
  const rel = relative(web, file)
  const offenders = []

  const push = (node, kind, value) => {
    const { line } = sf.getLineAndCharacterOfPosition(node.getStart(sf))
    if (lineContainsIgnore(text, lineStarts, node.getStart(sf))) return
    offenders.push({ id: `${rel}:${kind}:${value.trim()}`, rel, line: line + 1, kind, value: value.trim() })
  }

  const visit = (node) => {
    // Visible JSX text between tags.
    if (ts.isJsxText(node)) {
      const raw = node.text.replace(/\{[^}]*\}/g, '').trim()
      if (raw && hasWord(raw)) push(node, 'text', raw.replace(/\s+/g, ' '))
    }
    // User-facing string-literal attributes: placeholder="Search…" etc.
    if (ts.isJsxAttribute(node) && node.initializer) {
      const attr = node.name.getText(sf)
      if (TEXT_ATTRS.has(attr) && ts.isStringLiteral(node.initializer)) {
        const v = node.initializer.text
        if (hasWord(v)) push(node, `attr:${attr}`, v)
      }
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  return offenders
}

const files = walkTsxFiles(srcDir)
const all = files.flatMap(findOffenders)

if (process.env.UPDATE_I18N_ALLOWLIST) {
  const ids = [...new Set(all.map((o) => o.id))].sort()
  writeFileSync(ALLOWLIST, JSON.stringify(ids, null, 2) + '\n')
  console.log(`Baseline written: ${ids.length} known literals in ${relative(web, ALLOWLIST)}`)
  process.exit(0)
}

let baseline = []
try {
  baseline = JSON.parse(readFileSync(ALLOWLIST, 'utf8'))
} catch {
  console.log(`No baseline at ${relative(web, ALLOWLIST)} — generate it with UPDATE_I18N_ALLOWLIST=1`)
  process.exit(1)
}
const known = new Set(baseline)
const fresh = all.filter((o) => !known.has(o.id))

console.log(
  `hardcoded-string guard — ${files.length} .tsx files, ${all.length} literal(s) found, ` +
    `${baseline.length} baselined, ${fresh.length} new\n`,
)

if (fresh.length) {
  for (const o of fresh) console.log(`  ${o.rel}:${o.line}  [${o.kind}]  "${o.value.slice(0, 80)}"`)
  console.log(
    `\nFAIL — ${fresh.length} new hardcoded user-facing string(s). Route them through next-intl ` +
      `(useTranslations / getTranslations) and add the key to web/messages/en.json + every offered ` +
      `locale.\nIf a hit is genuinely not UX copy (a code identifier, product name, etc.), add ` +
      `\`i18n-ignore\` in a comment on that line, or — after review — regenerate the baseline ` +
      `(UPDATE_I18N_ALLOWLIST=1).`,
  )
  process.exit(1)
}
console.log('PASS — no new hardcoded user-facing strings.')
