/**
 * The shared field metadata behind the workspace CREATE and EDIT forms (#2253).
 *
 * In `lib/` rather than beside the components because that is where this tree
 * puts logic a test can import: every file in `web/tests/` imports from
 * `src/lib/*.ts` and none imports a `.tsx`, since the runner is bare
 * `node --test` with no transpiler. `dependencyMet` is the piece of real
 * logic here -- which field is meaningful -- and leaving it in a file holding
 * JSX meant it could not be unit-tested at all. `groupSummary` was the second,
 * and went with the collapsing (#2413 F4).
 *
 * `components/workspace-fields.tsx` re-exports everything here, so either
 * import path works and no consumer had to change.
 *
 * Its own file, beside `ansible-credential-fields.tsx`, for the same reason: it
 * is used by two surfaces -- `workspaces/page.tsx`'s create form and
 * `workspaces/[id]/page.tsx`'s Configuration tab -- so living inside either
 * would make the other import a page's internals.
 *
 * WHY IT EXISTS AT ALL, because the measurement is the whole argument: before
 * this file the two forms shared only chrome (`nav-bar`, `page-header`,
 * `error-banner`, ...) and NOT ONE field component. With nothing holding them
 * in step they diverged until create offered 13 controls against the 52
 * attributes `_create_workspace_impl` accepts -- the entire Operations group,
 * the entire Ansible block, drift, AI summary, security scan and VCS workflow
 * reachable only by creating a workspace and then editing it. Adding the
 * missing fields by hand would have restarted the same drift, so the fix is
 * that both forms read their field set from `FIELDS` below.
 *
 * `FIELDS` is therefore the single source of truth for which attribute belongs
 * to which group and which boolean gates it. The
 * parity gate reads it rather than a hand-kept list, so a new workspace
 * attribute that reaches one form and not the other fails a test instead of
 * being noticed years later.
 *
 * WHAT IS SHARED, AND WHAT IS NOT -- the line is STRUCTURE against CONTROL
 * IDIOM, and this paragraph used to draw it in the wrong place (#2413 F4).
 *
 * It said the autodiscovery rule form and the bulk update form are "NOT built
 * from this file", on the grounds that their idioms differ: bulk update uses
 * tri-state selects because "unchanged" is a real value there, and
 * autodiscovery leaves the Ansible resource pair empty to mean "take the
 * deployment default", so folding them in would force one of those to lie.
 *
 * That reasoning is right, and it is about the CONTROL -- which input, and what
 * an empty value means. It says nothing about STRUCTURE. Which group an
 * attribute belongs to, in what order the groups come, and which boolean gates
 * it are properties of the ATTRIBUTE, identical on every surface that sets it;
 * and four forms disagreeing about where a field lives is the drift this file
 * exists to stop. So:
 *
 *   - SHARED: `group`, `dependsOn`, and the order of `GROUPS`. The admin forms
 *     read them and render their own controls inside the same sections.
 *   - PER SURFACE: the control type, and what an empty value means there.
 *
 * Create and edit additionally share the controls themselves, because both set
 * a concrete value for a single workspace.
 */


// Defined here rather than imported, which is this tree's convention for a
// field-group component: `ansible-credential-fields.tsx` and
// `vault-file-delivery-fields.tsx` each carry their own. Two sets, because the
// two forms are visually different by design -- the edit tab is a dense
// definition list and the create form is a roomier standalone form -- and
// collapsing them would change the look of both surfaces in a change that is
// supposed to be about structure.
//
// Both carry a visible disabled state, which #2413 F20 made load-bearing
// rather than cosmetic: a field whose gate is off is now RENDERED instead of
// removed, so the operator has to be able to see that it cannot be typed into.
// Without this the dark-theme native disabled rendering is nearly
// indistinguishable from an ordinary input, and "disabled but visible" reads
// as "enabled and ignoring me".
export const EDIT_INPUT =
  'mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:opacity-50 disabled:cursor-not-allowed'
