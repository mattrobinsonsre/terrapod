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
  parseFieldList,
  parseVaultReference,
  usesField,
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
      if (!usesField(parseVaultReference(stored))) {
        // A template or format file reads the whole secret, and the server
        // refuses a `field` beside it (#1648): the box is hidden, and a stale
        // value in it must not reach the reference.
        assert.equal('field' in JSON.parse(out), false)
        assert.equal(out, stored)
        return
      }
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

// ── #1648: what the file holds ─────────────────────────────────────────

// Stored references for each kind of file content. Kept out of TABLE because
// the loops above edit `field`, which a template or format reference does not
// have (the server refuses one beside them).
const CONTENT: Record<string, string> = {
  template:
    '{"source":"vault","engine":"dynamic","mount":"aws","path":"creds/deploy","file":{"name":"~/.aws/credentials","template":"[default]\\naws_access_key_id = {{ access_key }}\\n","zz":1}}',
  format:
    '{"source":"vault","mount":"secret","path":"apps/db","file":{"name":"db.env","format":"env","fields":["DB_USER","DB_PASS"],"mode":"0400"}}',
  base64:
    '{"source":"vault","engine":"dynamic","mount":"gcp","path":"key/deploy","field":"private_key_data","file":{"name":"gcp/adc.json","encoding":"base64"}}',
}

describe('file content (#1648)', () => {
  for (const [name, stored] of Object.entries(CONTENT)) {
    it(`${name}: an unedited reference is returned verbatim`, () => {
      assert.equal(buildVaultReference(parseVaultReference(stored)), stored)
    })
    it(`${name}: editing the path changes only path`, () => {
      const out = edit(stored, { path: 'elsewhere' })
      assertOnlyChanged(stored, out, ['path'])
    })
    it(`${name}: renaming the file changes only file.name`, () => {
      const out = JSON.parse(edit(stored, { fileName: 'renamed' }))
      const before = JSON.parse(stored).file
      assert.equal(out.file.name, 'renamed')
      assert.deepEqual(Object.keys(out.file), Object.keys(before))
      for (const k of Object.keys(before).filter((k) => k !== 'name')) {
        assert.equal(JSON.stringify(out.file[k]), JSON.stringify(before[k]), `file.${k} changed`)
      }
    })
  }

  it('parses each kind', () => {
    const tpl = parseVaultReference(CONTENT.template)
    assert.equal(tpl.fileContent, 'template')
    assert.equal(tpl.template, '[default]\naws_access_key_id = {{ access_key }}\n')
    assert.equal(usesField(tpl), false)
    const fmt = parseVaultReference(CONTENT.format)
    assert.equal(fmt.fileContent, 'format')
    assert.equal(fmt.format, 'env')
    assert.equal(fmt.fields, 'DB_USER, DB_PASS')
    const b64 = parseVaultReference(CONTENT.base64)
    assert.equal(b64.fileContent, 'field')
    assert.equal(b64.encoding, 'base64')
    assert.equal(usesField(b64), true)
    assert.equal(usesField({ file: false, fileContent: 'template' }), true)
  })

  it('a template path edit never adds a field', () => {
    assert.equal('field' in JSON.parse(edit(CONTENT.template, { path: 'x' })), false)
  })

  it('editing the template text changes only file.template, verbatim', () => {
    const text = '  [prod]\naws_access_key_id={{access_key}}\n\n'
    const out = JSON.parse(edit(CONTENT.template, { template: text }))
    assert.equal(out.file.template, text, 'whitespace is kept as typed')
    assert.equal(out.file.zz, 1, 'an unknown file key survives')
    assertOnlyChanged(CONTENT.template, JSON.stringify(out), ['file'])
  })

  it('switching one field to a template drops field and encoding, keeps name and unknown keys', () => {
    const stored = CONTENT.base64
    const out = JSON.parse(
      edit(stored, { fileContent: 'template', template: '{{ private_key_data | base64decode }}' }),
    )
    assert.equal('field' in out, false)
    assert.deepEqual(out.file, {
      name: 'gcp/adc.json',
      template: '{{ private_key_data | base64decode }}',
    })
    assertOnlyChanged(stored, JSON.stringify(out), ['file', 'field'])
  })

  it('switching a template to a format writes json by default and the field list', () => {
    const out = JSON.parse(
      edit(CONTENT.template, { fileContent: 'format', fields: 'access_key,\nsecret_key' }),
    )
    assert.equal(out.file.template, undefined)
    assert.equal(out.file.format, 'json')
    assert.deepEqual(out.file.fields, ['access_key', 'secret_key'])
    assert.equal(out.file.zz, 1)
  })

  it('switching a format back to one field with a field typed restores field', () => {
    const out = JSON.parse(edit(CONTENT.format, { fileContent: 'field', field: ' DB_PASS ' }))
    assert.equal(out.field, 'DB_PASS')
    assert.equal(out.file.format, undefined)
    assert.equal(out.file.fields, undefined)
    assert.equal(out.file.mode, '0400', 'an unknown file key survives the switch')
  })

  it('switching to one field with none typed adds no empty field (the server names it)', () => {
    const out = JSON.parse(edit(CONTENT.template, { fileContent: 'field' }))
    assert.equal('field' in out, false)
    assert.equal(out.file.template, undefined)
  })

  it('turning the encoding off and on touches only file.encoding', () => {
    const off = edit(CONTENT.base64, { encoding: '' })
    assert.equal(JSON.parse(off).file.encoding, undefined)
    assertOnlyChanged(CONTENT.base64, off, ['file'])
    const on = edit(TABLE.fileNamed, { encoding: 'base64' })
    assert.deepEqual(JSON.parse(on).file, { name: 'gcp/adc.json', encoding: 'base64' })
    assertOnlyChanged(TABLE.fileNamed, on, ['file'])
  })

  it('emptying the fields list removes file.fields', () => {
    const out = JSON.parse(edit(CONTENT.format, { fields: ' , ' }))
    assert.equal(out.file.fields, undefined)
    assert.equal(out.file.format, 'env')
  })

  it('changing the format changes only file.format', () => {
    const out = JSON.parse(edit(CONTENT.format, { format: 'json' }))
    assert.equal(out.file.format, 'json')
    assert.deepEqual(out.file.fields, ['DB_USER', 'DB_PASS'])
  })

  it('a new templated reference has source first and no field', () => {
    const out = buildVaultReference({
      ...emptyVaultReference(),
      mount: 'pki',
      path: 'issue/web',
      engine: 'dynamic',
      file: true,
      fileName: 'tls/bundle.pem',
      fileContent: 'template',
      template: '{{certificate}}\n{{private_key}}\n{{ca_chain|lines}}\n',
    })
    assert.equal(
      out,
      '{"source":"vault","mount":"pki","path":"issue/web","engine":"dynamic","file":{"name":"tls/bundle.pem","template":"{{certificate}}\\n{{private_key}}\\n{{ca_chain|lines}}\\n"}}',
    )
  })

  it('parseFieldList splits on commas and newlines and drops blanks', () => {
    assert.deepEqual(parseFieldList(' a, b\nc,,\n '), ['a', 'b', 'c'])
    assert.deepEqual(parseFieldList(''), [])
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
