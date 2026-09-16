import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { parsePlanLogIndex, stripAnsi } from '../src/lib/plan-log-index.ts'

/**
 * The parser behind the plan-log index (#1590).
 *
 * The fixtures are real `terraform` and `tofu` plan output rather than lines
 * invented to match the patterns — that is the whole point of the exercise,
 * because a hand-written fixture agrees with whatever regex you just wrote and
 * tells you nothing about the engine you did not have to hand.
 */

const TERRAFORM = `Terraform used the selected providers to generate the following execution
plan. Resource actions are indicated with the following symbols:
  + create
  ~ update in-place
-/+ destroy and then create replacement
  - destroy

Terraform will perform the following actions:

  # aws_instance.web will be updated in-place
  ~ resource "aws_instance" "web" {
        id   = "i-0abc"
      ~ tags = {
          + "env" = "prod"
        }
    }

  # aws_s3_bucket.assets will be created
  + resource "aws_s3_bucket" "assets" {
      + bucket = "assets"
      + id     = (known after apply)
    }

  # aws_db_instance.main must be replaced
-/+ resource "aws_db_instance" "main" {
      ~ engine_version = "14.7" -> "15.3" # forces replacement
    }

  # aws_iam_role.legacy will be destroyed
  - resource "aws_iam_role" "legacy" {
      - name = "legacy" -> null
    }

  # data.aws_ami.ubuntu will be read during apply
 <= data "aws_ami" "ubuntu" {
      + id = (known after apply)
    }

Plan: 1 to add, 1 to change, 2 to destroy.
`

const TOFU = `OpenTofu used the selected providers to generate the following execution
plan. Resource actions are indicated with the following symbols:
  + create
  ~ update in-place

OpenTofu will perform the following actions:

  # module.vpc["eu west 1"].aws_subnet.private[0] will be created
  + resource "aws_subnet" "private" {
      + cidr_block = "10.0.1.0/24"
    }

  # module.a.module.b.aws_instance.node["key.with.dots"] will be updated in place
  ~ resource "aws_instance" "node" {
    }

  # aws_instance.tainted is tainted, so must be replaced
-/+ resource "aws_instance" "tainted" {
    }

  # aws_instance.forced will be replaced, as requested
-/+ resource "aws_instance" "forced" {
    }

  # aws_instance.legacy will be imported
    resource "aws_instance" "legacy" {
    }

  # aws_instance.web has moved to aws_instance.app

Plan: 3 to add, 1 to change, 2 to destroy.
`

function actions(text: string) {
  return parsePlanLogIndex(text).map((e) => e.action)
}

function addresses(text: string) {
  return parsePlanLogIndex(text).map((e) => e.address)
}

describe('parsePlanLogIndex', () => {
  it('finds every change terraform announces, in log order', () => {
    assert.deepEqual(actions(TERRAFORM), [
      'update',
      'create',
      'replace',
      'destroy',
      'read',
      'summary',
    ])
    assert.deepEqual(addresses(TERRAFORM), [
      'aws_instance.web',
      'aws_s3_bucket.assets',
      'aws_db_instance.main',
      'aws_iam_role.legacy',
      'data.aws_ami.ubuntu',
      '',
    ])
  })

  it('finds every change tofu announces, including the replacement spellings', () => {
    assert.deepEqual(actions(TOFU), [
      'create',
      'update',
      'replace',
      'replace',
      'import',
      'move',
      'summary',
    ])
  })

  it('keeps a module, count and for_each address intact, quoted keys and all', () => {
    const [first, second] = parsePlanLogIndex(TOFU)
    assert.equal(first.address, 'module.vpc["eu west 1"].aws_subnet.private[0]')
    assert.equal(second.address, 'module.a.module.b.aws_instance.node["key.with.dots"]')
  })

  it('accepts both spellings of an in-place update', () => {
    // terraform writes "in-place"; the tofu fixture above writes "in place".
    assert.deepEqual(actions('  # a.b will be updated in-place'), ['update'])
    assert.deepEqual(actions('  # a.b will be updated in place'), ['update'])
  })

  it('reads a tainted resource as a replacement, not as its own phrasing', () => {
    const [entry] = parsePlanLogIndex('  # aws_instance.x is tainted, so must be replaced')
    assert.equal(entry.action, 'replace')
    // The address must stop before "is tainted", not swallow it.
    assert.equal(entry.address, 'aws_instance.x')
  })

  it('points at the line to scroll to', () => {
    const entries = parsePlanLogIndex(TERRAFORM)
    const lines = TERRAFORM.split('\n')
    for (const entry of entries) {
      assert.ok(
        lines[entry.line].includes(entry.address || 'Plan:'),
        `entry ${JSON.stringify(entry)} does not point at its own line`,
      )
    }
  })

  it('matches lines the engine has colourised', () => {
    const coloured =
      '\x1b[1m  # aws_s3_bucket.assets\x1b[0m\x1b[1m will be created\x1b[0m\n' +
      '\x1b[32m  + resource "aws_s3_bucket" "assets" {\x1b[0m\n' +
      '\x1b[1mPlan:\x1b[0m 1 to add, 0 to change, 0 to destroy.\n'
    assert.deepEqual(actions(coloured), ['create', 'summary'])
    assert.deepEqual(addresses(coloured), ['aws_s3_bucket.assets', ''])
  })

  it('takes a no-changes plan as its own summary', () => {
    assert.deepEqual(
      actions('No changes. Your infrastructure matches the configuration.\n'),
      ['summary'],
    )
  })

  it('takes a refresh-only plan as its own summary', () => {
    assert.deepEqual(
      actions('No changes. Your infrastructure still matches the configuration.\n'),
      ['summary'],
    )
  })

  it('indexes a destroy plan', () => {
    const destroy = `  # aws_instance.a will be destroyed
  - resource "aws_instance" "a" {
    }

Plan: 0 to add, 0 to change, 1 to destroy.
`
    assert.deepEqual(actions(destroy), ['destroy', 'summary'])
  })

  it('summarises once, however many tallies the log ends up holding', () => {
    const twice = `${TERRAFORM}\nApply complete!\n\nPlan: 1 to add, 0 to change, 0 to destroy.\n`
    assert.equal(actions(twice).filter((a) => a === 'summary').length, 1)
  })

  it('grows as the log streams, without moving the entries already found', () => {
    const lines = TERRAFORM.split('\n')
    const partial = lines.slice(0, 20).join('\n')
    const early = parsePlanLogIndex(partial)
    const complete = parsePlanLogIndex(TERRAFORM)

    assert.ok(early.length > 0 && early.length < complete.length)
    assert.deepEqual(complete.slice(0, early.length), early)
    // The summary only arrives at the end, which is why the list must grow.
    assert.ok(!early.some((e) => e.action === 'summary'))
    assert.equal(complete.at(-1)?.action, 'summary')
  })

  it('leaves out what it does not recognise, and never throws', () => {
    assert.deepEqual(parsePlanLogIndex(''), [])
    assert.deepEqual(parsePlanLogIndex(null), [])
    assert.deepEqual(parsePlanLogIndex(undefined), [])
    assert.deepEqual(parsePlanLogIndex('# not a plan line at all\nrandom text\n'), [])
    // A comment that is not an announcement must not become an entry.
    assert.deepEqual(parsePlanLogIndex('  # aws_instance.web is up to date'), [])
  })
})

