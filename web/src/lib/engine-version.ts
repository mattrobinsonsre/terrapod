/**
 * Which binary a workspace's version field pins, and what it defaults to (#1559).
 *
 * A workspace carries two settings that both look like "which Terraform", and
 * they are **not** the same thing:
 *
 * - **engine** — which engine runs the workspace at all (`terraform`, `pulumi`).
 * - **execution backend** — `tofu` or `terraform`: the choice of binary *within*
 *   the Terraform engine. It is meaningless on any other engine, and a Pulumi
 *   workspace still carries a value for it — `tofu`, the platform default —
 *   because nothing consults `PulumiStrategy.default_execution_backend` when the
 *   workspace is created, and nothing reads the column on a Pulumi run either.
 *
 * The version column pins the version of *the engine the workspace runs*, so the
 * suggestions have to be keyed on the engine. Keying them on the execution
 * backend hands a Pulumi workspace a list of OpenTofu releases — merely useless
 * before #1559, and actively misleading now that the value chooses which Pulumi
 * CLI the run uses.
 *
 * `/api/v1/binary-cache/versions` serves `pulumi` alongside `tofu`/`terraform`,
 * so the only thing needed here is asking it for the right one.
 */

/** The execution backend a workspace falls back to when the API sends none. */
export const DEFAULT_EXECUTION_BACKEND = 'tofu'

/**
 * The Terraform-engine version the create form prefills.
 *
 * Kept equal to the server's `default_terraform_version`. The form said `1.11`
 * while the server had moved to `1.12`, so a workspace created through the UI
 * landed a minor behind one created through the API for no reason anybody
 * chose — the same drift the provider's autodiscovery-rule default had.
 */
export const DEFAULT_TERRAFORM_VERSION = '1.12'

/**
 * The binary-cache tool whose versions a workspace's version field should offer.
 *
 * Pulumi pins the Pulumi CLI; every other engine pins whichever Terraform-family
 * binary its execution backend names.
 */
export function versionToolFor(
  engine: string | null | undefined,
  executionBackend: string | null | undefined,
): string {
  if ((engine ?? '').trim().toLowerCase() === 'pulumi') return 'pulumi'
  return (executionBackend ?? '').trim() || DEFAULT_EXECUTION_BACKEND
}

/**
 * The version to prefill when creating a workspace on a given engine.
 *
 * Empty is a value, not an absence: the API reads it as "the deployment's
 * default". That is the right answer for Pulumi — its releases are 3.x, so
 * carrying the Terraform prefill across would offer a version that does not
 * exist — and it keeps this file from pinning a Pulumi version that would rot.
 */
export function defaultEngineVersion(engine: string | null | undefined): string {
  return (engine ?? '').trim().toLowerCase() === 'pulumi' ? '' : DEFAULT_TERRAFORM_VERSION
}