export const CREATE_INPUT =
  'w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent disabled:opacity-50 disabled:cursor-not-allowed'

/**
 * Which form is rendering.
 *
 * `view` is the edit tab with `editing` false -- a `<dd>` showing the stored
 * value. `edit` is the edit tab mid-edit. `create` is the create form, which
 * has no stored value to show and so has no `view` state at all.
 */
export type FieldMode = 'view' | 'edit' | 'create'

/** The groups, in the order both forms render them. */
export const GROUPS = [
  'identity',
  'execution',
  'operations',
  'ansible',
  'pools',
  'vcs',
  'governance',
  'ai',
  'security',
  'metadata',
  'cloudIdentity',
  'diagnostics',
] as const

export type GroupId = (typeof GROUPS)[number]

export interface FieldSpec {
  /** The API attribute name. The single source of truth for this field. */
  attr: string
  /** The DOM id both forms use, so one e2e locator serves both. */
  id: string
  group: GroupId
  /** The boolean attribute that makes this field meaningful. */
  dependsOn?: string
  /** Engines this field applies to; absent means all of them. */
  engines?: readonly string[]
  /**
   * Set by the server rather than the operator, or handled by its own
   * component outside the generic groups. Excluded from the parity gate's
   * expectation WITH A REASON, which is what stops the exclusion list
   * becoming a dumping ground.
   */
  notInForms?: string
  /**
   * Settable at create and immutable afterwards.
   *
   * `engine` is the only one: the native create route reads it off the body
   * (`attrs.get("engine")`, defaulting to Terraform) and nothing on the PATCH
   * path accepts it, because a workspace holds state produced by its own
   * engine. So the create form offers a selector, the edit tab shows it
   * read-only, and `toAttributes` must still emit it on create -- dropping it
   * would silently make every workspace Terraform and leave Pulumi
   * uncreatable from the UI, which is exactly the #1535 defect over again.
   */
  createOnly?: boolean
  /**
   * Older payload spellings the API still accepts for this same setting.
   *
   * Not a convenience: the parity gate derives what the create route accepts,
   * so an alias it does not know about reads as a field the forms are missing.
   * `auto-apply` is the original boolean that `auto-apply-mode` replaced, and
   * `agent-pool-id` is the single-pool form of `agent-pool-ids`. Offering the
   * superseded spelling as a SECOND control would let an operator set two
   * things that mean one thing, so each is listed here rather than rendered.
   */
  alsoAccepts?: readonly string[]
}

/**
 * Every workspace attribute the two forms set.
 *
 * Order within a group is the render order. Keep `attr` exactly as the API
 * spells it -- the parity gate intersects this list with what
 * `_create_workspace_impl` reads, so a typo here shows up as a missing field
 * rather than as a silently ignored one.
 */