/**
 * Diagnostics. A failed plan is when the log is longest and the reason hardest
 * to find, so these are the entries that earn the picker its keep.
 *
 * Both fixtures are real `tofu` output: the first as `-no-color` prints it (no
 * frame at all), the second as the runner logs it (box-drawing gutter, ANSI
 * intact). Only the second is what the viewer actually receives, and an earlier
 * pattern built against the first alone would have matched nothing in it.
 */
const PLAIN_ERRORS = `
Error: Invalid function argument

  on main.tf line 2, in locals:
   2:   missing_file = file("does-not-exist.txt")

Invalid value for "path" parameter: no file exists at "does-not-exist.txt".

Error: Reference to undeclared input variable

  on main.tf line 8, in resource "terraform_data" "uses_missing_var":
   8:   input = var.never_declared

An input variable with the name "never_declared" has not been declared.
`

// The gutter is U+2502, wrapped in the colour codes the engine emits.
const FRAMED_ERRORS =
  '\x1b[31m╷\x1b[0m\n' +
  '\x1b[31m│\x1b[0m \x1b[1m\x1b[31mError: \x1b[0m\x1b[1mInvalid index\x1b[0m\n' +
  '\x1b[31m│\x1b[0m \n' +
  '\x1b[31m│\x1b[0m   on main.tf line 3, in locals:\n' +
  '\x1b[31m╵\x1b[0m\n' +
  '\x1b[33m│\x1b[0m \x1b[1m\x1b[33mWarning: \x1b[0m\x1b[1mDeprecated attribute\x1b[0m\n'

describe('parsePlanLogIndex — diagnostics', () => {
  it('indexes each error in an unframed log', () => {
    assert.deepEqual(actions(PLAIN_ERRORS), ['error', 'error'])
    assert.deepEqual(addresses(PLAIN_ERRORS), [
      'Invalid function argument',
      'Reference to undeclared input variable',
    ])
  })

  it('indexes errors and warnings through the frame the runner logs', () => {
    assert.deepEqual(actions(FRAMED_ERRORS), ['error', 'warning'])
    assert.deepEqual(addresses(FRAMED_ERRORS), ['Invalid index', 'Deprecated attribute'])
  })

  it('takes the title only, not the prose beneath it', () => {
    const [entry] = parsePlanLogIndex(PLAIN_ERRORS)
    assert.equal(entry.address, 'Invalid function argument')
    assert.ok(!entry.address.includes('no file exists'))
  })

  it('points at the line the diagnostic starts on', () => {
    const lines = FRAMED_ERRORS.split('\n')
    for (const entry of parsePlanLogIndex(FRAMED_ERRORS)) {
      assert.ok(
        stripAnsi(lines[entry.line]).includes(entry.address),
        `entry ${JSON.stringify(entry)} does not point at its own line`,
      )
    }
  })

  it('indexes changes and errors together, in log order', () => {
    const mixed = `  # aws_s3_bucket.a will be created\n\nError: Invalid index\n\n  # aws_s3_bucket.b will be destroyed\n`
    assert.deepEqual(actions(mixed), ['create', 'error', 'destroy'])
  })

  it('does not mistake ordinary prose for a diagnostic', () => {
    // A plan body can contain the word, and an attribute can be named for it.
    assert.deepEqual(parsePlanLogIndex('      + error_message = "boom"'), [])
    assert.deepEqual(parsePlanLogIndex('Errors are reported at the end.'), [])
  })
})

describe('stripAnsi', () => {
  it('removes the escapes the viewer renders but the parser must not see', () => {
    assert.equal(stripAnsi('\x1b[1mbold\x1b[0m plain'), 'bold plain')
    assert.equal(stripAnsi('no escapes here'), 'no escapes here')
  })
})
