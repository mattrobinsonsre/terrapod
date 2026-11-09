/**
 * The Workspaces list keeps its filter and sort until the user clears them.
 * The URL wins (a shared link must show what it says), then the stored value,
 * then the default; a cleared value is removed rather than stored empty.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import {
  parseSort,
  readPref,
  resolveInitial,
  serializeSort,
  usableEngine,
  writePref,
  type PrefStore,
} from '../src/lib/workspace-view-prefs.ts'

function memoryStore(initial: Record<string, string> = {}): PrefStore & { data: Record<string, string> } {
  const data = { ...initial }
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = v
    },
    removeItem: (k) => {
      delete data[k]
    },
  }
}

describe('resolveInitial', () => {
  it('prefers the URL over the stored value', () => {
    assert.equal(resolveInitial('status:errored', 'status:applied'), 'status:errored')
  })

  it('falls back to the stored value when the URL carries none', () => {
    assert.equal(resolveInitial(null, 'status:errored'), 'status:errored')
    assert.equal(resolveInitial('', 'status:errored'), 'status:errored')
  })

  it('returns an empty string when neither is set', () => {
    assert.equal(resolveInitial(null, null), '')
  })
})

describe('writePref / readPref', () => {
  it('keeps a value across a read, the way a navigation re-reads it', () => {
    const store = memoryStore()
    writePref(store, 'terrapod:workspace-filter', 'status:errored')
    assert.equal(readPref(store, 'terrapod:workspace-filter'), 'status:errored')
  })

  it('removes the key when the value is cleared, so it stays cleared', () => {
    const store = memoryStore({ 'terrapod:workspace-filter': 'status:errored' })
    writePref(store, 'terrapod:workspace-filter', '')
    assert.equal(readPref(store, 'terrapod:workspace-filter'), null)
    assert.ok(!('terrapod:workspace-filter' in store.data))
  })

  it('survives a store that throws, e.g. storage disabled', () => {
    const broken: PrefStore = {
      getItem: () => {
        throw new Error('denied')
      },
      setItem: () => {
        throw new Error('denied')
      },
      removeItem: () => {
        throw new Error('denied')
      },
    }
    assert.equal(readPref(broken, 'k'), null)
    assert.doesNotThrow(() => writePref(broken, 'k', 'v'))
  })
})

describe('sort preference', () => {
  it('round-trips key and direction', () => {
    assert.deepEqual(parseSort(serializeSort({ key: 'status', direction: 'desc' }), ['name', 'status']), {
      key: 'status',
      direction: 'desc',
    })
  })

  it('ignores an unknown key or a malformed value', () => {
    assert.equal(parseSort('nope:asc', ['name', 'status']), null)
    assert.equal(parseSort('status:sideways', ['name', 'status']), null)
    assert.equal(parseSort('garbage', ['name', 'status']), null)
    assert.equal(parseSort(null, ['name', 'status']), null)
  })
})

describe('usableEngine', () => {
  it('keeps a stored engine that is still offered', () => {
    assert.equal(usableEngine('pulumi', ['terraform', 'pulumi']), 'pulumi')
  })

  it('drops one that is no longer enabled, so it cannot hide every workspace unseen', () => {
    assert.equal(usableEngine('pulumi', ['terraform', 'ansible']), '')
  })

  it('drops any engine when the selector is not shown, i.e. fewer than two engines', () => {
    assert.equal(usableEngine('terraform', ['terraform']), '')
  })

  it('leaves an empty filter empty', () => {
    assert.equal(usableEngine('', ['terraform', 'pulumi']), '')
  })
})
