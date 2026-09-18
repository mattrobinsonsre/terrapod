// Which tool a workspace's version field pins (#1559).
//
// The bug this guards: a Pulumi workspace still carries an execution backend of
// `tofu` — nothing sets it from the Pulumi strategy and nothing reads it — so
// keying the version suggestions on the backend offered a Pulumi workspace a
// list of OpenTofu releases, for a field that chooses which Pulumi CLI runs.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  DEFAULT_EXECUTION_BACKEND,
  DEFAULT_TERRAFORM_VERSION,
  defaultEngineVersion,
  versionToolFor,
} from '../src/lib/engine-version.ts'

test('a Pulumi workspace pins the Pulumi CLI, whatever backend it carries', () => {
  // The three shapes a Pulumi workspace's backend actually takes: the platform
  // default it was created with, an explicitly set one, and none at all.
  assert.equal(versionToolFor('pulumi', 'tofu'), 'pulumi')
  assert.equal(versionToolFor('pulumi', 'terraform'), 'pulumi')
  assert.equal(versionToolFor('pulumi', ''), 'pulumi')
  assert.equal(versionToolFor('pulumi', null), 'pulumi')
  assert.equal(versionToolFor('pulumi', undefined), 'pulumi')
})

test('every other engine pins the binary its execution backend names', () => {
  assert.equal(versionToolFor('terraform', 'tofu'), 'tofu')
  assert.equal(versionToolFor('terraform', 'terraform'), 'terraform')
  // An older API sends no engine at all; the column's default is Terraform.
  assert.equal(versionToolFor(undefined, 'terraform'), 'terraform')
  assert.equal(versionToolFor(null, 'tofu'), 'tofu')
  // An engine this page has not heard of still runs a Terraform-family binary
  // as far as the version field is concerned — better a usable list than none.
  assert.equal(versionToolFor('ansible', 'terraform'), 'terraform')
})

test('a missing execution backend falls back the way the API record does', () => {
  assert.equal(versionToolFor('terraform', ''), DEFAULT_EXECUTION_BACKEND)
  assert.equal(versionToolFor('terraform', null), DEFAULT_EXECUTION_BACKEND)
  assert.equal(versionToolFor(undefined, undefined), DEFAULT_EXECUTION_BACKEND)
})

test('the engine name is matched loosely, since it is data off the wire', () => {
  assert.equal(versionToolFor('Pulumi', 'tofu'), 'pulumi')
  assert.equal(versionToolFor('  pulumi  ', 'tofu'), 'pulumi')
})

test('the create form prefills a version the selected engine can actually run', () => {
  // Empty is a value, not an absence: the API reads it as the deployment's
  // default. Pulumi releases are 3.x, so carrying the Terraform prefill across
  // would submit a version that does not exist.
  assert.equal(defaultEngineVersion('pulumi'), '')
  assert.equal(defaultEngineVersion('terraform'), DEFAULT_TERRAFORM_VERSION)
  assert.equal(defaultEngineVersion(undefined), DEFAULT_TERRAFORM_VERSION)
})