export const FIELDS: readonly FieldSpec[] = [
  // --- identity ---------------------------------------------------------
  { attr: 'name', id: 'ws-name', group: 'identity' },
  {
    attr: 'engine',
    id: 'ws-engine',
    group: 'identity',
    // Read on CREATE by the native route rather than by
    // `_create_workspace_impl` -- `workspace_extensions` takes it off the body
    // and passes it down as a parameter -- and accepted on no PATCH path,
    // because `NEVER_BULK_SETTABLE` rightly calls it identity. So it must be
    // SENT at create and shown read-only afterwards.
    createOnly: true,
  },

  // --- execution --------------------------------------------------------
  { attr: 'execution-mode', id: 'ws-exec', group: 'execution' },
  {
    attr: 'execution-backend',
    id: 'ws-backend',
    group: 'execution',
    // Terraform only -- it picks the tofu-or-terraform BINARY, which a Pulumi
    // workspace has no use for. The hand-written edit tab gated it on
    // `!isPulumi` and `e2e/tests/pulumi-workspace.spec.ts` asserts it is
    // absent there, commented 'Terraform-only settings are hidden rather than
    // shown and ignored'. The restriction was lost when the fields were ported
    // into this table, so the create form would have offered a Pulumi
    // workspace a binary picker.
    engines: ['terraform'],
  },
  {
    attr: 'engine-version',
    id: 'ws-version',
    group: 'execution',
    // One version, two spellings (#1559): `engine-version` is canonical and
    // `terraform-version` is kept for `go-tfe`, which holds it as a constant.
    // The API reads either; the form offers one control.
    alsoAccepts: ['terraform-version'],
  },
  { attr: 'working-directory', id: 'ws-workdir', group: 'execution' },
  {
    attr: 'auto-apply-mode',
    id: 'ws-auto-apply-mode',
    group: 'execution',
    alsoAccepts: ['auto-apply'],
  },
  { attr: 'terragrunt-enabled', id: 'ws-terragrunt', group: 'execution', engines: ['terraform'] },
  {
    attr: 'terragrunt-version',
    id: 'ws-terragrunt-version',
    group: 'execution',
    dependsOn: 'terragrunt-enabled',
    engines: ['terraform'],
  },
  { attr: 'pulumi-bind-plan', id: 'ws-bind-plan', group: 'execution', engines: ['pulumi'] },
  // The three with a real default. They used to be called out here because a
  // value like "10" cannot be summarised as set-or-unset, which mattered while
  // a group could collapse behind a summary. Nothing collapses now (#2413 F4),
  // so they are ordinary fields and the note is kept only to say so.
  { attr: 'parallelism', id: 'ws-parallelism', group: 'execution' },
  { attr: 'resource-cpu', id: 'ws-cpu', group: 'execution' },
  { attr: 'resource-memory', id: 'ws-mem', group: 'execution' },

  // --- operations -------------------------------------------------------
  { attr: 'plan-apply-enabled', id: 'ws-plan-apply-enabled', group: 'operations' },
  { attr: 'configure-enabled', id: 'ws-configure-enabled', group: 'operations' },
  {
    attr: 'auto-configure-enabled',
    id: 'ws-auto-configure-enabled',
    group: 'operations',
    dependsOn: 'configure-enabled',
  },
  {
    attr: 'configure-after-apply',
    id: 'ws-configure-after-apply',
    group: 'operations',
    dependsOn: 'configure-enabled',
  },
  {
    attr: 'configure-all-hosts',
    id: 'ws-configure-all-hosts',
    group: 'operations',
    dependsOn: 'configure-enabled',
  },

  // --- ansible ----------------------------------------------------------
  { attr: 'config-directory', id: 'ws-config-dir', group: 'ansible', dependsOn: 'configure-enabled' },
  {
    attr: 'default-playbook',
    id: 'ws-default-playbook',
    group: 'ansible',
    dependsOn: 'configure-enabled',
  },
  { attr: 'default-limit', id: 'ws-default-limit', group: 'ansible', dependsOn: 'configure-enabled' },
  {
    attr: 'inventory-paths',
    id: 'ws-inventory-paths',
    group: 'ansible',
    dependsOn: 'configure-enabled',
  },
  // Which inventory SOURCES under those paths ansible may resolve (#2033).
  // Two switches rather than one, because a declared plugin config names a
  // collection that can be pinned and inspected while a script names nothing,
  // so permitting the first is not permitting the second.
  {
    attr: 'dynamic-inventory-enabled',
    id: 'ws-dynamic-inventory-enabled',
    group: 'ansible',
    dependsOn: 'configure-enabled',
  },
  {
    attr: 'inventory-scripts-enabled',
    id: 'ws-inventory-scripts-enabled',
    group: 'ansible',
    dependsOn: 'configure-enabled',
  },
  // Whether a configure verifies the SSH host key of the hosts it targets
  // (#2318). The only boolean in this group whose default is TRUE, which is
  // why both forms seed it rather than letting an absent value read as off:
  // on IS ansible's own behaviour, so an unchecked box would misreport a
  // workspace nobody has touched as accepting any host key.
  {
    attr: 'ansible-host-key-checking',
    id: 'ws-ansible-host-key-checking',
    group: 'ansible',
    dependsOn: 'configure-enabled',
  },
  { attr: 'ansible-version', id: 'ws-ansible-version', group: 'ansible', dependsOn: 'configure-enabled' },
  {
    attr: 'ansible-execution-environment',
    id: 'ws-ansible-ee',
    group: 'ansible',
    dependsOn: 'configure-enabled',
  },
  {
    attr: 'ansible-collections',
    id: 'ws-ansible-collections',
    group: 'ansible',
    dependsOn: 'configure-enabled',
  },
  { attr: 'ee-resource-cpu', id: 'ws-ee-cpu', group: 'ansible', dependsOn: 'configure-enabled' },
  { attr: 'ee-resource-memory', id: 'ws-ee-mem', group: 'ansible', dependsOn: 'configure-enabled' },

  // --- agent pools ------------------------------------------------------
  {
    attr: 'agent-pool-ids',
    id: 'ws-pool',
    group: 'pools',
    alsoAccepts: ['agent-pool-id'],
  },

  // --- vcs --------------------------------------------------------------
  { attr: 'vcs-connection-id', id: 'ws-vcs-conn', group: 'vcs' },
  { attr: 'vcs-repo-url', id: 'ws-vcs-repo', group: 'vcs' },
  { attr: 'vcs-branch', id: 'ws-vcs-branch', group: 'vcs' },
  { attr: 'vcs-workflow', id: 'ws-vcs-workflow', group: 'vcs', dependsOn: 'vcs-connection-id' },
  { attr: 'auto-merge', id: 'ws-auto-merge', group: 'vcs', dependsOn: 'vcs-connection-id' },
  {
    attr: 'auto-merge-strategy',
    id: 'ws-auto-merge-strategy',
    group: 'vcs',
    dependsOn: 'auto-merge',
  },
  {
    attr: 'allow-fork-pr-plans',
    id: 'ws-allow-fork-pr-plans',
    group: 'vcs',
    // NO `dependsOn`, and that is the rule rather than an omission: this is a
    // FEATURE'S OWN TOGGLE, and hiding one of those removes the only way to
    // discover the feature exists -- where hiding a DEPENDENT only removes
    // something meaningless. I had declared it a dependent of
    // `vcs-connection-id`; `workspaces.spec.ts:663` creates a workspace with
    // no VCS connection at all and asserts the toggle is visible, which is
    // both the existing behaviour and the correct one.
  },
  {
    attr: 'trigger-prefixes',
    id: 'ws-trigger-prefixes',
    group: 'vcs',
    dependsOn: 'vcs-connection-id',
  },

  // --- governance -------------------------------------------------------
  { attr: 'drift-detection-enabled', id: 'ws-drift-enabled', group: 'governance' },
  {
    attr: 'drift-detection-interval-seconds',
    id: 'ws-drift-interval',
    group: 'governance',
    dependsOn: 'drift-detection-enabled',
  },
  {
    attr: 'drift-ignore-rules',
    id: 'ws-drift-ignore-rules',
    group: 'governance',
    // The third Terraform-only field the table had lost, and the one with the
    // sharpest evidence: `pulumi-workspace.spec.ts` asserts it absent at :326
    // and visible at :339, i.e. it uses this field's presence to tell the two
    // engines apart. Drift classification reads a Terraform plan.
    engines: ['terraform'],
  },
  {
    attr: 'plan-expiry-seconds',
    id: 'ws-plan-expiry',
    group: 'governance',
  },

  // --- ai ---------------------------------------------------------------
  { attr: 'ai-summary-mode', id: 'ws-ai-summary-mode', group: 'ai' },
  { attr: 'ai-summary-context', id: 'ws-ai-summary-context', group: 'ai' },
  { attr: 'ai-policy-mode', id: 'ws-ai-policy-mode', group: 'ai' },

  // --- security scanning ------------------------------------------------
  { attr: 'security-scan-engine', id: 'ws-scan-engine', group: 'security' },
  // Refused outright on an engine the scanners cannot read: `validate_scan_enforcement`
  // raises unless the value is `off`, keyed on `engines.evaluates_security_scans`.
  // So a create form that sends it on a Pulumi workspace 422s the whole request and
  // creates nothing, which is how it shipped. Derived from that predicate over
  // `known_engines()` by `test_scan_enforcement_engines_match_the_api`, so a new
  // engine or a changed predicate fails there rather than 422ing an operator.
  { attr: 'security-scan-enforcement', id: 'ws-scan-enforcement', group: 'security',
    engines: ['terraform'] },
  {
    attr: 'security-scan-severity-threshold',
    id: 'ws-scan-threshold',
    group: 'security',
  },
  {
    attr: 'security-scan-skip-rules',
    id: 'ws-scan-skip-rules',
    group: 'security',
  },

  // --- metadata ---------------------------------------------------------
  {
    attr: 'owner-email',
    id: 'ws-owner',
    group: 'metadata',
    // Platform-admin only: ownership grants workspace admin, so the control
    // is rendered disabled for anyone else rather than hidden -- the field
    // is meaningful, the caller just may not change it.
  },
  { attr: 'labels', id: 'ws-labels', group: 'metadata' },
  {
    attr: 'var-files',
    id: 'ws-var-files',
    group: 'metadata',
    // Terraform only: `.tfvars` files. Same provenance as `execution-backend`
    // above -- gated `!isPulumi` on the edit tab and asserted absent by the
    // Pulumi spec.
    engines: ['terraform'],
  },

  // --- cloud identity ---------------------------------------------------
  {
    attr: 'oidc-audiences',
    id: 'ws-oidc-audiences',
    group: 'cloudIdentity',
  },

  // --- diagnostics ------------------------------------------------------
  { attr: 'debug-mode', id: 'ws-debug-mode', group: 'diagnostics' },
  { attr: 'slack-channel', id: 'ws-slack-channel', group: 'diagnostics' },
]

