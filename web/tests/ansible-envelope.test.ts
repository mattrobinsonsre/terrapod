/**
 * The form is the REPAIR path for a legacy credential (#2425 §0.3).
 *
 * `MACHINE_VALUE_FIELDS` went from six fields to two, so `user`, `become`,
 * `become_method` and `become_user` are refused on write. The form had already
 * dropped their inputs — but it round-tripped the whole parsed envelope on
 * save, so editing any other field sent the retired name back and the server
 * refused it. An operator could not fix, through the UI, a row the UI no longer
 * renders an input for, and the only other route was the API directly.
 *
 * The server half (tolerate on read, refuse on write) is
 * `services/tests/services/test_retired_credential_fields.py`. This is the half
 * that makes an edit CLEAN UP rather than fail.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import {
  RETIRED_MACHINE_FIELDS,
  parseAnsibleEnvelope,
  serializeAnsibleEnvelope,
} from '../src/lib/ansible-envelope.ts'

/** An alpha.1 machine credential: a real secret plus the four retired names. */
const LEGACY = JSON.stringify({
  private_key: '-----BEGIN KEY-----\nk\n-----END KEY-----',
  user: 'deploy',
  become: 'true',
  become_method: 'sudo',
  become_user: 'root',
})

describe('a legacy envelope is repaired by editing it', () => {
  it('drops every retired field on parse', () => {
    const fields = parseAnsibleEnvelope(LEGACY)
    for (const retired of RETIRED_MACHINE_FIELDS) {
      assert.ok(!(retired in fields), `${retired} survived the parse`)
    }
  })

  it('keeps the secret, so the form still shows what it has', () => {
    // The contrast, and the reason both assertions exist: a parse that dropped
    // everything would pass the one above and lose the credential.
    const fields = parseAnsibleEnvelope(LEGACY)
    assert.deepEqual(Object.keys(fields), ['private_key'])
    assert.match(fields.private_key, /BEGIN KEY/)
  })

  it('a round trip through the form writes the NARROWED envelope back', () => {
    // The whole defect: this is what the save sends. Before the fix it carried
    // the four retired names and the server answered 422.
    const fields = parseAnsibleEnvelope(LEGACY)
    const saved = serializeAnsibleEnvelope({ ...fields, password: 'p' })
    const parsed = JSON.parse(saved) as Record<string, string>
    assert.deepEqual(Object.keys(parsed).sort(), ['password', 'private_key'])
  })

  it('editing one field does not resurrect a retired one', () => {
    // The realistic operator action -- they came to rotate the key, not to
    // clean up field names -- and it has to be enough on its own.
    const fields = parseAnsibleEnvelope(LEGACY)
    const saved = serializeAnsibleEnvelope({ ...fields, private_key: 'NEW' })
    for (const retired of RETIRED_MACHINE_FIELDS) {
      assert.ok(!saved.includes(`"${retired}"`), `${retired} came back in ${saved}`)
    }
  })

  it('and the retired set is not empty, so these assertions mean something', () => {
    // A floor: if the set were emptied, every assertion above would pass while
    // checking nothing -- this project's most repeated gate defect.
    assert.ok(RETIRED_MACHINE_FIELDS.size >= 4, `only ${RETIRED_MACHINE_FIELDS.size} retired`)
  })
})

describe('the parser and serialiser keep their existing contracts', () => {
  it('an unreadable value is an empty envelope rather than a throw', () => {
    // The form renders for a row it cannot read; it does not crash the page.
    for (const bad of ['', '   ', 'not json', '[]', 'null', '42', '"a string"']) {
      assert.deepEqual(parseAnsibleEnvelope(bad), {}, `parse(${JSON.stringify(bad)}) was not {}`)
    }
  })

  it('a non-string field is dropped rather than stringified', () => {
    assert.deepEqual(parseAnsibleEnvelope(JSON.stringify({ private_key: 'k', become: 1 })), {
      private_key: 'k',
    })
  })

  it('an empty field is OMITTED, never sent as an empty string', () => {
    // The server reads `private_key: ""` as supplied-but-empty and refuses the
    // credential for having neither secret, which names the wrong problem.
    assert.equal(serializeAnsibleEnvelope({ private_key: 'k', password: '' }), '{"private_key":"k"}')
  })

  it('an envelope with nothing in it serialises to the empty STRING', () => {
    // Not `'{}'`: the edit path sends `value` only when it is non-empty, so
    // clearing every field must leave the stored credential alone rather than
    // replacing it with an empty object the server would refuse.
    assert.equal(serializeAnsibleEnvelope({}), '')
    assert.equal(serializeAnsibleEnvelope({ private_key: '', password: '' }), '')
  })
})
