/**
 * Which label the one native variable category wears, per engine (#1898).
 *
 * There is a single stored category for "the parameters the platform supplies
 * to this run" — Terraform's input variables, Pulumi's stack config, Ansible's
 * extra vars are one role with three deliveries. But an operator looking at a
 * workspace is looking at *their* engine, and naming another engine's mechanism
 * at them is the confusion this whole change set out to remove: a Pulumi
 * workspace offering "Terraform", or a Terraform one offering stack config.
 *
 * So the value is one thing and the label is the engine's own word for it. The
 * neutral name appears only where there is genuinely no engine to name — a
 * variable set is org-scoped and reaches workspaces of either kind.
 */
export type NativeCategoryKey = 'categoryTerraform' | 'categoryPulumiConfig' | 'categoryNative'

/**
 * The message key for the native category on a workspace running `engine`.
 *
 * Pass nothing where there is no single engine (a variable set), and the
 * neutral name is used. An unrecognised engine also reads as Terraform, which
 * is what the server does with an unset one.
 */
export function nativeCategoryKey(engine?: string | null): NativeCategoryKey {
  if (!engine) return 'categoryNative'
  return engine === 'pulumi' ? 'categoryPulumiConfig' : 'categoryTerraform'
}

/**
 * The message key for any category, or null where the label is not translated.
 *
 * The two git-auth categories carry credentials and are labelled in English on
 * purpose — they name a protocol, not a concept that reads differently per
 * engine.
 */
export function categoryKey(category: string, engine?: string | null): string | null {
  if (category === 'native') return nativeCategoryKey(engine)
  if (category === 'env') return 'categoryEnv'
  return null
}