/** `FIELDS` indexed by attribute, for the gate and for dependency lookups. */
export const FIELD_BY_ATTR: Record<string, FieldSpec> = Object.fromEntries(
  FIELDS.map((f) => [f.attr, f]),
)

/** The fields of one group, in render order, filtered to this engine. */
export function groupFields(group: GroupId, engine: string): FieldSpec[] {
  return FIELDS.filter(
    (f) => f.group === group && (!f.engines || f.engines.includes(engine)),
  )
}

/**
 * Whether a field's gating boolean is satisfied.
 *
 * A field with no `dependsOn` is always meaningful. This is what replaces the
 * old disable-and-explain idiom for DEPENDENTS (#2253): with drift off, the
 * interval input is now absent rather than rendered showing `86400` it is not
 * using. The feature's own TOGGLE is never hidden by this -- hiding a
 * dependent removes something meaningless, while hiding a toggle removes the
 * only way to discover the feature exists.
 */
/**
 * Whether a field can EVER apply to this workspace, which is engine
 * applicability and nothing else (#2413 F20).
 *
 * Split from the gate deliberately, because the two look alike and must not be
 * treated alike. A field whose GATE is off could apply once the gate is set, so
 * it renders DISABLED -- seeing the option is how an operator learns it exists,
 * and nothing on these forms is hidden by a gate. A field belonging to another
 * ENGINE can never apply here, so a permanently disabled control would be noise
 * rather than understanding, and it stays hidden.
 *
 * Collapsing the two is what would put a set of dead Terraform controls on
 * every Pulumi workspace for ever; `e2e/tests/pulumi-workspace.spec.ts` asserts
 * three of them absent and is what catches it.
 */
