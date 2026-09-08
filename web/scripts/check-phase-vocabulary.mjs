#!/usr/bin/env node
/**
 * No component may hardcode an engine's verb into a phase label (#1407 §11, #1521).
 *
 * Internal state names never change — a run is `planning` whatever engine it
 * belongs to. What a person is *shown* does: Terraform plans and applies, Pulumi
 * previews and updates, Ansible checks and runs. #1407 §3 requires that
 * difference stay visible "not smoothed over", and a component that renders the
 * word "plan" directly is what stops a second engine ever saying "preview".
 *
 * The message catalogues may still say "terraform plan" for Terraform — that is
 * its vocabulary and it is correct. What must not happen is a *component*
 * reaching for that wording, because a catalogue can be translated per engine
 * and a literal cannot.
 *
 * Lives here rather than in the Python suite because it scans `web/src`, which
 * the test image does not carry — and a guard that quietly skips in CI while
 * passing on a laptop is worse than none.
 */

import { readFileSync } from 'node:fs'
import { globSync } from 'node:fs'
import path from 'node:path'

const SRC = path.join(process.cwd(), 'src')

/** An engine's verb, as it would appear if someone wrote it into a component. */
const FORBIDDEN = [/terraform\s+plan/i, /terraform\s+apply/i]

const files = globSync('**/*.{tsx,ts}', { cwd: SRC })
const offenders = []

for (const rel of files) {
  const full = path.join(SRC, rel)
  const lines = readFileSync(full, 'utf8').split('\n')
  lines.forEach((line, i) => {
    const trimmed = line.trim()
    // Comments are where this rule gets explained, so matching them would make
    // the guard fire on its own rationale — and a check that flags prose is a
    // check someone disables.
    if (trimmed.startsWith('//') || trimmed.startsWith('*') || trimmed.startsWith('/*')) return
    if (FORBIDDEN.some(re => re.test(line))) {
      offenders.push(`  src/${rel}:${i + 1}  ${trimmed.slice(0, 70)}`)
    }
  })
}

if (offenders.length > 0) {
  console.error(
    `FAIL — an engine's verb is hardcoded in a component. Phase words must come\n` +
      `from phaseKey(engine, …) so a second engine can supply its own:\n` +
      offenders.join('\n'),
  )
  process.exit(1)
}

console.log(`PASS — no hardcoded engine verbs in ${files.length} component file(s).`)
