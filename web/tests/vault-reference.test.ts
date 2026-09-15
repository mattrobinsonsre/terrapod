/**
 * The Vault reference round trip (#1619).
 *
 * The form renders only some of a reference's keys. These tests pin that an
 * edit changes the keys the user touched and nothing else: `method`, `data`,
 * unknown keys and keys inside `file` all come back byte-for-byte, in their
 * original positions. Before #1619 the UI rebuilt the reference from its
 * fields alone and dropped `method`/`data` on every save.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import {
  buildVaultReference,
  emptyVaultReference,
  parseVaultReference,
  type VaultReferenceValue,
} from '../src/lib/vault-reference.ts'

// Stored references as the API returns them (compact JSON, server key order).
const TABLE: Record<string, string> = {
  minimal: '{"source":"vault","mount":"secret","path":"apps/netbox","field":"apitoken"}',
  named: '{"source":"vault","vault":"default","mount":"kvv2","path":"apps/x","field":"token","engine":"kv2","method":"GET"}',
  fileDefault: '{"source":"vault","mount":"secret","path":"apps/gcp","field":"sa_json","file":{}}',
  fileNamed: '{"source":"vault","mount":"secret","path":"apps/gcp","field":"sa_json","file":{"name":"gcp/adc.json"}}',
  fileHome: '{"source":"vault","mount":"secret","path":"apps/aws","field":"credentials","file":{"name":"~/.aws/credentials"}}',
  postWithData:
    '{"source":"vault","engine":"dynamic","method":"POST","mount":"pki","path":"issue/example","field":"certificate","data":{"common_name":"app.example.internal","ttl":"1h","alt_names":["a","b"]}}',
  postWithFile:
    '{"source":"vault","engine":"dynamic","method":"POST","mount":"pki","path":"issue/example","field":"certificate","data":{"common_name":"app.example.internal"},"file":{"name":"tls/cert.pem"}}',
  unknownKeys:
    '{"future_top":{"nested":[1,2,{"x":null}]},"source":"vault","mount":"secret","path":"apps/x","field":"token","file":{"name":"x.json","mode":"0400","encoding":"base64"},"zz_last":true}',
  // A file key set through the API that this UI does not render yet (a
  // multi-field template). It has to survive a UI edit untouched.
  futureTemplate:
    '{"source":"vault","mount":"secret","path":"apps/aws","file":{"name":"~/.aws/credentials","format":"ini","template":"[default]\\naws_access_key_id = {{ .access_key }}\\naws_secret_access_key = {{ .secret_key }}\\n"}}',
}

/** Parse both strings and assert every key except `except` is identical and
 *  in the same order. Serialising each value is the byte-for-byte check. */
function assertOnlyChanged(before: string, after: string, except: string[]) {
  const a = JSON.parse(before) as Record<string, unknown>
  const b = JSON.parse(after) as Record<string, unknown>
  const keep = (o: Record<string, unknown>) => Object.keys(o).filter((k) => !except.includes(k))
  assert.deepEqual(keep(b), keep(a), 'untouched keys must keep their order')
  for (const k of keep(a)) {
    assert.equal(JSON.stringify(b[k]), JSON.stringify(a[k]), `key ${k} changed`)
  }
}

function edit(stored: string, patch: Partial<VaultReferenceValue>): string {
  return buildVaultReference({ ...parseVaultReference(stored), ...patch })
}

describe('an unedited reference', () => {
  for (const [name, stored] of Object.entries(TABLE)) {
    it(`${name} is returned verbatim`, () => {
      assert.equal(buildVaultReference(parseVaultReference(stored)), stored)
    })
  }
})

describe('editing one field changes only that key', () => {
  for (const [name, stored] of Object.entries(TABLE)) {
    it(`${name}: path`, () => {
      const out = edit(stored, { path: 'apps/changed' })
      assert.equal(JSON.parse(out).path, 'apps/changed')
      assertOnlyChanged(stored, out, ['path'])
    })
    it(`${name}: field`, () => {
      const out = edit(stored, { field: 'other' })
      assert.equal(JSON.parse(out).field, 'other')
      assertOnlyChanged(stored, out, ['field'])
    })
    it(`${name}: instance`, () => {
      const out = edit(stored, { instance: 'secondary' })
      assert.equal(JSON.parse(out).vault, 'secondary')
      assertOnlyChanged(stored, out, ['vault'])
    })
  }

  it('method and data survive an edit of an unrelated field', () => {
    const out = JSON.parse(edit(TABLE.postWithData, { mount: 'pki_int' }))
    assert.equal(out.method, 'POST')
    assert.deepEqual(out.data, {
      common_name: 'app.example.internal',
      ttl: '1h',
      alt_names: ['a', 'b'],
    })
    assert.equal(out.engine, 'dynamic')
  })

  it('an edit that restores the stored value is not an edit', () => {
    const v = parseVaultReference(TABLE.named)
    const out = buildVaultReference({ ...v, path: 'elsewhere' })
    assert.notEqual(out, TABLE.named)
    assert.equal(buildVaultReference({ ...v, path: 'apps/x' }), TABLE.named)
  })

  it('switching to kv2 removes engine and nothing else', () => {
    const out = edit(TABLE.postWithData, { engine: 'kv2' })
    assert.equal('engine' in JSON.parse(out), false)
    assertOnlyChanged(TABLE.postWithData, out, ['engine'])
  })

  it('switching to dynamic adds engine and nothing else', () => {
    const out = edit(TABLE.minimal, { engine: 'dynamic' })
    assert.equal(JSON.parse(out).engine, 'dynamic')
    assertOnlyChanged(TABLE.minimal, out, ['engine'])
  })

  it('clearing the instance removes the vault key', () => {
    const out = edit(TABLE.named, { instance: '' })
    assert.equal('vault' in JSON.parse(out), false)
    assertOnlyChanged(TABLE.named, out, ['vault'])
  })
})