export function fieldApplies(attr: string, engine: string): boolean {
  const spec = FIELD_BY_ATTR[attr]
  if (!spec) return false
  return !spec.engines || spec.engines.includes(engine)
}

/**
 * Whether a field's gate is on, i.e. whether it is meaningful right now.
 *
 * Feeds `disabled`, never a decision to render. Returns false for an unknown
 * attribute, which is the fail-closed direction: a control nothing describes
 * must not be editable.
 */
export function fieldGateMet(attr: string, values: Record<string, unknown>): boolean {
  const spec = FIELD_BY_ATTR[attr]
  return !!spec && dependencyMet(spec, values)
}

export function dependencyMet(spec: FieldSpec, values: Record<string, unknown>): boolean {
  if (!spec.dependsOn) return true
  const gate = values[spec.dependsOn]
  // An absent gate reads as off, which is the fail-closed direction: a
  // dependent is hidden rather than shown against a gate nobody set.
  if (typeof gate === 'boolean') return gate
  if (typeof gate === 'string') return gate !== '' && gate !== 'false'
  return Boolean(gate)
}


/**
 * The attribute payload for a create or a save, built from the values.
 *
 * ONE producer, because the two rules below are each a 422 waiting to happen
 * and neither is visible from a field's declaration:
 *
 *   - **Never send an alias beside its canonical key.** The API derives
 *     `auto-apply` from `auto-apply-mode` and refuses a request carrying both.
 *     `alsoAccepts` records the superseded spellings precisely so nothing
 *     renders a second control for one, and this function never emits one.
 *   - **`vcs-connection` is a RELATIONSHIP, not an attribute.** It is returned
 *     here under its own key so the caller can place it, rather than being
 *     silently dropped into `attributes` where the API would ignore it.
 *
 * `engine` is emitted on create and withheld on a save, because the native
 * create route reads it off the body while no PATCH path accepts it. Getting
 * that backwards is not cosmetic: omitting it at create makes every workspace
 * Terraform and leaves Pulumi uncreatable from the UI.
 */
