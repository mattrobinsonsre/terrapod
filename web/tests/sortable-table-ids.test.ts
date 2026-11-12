/**
 * Every sortable table keeps its sort for the life of the tab, keyed by a table id
 * passed to `useSortable`. The key is per call site, not per page: several pages
 * sort more than one table, and one key per page would make them overwrite each
 * other. A call site that omits the id silently loses its sort on navigation,
 * which is the bug this guards against.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

const SRC = new URL('../src', import.meta.url).pathname

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) return sourceFiles(full)
    return /\.tsx?$/.test(name) ? [full] : []
  })
}

// The text of each `useSortable( ... )` or `useSortable<...>( ... )` call, found by matching parentheses.
function callsIn(text: string): string[] {
  const calls: string[] = []
  const re = /\buseSortable(?:<[^(]*)?\s*\(/g
  let m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    let depth = 1
    let i = re.lastIndex
    while (i < text.length && depth > 0) {
      if (text[i] === '(') depth++
      else if (text[i] === ')') depth--
      i++
    }
    calls.push(text.slice(m.index, i))
  }
  return calls
}

const sites = sourceFiles(SRC)
  .filter((f) => !f.endsWith('lib/use-sortable.ts'))
  .flatMap((file) => callsIn(readFileSync(file, 'utf8')).map((call) => ({ file, call })))

describe('useSortable call sites', () => {
  it('finds the call sites, so the checks below are not vacuous', () => {
    assert.ok(sites.length >= 19, `only ${sites.length} call sites found`)
  })

  it('also finds a call written without type arguments, and ignores an import', () => {
    assert.equal(callsIn("useSortable(items, 'name', 'asc', fn)").length, 1)
    assert.equal(callsIn("import { useSortable } from '@/lib/use-sortable'\nfoo(1)").length, 0)
  })

  it('every call passes a kebab-case table id as its last argument', () => {
    const missing = sites
      .filter(({ call }) => !/,\s*'[a-z0-9]+(-[a-z0-9]+)*',?\s*\)$/.test(call))
      .map(({ file }) => file.replace(SRC, 'src'))
    assert.deepEqual(missing, [], `call sites without a table id:\n  ${missing.join('\n  ')}`)
  })

  it('no two call sites share a table id', () => {
    const ids = sites
      .map(({ call }) => /,\s*'([a-z0-9-]+)',?\s*\)$/.exec(call)?.[1])
      .filter((id): id is string => !!id)
    const dupes = ids.filter((id, i) => ids.indexOf(id) !== i)
    assert.deepEqual([...new Set(dupes)], [], 'table ids must be unique')
  })
})
