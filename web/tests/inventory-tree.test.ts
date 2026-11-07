/**
 * The resolved inventory's tree and YAML rendering (#2413 F8).
 *
 * Two properties carry the finding, and each is a RELATIONSHIP rather than a
 * value, so a fixture holding one shape proves nothing about it:
 *
 *  - the tree's roots are DERIVED, because `all` and `ungrouped` are ansible's
 *    implicit groups and never reach `group-children`;
 *  - a value is rendered unambiguously, which the display this replaces could
 *    not promise for any value containing a space.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { buildTree, varYamlLine } from '../src/lib/inventory-tree.ts'

describe('buildTree', () => {
  it('roots at the groups nothing nests under', () => {
    const { roots } = buildTree(
      { prod_web: ['web1'], prod_db: ['db1'] },
      { prod: ['prod_web', 'prod_db'] },
      ['web1', 'db1'],
    )
    // `prod` only: `prod_web` and `prod_db` are its children, so they are not
    // roots even though they are the groups that hold the hosts.
    assert.deepEqual(
      roots.map((r) => r.name),
      ['prod'],
    )
    assert.deepEqual(
      roots[0].children.map((c) => `${c.kind}:${c.name}`),
      ['group:prod_db', 'group:prod_web'],
    )
  })

  it('nests transitively, which the display it replaces could not', () => {
    // The old nesting view showed ONE hop while its own hint promised
    // transitive reach. `web1` is two levels down.
    const { roots } = buildTree({ prod_web: ['web1'] }, { prod: ['prod_web'] }, ['web1'])
    const prodWeb = roots[0].children[0]
    assert.equal(prodWeb.name, 'prod_web')
    assert.deepEqual(
      prodWeb.children.map((c) => c.name),
      ['web1'],
    )
  })

  it('collects hosts in no group at all, rather than dropping them', () => {
    // Ansible calls these `ungrouped` and that group is implicit, so it never
    // reaches `groups`. A host the view does not show is a host nobody knows
    // is there.
    const { roots, ungrouped } = buildTree({ prod: ['web1'] }, {}, ['web1', 'lonely'])
    assert.deepEqual(ungrouped, ['lonely'])
    assert.deepEqual(
      roots.map((r) => r.name),
      ['prod'],
    )
  })

  it('a group with no hosts and no children is still a node', () => {
    // Declared and empty is a real state, and a view that hid it would make a
    // mis-declared group invisible.
    const { roots } = buildTree({ empty: [] }, {}, [])
    assert.deepEqual(
      roots.map((r) => r.name),
      ['empty'],
    )
    assert.deepEqual(roots[0].children, [])
  })

  it('a group that appears only in the nesting is still a node', () => {
    // A parent whose members all come through a child reports none of its own
    // in `groups`, which the route's own comment says explicitly -- so the
    // group set cannot be read off `groups` alone.
    const { roots } = buildTree({}, { parent: ['child'] }, [])
    assert.deepEqual(
      roots.map((r) => r.name),
      ['parent'],
    )
    assert.deepEqual(
      roots[0].children.map((c) => c.name),
      ['child'],
    )
  })

  it('a cycle terminates instead of hanging', () => {
    // The API refuses a cycle on the nesting write, so this cannot arrive --
    // but this renders whatever the server sent, and a render that hangs is
    // worse than one that stops.
    // REACHABLE FROM A ROOT, which the first version of this test was not:
    // with only `a <-> b`, neither is a root, so the walk never started and
    // the `seen` guard was never exercised -- the test passed with the guard
    // deleted. `top` is a root, and walking it reaches the cycle.
    const { roots } = buildTree({}, { top: ['a'], a: ['b'], b: ['a'] }, [])
    assert.deepEqual(
      roots.map((r) => r.name),
      ['top'],
    )
    // The walk descends top -> a -> b and stops, rather than recurring.
    const a = roots[0].children[0]
    assert.equal(a.name, 'a')
    assert.equal(a.children[0].name, 'b')
    assert.deepEqual(a.children[0].children, [])
  })

  it('children and hosts are each sorted, so the order is stable', () => {
    const { roots } = buildTree(
      { g: ['zeta', 'alpha'] },
      { g: ['second', 'first'] },
      ['zeta', 'alpha'],
    )
    assert.deepEqual(
      roots[0].children.map((c) => c.name),
      ['first', 'second', 'alpha', 'zeta'],
    )
  })
})

describe('varYamlLine', () => {
  const prov = (value: unknown) => ({ value, origin: 'all', chain: [] })

  it('quotes a value containing a space, which is the whole finding', () => {
    // `ansible_ssh_common_args` routinely holds a space, a quote AND a colon.
    // The display this replaces space-joined `k=v` pairs, so the value could
    // not be read back at all.
    const v = '-o ProxyCommand="ssh -W %h:%p bastion"'
    const line = varYamlLine('ansible_ssh_common_args', prov(v), undefined)
    assert.equal(line, `ansible_ssh_common_args: ${JSON.stringify(v)}`)
    // The round trip is the property: the value survives whole.
    assert.equal(JSON.parse(line.slice(line.indexOf(': ') + 2)), v)
  })

  it('a colon or a # in a value cannot be misread as YAML syntax', () => {
    for (const v of ['a: b', 'trailing # comment', '{not: a map}', '- not a list']) {
      const line = varYamlLine('k', prov(v), undefined)
      assert.equal(JSON.parse(line.slice(3)), v, `round trip failed for ${v}`)
    }
  })

  it('keeps a non-string value typed rather than stringifying it', () => {
    // `--graph` stringifies and `--list` does not; the view renders the typed
    // value and only the COMPARISON behind `origin` is textual.
    assert.equal(varYamlLine('n', prov(3), undefined), 'n: 3')
    assert.equal(varYamlLine('b', prov(true), undefined), 'b: true')
    assert.equal(varYamlLine('l', prov(['a', 'b']), undefined), 'l: ["a","b"]')
  })

  it('falls back to the raw value when there is no provenance for the key', () => {
    // Provenance is served only with `?provenance=1`; the line must still
    // render without it rather than showing nothing.
    assert.equal(varYamlLine('k', undefined, 'raw'), 'k: "raw"')
  })
})