export function toAttributes(
  values: Record<string, unknown>,
  opts: { include?: (attr: string) => boolean; create?: boolean } = {},
): { attributes: Record<string, unknown>; vcsConnectionId: string } {
  const attributes: Record<string, unknown> = {}
  let vcsConnectionId = ''
  for (const spec of FIELDS) {
    if (spec.notInForms) continue
    // A create-only attribute is emitted on create and never on a save: the
    // PATCH path does not accept `engine`, so sending it would be noise at
    // best and a 422 if that ever tightens.
    if (spec.createOnly && !opts.create) continue
    if (opts.include && !opts.include(spec.attr)) continue
    const v = values[spec.attr]
    if (v === undefined) continue
    if (spec.attr === 'vcs-connection-id') {
      vcsConnectionId = typeof v === 'string' ? v : ''
      continue
    }
    attributes[spec.attr] = v
  }
  return { attributes, vcsConnectionId }
}

/**
 * Where each attribute's label lives, as a key under `workspaceDetail`.
 *
 * ONE map, because the alternative is each form guessing. It is not a
 * mechanical kebab-to-camel conversion and that is the whole reason it exists:
 * measured against `en.json`, only 29 of the 55 attributes convert cleanly.
 * `vcs-repo-url` is `fields.vcsRepository`, `engine-version` is
 * `fields.version`, `resource-cpu` is `fields.cpuRequest`, and the settings
 * the specialised cards own live in those cards' own sections --
 * `aiSummary.mode`, `securityScan.threshold`, `drift.checkInterval`.
 *
 * Keys under `workspaceDetail` rather than a new shared root: those 111 labels
 * are already translated in all 33 catalogues, so pointing at them means the
 * create form inherits the translations instead of needing ~50 new strings
 * translated 33 times. `workspace-field-groups.tsx` records why the namespace
 * keeps the detail page's name.
 *
 * `test_workspace_field_labels` asserts every entry RESOLVES, so a typo here
 * fails a test rather than rendering a raw key at an operator.
 */
