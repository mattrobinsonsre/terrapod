import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * The TFE-compatibility surface serves Terraform and nothing else (#1904), so a
 * UI call on `/api/v2` answers 404 for every Pulumi workspace — silently, from
 * the only page that operates one.
 *
 * That is not theoretical: #1905 extended the engine filter from one router to
 * all of them, and the UI was not moved with it. Confirm & Apply, Discard,
 * Cancel and drift remediation all 404'd on a Pulumi run, and the code comment
 * beside them asserted `/api/v2` was permanently correct.
 *
 * So the rule is inverted here: a workspace- or run-scoped `/api/v2` call is a
 * defect unless it is listed below with a reason, and the reason has to be that
 * no native route exists yet.
 */

const SRC = fileURLToPath(new URL('../src', import.meta.url))

/** Each entry: the file, and why it may still call the TFE surface. */
const ALLOWED: Record<string, string> = {
  'app/admin/variable-sets/page.tsx':
    'org-scoped varsets — not workspace- or run-scoped, so the engine filter does not apply',
  'app/admin/variable-sets/[id]/page.tsx':
    'varset CRUD and its workspace relationships are org-scoped; the workspace LIST here is native',
  'components/nav-bar.tsx': 'GET /api/v2/ping is the TFE service-discovery probe itself',
  'lib/api.ts': 'prose in a doc comment, not a call',
}

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) return walk(full)
    return /\.(ts|tsx)$/.test(entry) ? [full] : []
  })
}

test('no unlisted page drives an engine-scoped route through the TFE surface', () => {
    const offenders: string[] = []
    for (const file of walk(SRC)) {
      const rel = file.slice(SRC.length + 1)
      if (rel in ALLOWED) continue
      const body = readFileSync(file, 'utf8')
      body.split('\n').forEach((line, i) => {
        if (line.includes('/api/v2/')) offenders.push(`${rel}:${i + 1}  ${line.trim()}`)
      })
    }
    assert.deepEqual(
      offenders,
      [],
      'these call the TFE surface, which serves only Terraform — on a Pulumi ' +
        'workspace they 404 with no error shown. Move them to /api/v1, or add ' +
        'an entry to ALLOWED saying which native route is missing:\n' +
        offenders.join('\n'),
    )
})

test('the run page drives its own actions natively', () => {
  const body = readFileSync(join(SRC, 'app/workspaces/[id]/runs/[runId]/page.tsx'), 'utf8')
  assert.ok(
    body.includes("const prefix = '/api/v1'"),
    'confirm / discard / cancel must not go through the TFE surface — that is the apply gate',
  )
  assert.ok(!body.includes("'/api/v2/runs'"), 'drift remediation creates a run natively')
})