describe('file delivery', () => {
  it('parses the file toggle and name', () => {
    assert.equal(parseVaultReference(TABLE.minimal).file, false)
    const d = parseVaultReference(TABLE.fileDefault)
    assert.equal(d.file, true)
    assert.equal(d.fileName, '')
    const n = parseVaultReference(TABLE.fileHome)
    assert.equal(n.file, true)
    assert.equal(n.fileName, '~/.aws/credentials')
  })

  it('a future file key (template, format) survives an unrelated edit and a rename', () => {
    const stored = TABLE.futureTemplate
    const before = JSON.parse(stored).file

    const pathEdit = edit(stored, { path: 'apps/aws-prod' })
    assert.deepEqual(JSON.parse(pathEdit).file, before)
    assert.equal(JSON.stringify(JSON.parse(pathEdit).file), JSON.stringify(before))

    const renamed = JSON.parse(edit(stored, { fileName: '~/.aws/credentials.prod' })).file
    assert.equal(renamed.name, '~/.aws/credentials.prod')
    assert.equal(renamed.template, before.template)
    assert.equal(renamed.format, 'ini')
    assert.deepEqual(Object.keys(renamed), Object.keys(before), 'file key order kept')
  })

  it('a reference with no field (a template reads the whole secret) keeps having none', () => {
    // The form always writes mount/path/field for a new reference, but an
    // existing one without `field` must not gain an empty one on edit.
    const out = JSON.parse(edit(TABLE.futureTemplate, { path: 'apps/other' }))
    assert.equal('field' in out, false)
  })

  for (const name of ['fileDefault', 'fileNamed', 'fileHome', 'postWithFile', 'unknownKeys', 'futureTemplate']) {
    it(`${name}: toggling it off removes file and nothing else`, () => {
      const stored = TABLE[name]
      const out = edit(stored, { file: false })
      assert.equal('file' in JSON.parse(out), false)
      assertOnlyChanged(stored, out, ['file'])
    })
  }

  for (const name of ['minimal', 'named', 'postWithData']) {
    it(`${name}: toggling it on adds file and nothing else`, () => {
      const stored = TABLE[name]
      const out = edit(stored, { file: true, fileName: 'creds/token' })
      assert.deepEqual(JSON.parse(out).file, { name: 'creds/token' })
      assertOnlyChanged(stored, out, ['file'])
    })
  }

  it('an unnamed file is sent without a name, so the server default applies', () => {
    const out = JSON.parse(edit(TABLE.minimal, { file: true, fileName: '  ' }))
    assert.deepEqual(out.file, {})
  })

  it('renaming keeps the other keys inside file', () => {
    const out = edit(TABLE.unknownKeys, { fileName: 'renamed.json' })
    assert.deepEqual(JSON.parse(out).file, {
      name: 'renamed.json',
      mode: '0400',
      encoding: 'base64',
    })
    assertOnlyChanged(TABLE.unknownKeys, out, ['file'])
  })

  it('clearing the name keeps file delivery on with the default name', () => {
    const out = edit(TABLE.fileNamed, { fileName: '' })
    assert.deepEqual(JSON.parse(out).file, {})
  })

  it('a name is trimmed', () => {
    const out = edit(TABLE.minimal, { file: true, fileName: '  gcp/adc.json ' })
    assert.deepEqual(JSON.parse(out).file, { name: 'gcp/adc.json' })
  })
})

describe('a new reference', () => {
  it('has source first and the coordinates present', () => {
    const out = buildVaultReference({
      ...emptyVaultReference(),
      mount: ' secret ',
      path: 'apps/x',
      field: 'token',
    })
    assert.equal(out, '{"source":"vault","mount":"secret","path":"apps/x","field":"token"}')
  })

  it('carries instance, engine and file when set', () => {
    const out = JSON.parse(
      buildVaultReference({
        ...emptyVaultReference(),
        instance: 'default',
        mount: 'database',
        path: 'creds/ro',
        field: 'password',
        engine: 'dynamic',
        file: true,
        fileName: 'db/password',
      }),
    )
    assert.deepEqual(out, {
      source: 'vault',
      vault: 'default',
      mount: 'database',
      path: 'creds/ro',
      field: 'password',
      engine: 'dynamic',
      file: { name: 'db/password' },
    })
  })

  it('an unparseable stored value starts from nothing, never echoed back', () => {
    const v = parseVaultReference('{not json')
    assert.equal(v.raw, '')
    const out = JSON.parse(buildVaultReference({ ...v, mount: 'm', path: 'p', field: 'f' }))
    assert.deepEqual(out, { source: 'vault', mount: 'm', path: 'p', field: 'f' })
  })
})