export const LABEL_KEY: Record<string, string> = {
  // identity
  name: 'fields.name',
  engine: 'fields.engine',
  // execution
  'execution-mode': 'fields.executionMode',
  'execution-backend': 'fields.executionBackend',
  'engine-version': 'fields.version',
  'working-directory': 'fields.workingDirectory',
  'auto-apply-mode': 'fields.autoApply',
  // NOT `fields.terragruntEnabledAgent` ('Enabled (agent mode)') -- that is a
  // STATE word the read view shows, not a label. And emphatically not
  // `fields.terragruntEnabledVersion`, which is `'Enabled (v{version})'`: an
  // ICU message requiring an argument, so using it as a label THROWS
  // MISSING_FORMAT_VALUE and renders a raw key. Both labels were lifted from
  // `adminAutodiscovery.form`, where the English was already identical.
  'terragrunt-enabled': 'fields.terragruntEnabled',
  'terragrunt-version': 'fields.terragruntVersion',
  'pulumi-bind-plan': 'fields.bindPlan',
  parallelism: 'fields.parallelism',
  'resource-cpu': 'fields.cpuRequest',
  'resource-memory': 'fields.memoryRequest',
  // operations
  'plan-apply-enabled': 'fields.planApplyEnabled',
  'configure-enabled': 'fields.configureEnabled',
  'auto-configure-enabled': 'fields.autoConfigureEnabled',
  'configure-after-apply': 'fields.configureAfterApply',
  'configure-all-hosts': 'fields.configureAllHosts',
  // ansible
  'config-directory': 'fields.configDirectory',
  'default-playbook': 'fields.defaultPlaybook',
  'default-limit': 'fields.defaultLimit',
  'inventory-paths': 'fields.inventoryPaths',
  // The COMPOSED label (#2413 F19): the three surfaces that set a concrete
  // value render one ordered select over this attribute and
  // `inventory-scripts-enabled`, so "Dynamic inventory plugins" would name half
  // of what the control writes. Bulk update keeps the two separate and uses its
  // own labels, so it is unaffected.
  'dynamic-inventory-enabled': 'fields.dynamicInventoryMode',
  'inventory-scripts-enabled': 'fields.inventoryScriptsEnabled',
  'ansible-host-key-checking': 'fields.ansibleHostKeyChecking',
  'ansible-version': 'fields.ansibleVersion',
  'ansible-execution-environment': 'fields.ansibleExecutionEnvironment',
  'ansible-collections': 'fields.ansibleCollections',
  'ee-resource-cpu': 'fields.eeCpuRequest',
  'ee-resource-memory': 'fields.eeMemoryRequest',
  // pools
  'agent-pool-ids': 'fields.agentPools',
  // vcs
  'vcs-connection-id': 'fields.vcsConnection',
  'vcs-repo-url': 'fields.vcsRepository',
  'vcs-branch': 'fields.vcsBranch',
  'vcs-workflow': 'fields.vcsWorkflow',
  'auto-merge': 'fields.autoMerge',
  'auto-merge-strategy': 'fields.autoMergeStrategy',
  'allow-fork-pr-plans': 'allowForkPrPlans.label',
  'trigger-prefixes': 'fields.triggerPrefixes',
  // governance
  'drift-detection-enabled': 'drift.enabledLabel',
  'drift-detection-interval-seconds': 'drift.checkInterval',
  'drift-ignore-rules': 'fields.driftIgnoreRules',
  'plan-expiry-seconds': 'planExpiry.expireAfter',
  // ai
  'ai-summary-mode': 'aiSummary.mode',
  'ai-summary-context': 'aiSummary.contextLabel',
  'ai-policy-mode': 'aiSummary.policyMode',
  // security scanning
  'security-scan-engine': 'securityScan.engine',
  'security-scan-enforcement': 'securityScan.enforcement',
  'security-scan-severity-threshold': 'securityScan.threshold',
  'security-scan-skip-rules': 'securityScan.skipRules',
  // metadata
  'owner-email': 'fields.owner',
  labels: 'fields.labels',
  'var-files': 'fields.varFiles',
  // cloud identity
  'oidc-audiences': 'fields.oidcAudiences',
  // diagnostics
  'debug-mode': 'debugMode.label',
  'slack-channel': 'slack.channel',
}
