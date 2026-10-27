'use client'

/**
 * The shared field metadata behind the workspace CREATE and EDIT forms (#2253).
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
 * to which group, whether it is advanced, and which boolean gates it. The
 * parity gate reads it rather than a hand-kept list, so a new workspace
 * attribute that reaches one form and not the other fails a test instead of
 * being noticed years later.
 *
 * WHAT IS DELIBERATELY NOT SHARED. The autodiscovery rule form and the bulk
 * update form set the same attributes and are NOT built from this file. Their
 * idioms differ on purpose -- bulk update uses tri-state selects because
 * "unchanged" is a real value there, and autodiscovery leaves the Ansible
 * resource pair empty to mean "take the deployment default" -- so folding them
 * in would force one of those to lie. Create and edit both set a concrete
 * value for a single workspace, which is what makes them the pair that can
 * genuinely share.
 */

import type { ReactNode } from 'react'

// Defined here rather than imported, which is this tree's convention for a
// field-group component: `ansible-credential-fields.tsx` and
// `vault-file-delivery-fields.tsx` each carry their own. Two sets, because the
// two forms are visually different by design -- the edit tab is a dense
// definition list and the create form is a roomier standalone form -- and
// collapsing them would change the look of both surfaces in a change that is
// supposed to be about structure.
export const EDIT_INPUT =
  'mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500'
export const CREATE_INPUT =
  'w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent'

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

/**
 * How a collapsed group says it holds something, without a defaults table.
 *
 * DERIVED FROM THE VALUE, never from a copy of the model's defaults (#2253's
 * one settled design property). An "auto-expand when non-default" rule needs
 * to know every default, and one copy of that already exists in
 * `admin/autodiscovery/page.tsx` -- a second would be exactly the kind of
 * ledger this repo keeps learning drifts. A count for a list and set/unset for
 * an optional scalar need nothing but the value itself.
 *
 * The cost, stated so nobody reads it as an oversight: a scalar with a real
 * default -- `parallelism`, `resource-cpu`, `resource-memory` -- cannot be
 * summarised this way, because "10" tells a reader nothing about whether
 * somebody chose it. Those carry `advanced: false` below and stay expanded.
 */
export type Summary = 'count' | 'setOrUnset' | 'none'

export interface FieldSpec {
  /** The API attribute name. The single source of truth for this field. */
  attr: string
  /** The DOM id both forms use, so one e2e locator serves both. */
  id: string
  group: GroupId
  /** Sits inside a collapsed group. Never true for a value with a real default. */
  advanced?: boolean
  /** The boolean attribute that makes this field meaningful. */
  dependsOn?: string
  /** How a collapsed group summarises it. */
  summary?: Summary
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
  { attr: 'execution-backend', id: 'ws-backend', group: 'execution' },
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
  // The three with real defaults: expanded, never collapsed, per `Summary`.
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
    attr: 'inventory-include-platform',
    id: 'ws-inventory-include-platform',
    group: 'ansible',
    dependsOn: 'configure-enabled',
  },
  {
    attr: 'inventory-paths',
    id: 'ws-inventory-paths',
    group: 'ansible',
    dependsOn: 'configure-enabled',
    summary: 'count',
  },
  {
    attr: 'inventory-ignore-paths',
    id: 'ws-inventory-ignore-paths',
    group: 'ansible',
    dependsOn: 'configure-enabled',
    summary: 'count',
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
    summary: 'count',
  },
  { attr: 'ee-resource-cpu', id: 'ws-ee-cpu', group: 'ansible', dependsOn: 'configure-enabled' },
  { attr: 'ee-resource-memory', id: 'ws-ee-mem', group: 'ansible', dependsOn: 'configure-enabled' },

  // --- agent pools ------------------------------------------------------
  {
    attr: 'agent-pool-ids',
    id: 'ws-pool',
    group: 'pools',
    summary: 'count',
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
    dependsOn: 'vcs-connection-id',
  },
  {
    attr: 'trigger-prefixes',
    id: 'ws-trigger-prefixes',
    group: 'vcs',
    advanced: true,
    summary: 'count',
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
    advanced: true,
    summary: 'count',
  },
  {
    attr: 'plan-expiry-seconds',
    id: 'ws-plan-expiry',
    group: 'governance',
    advanced: true,
    summary: 'setOrUnset',
  },

  // --- ai ---------------------------------------------------------------
  { attr: 'ai-summary-mode', id: 'ws-ai-summary-mode', group: 'ai' },
  { attr: 'ai-summary-context', id: 'ws-ai-summary-context', group: 'ai', summary: 'setOrUnset' },
  { attr: 'ai-policy-mode', id: 'ws-ai-policy-mode', group: 'ai' },

  // --- security scanning ------------------------------------------------
  { attr: 'security-scan-engine', id: 'ws-scan-engine', group: 'security' },
  { attr: 'security-scan-enforcement', id: 'ws-scan-enforcement', group: 'security' },
  {
    attr: 'security-scan-severity-threshold',
    id: 'ws-scan-threshold',
    group: 'security',
  },
  {
    attr: 'security-scan-skip-rules',
    id: 'ws-scan-skip-rules',
    group: 'security',
    advanced: true,
    summary: 'count',
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
  { attr: 'labels', id: 'ws-labels', group: 'metadata', advanced: true, summary: 'count' },
  { attr: 'var-files', id: 'ws-var-files', group: 'metadata', advanced: true, summary: 'count' },

  // --- cloud identity ---------------------------------------------------
  {
    attr: 'oidc-audiences',
    id: 'ws-oidc-audiences',
    group: 'cloudIdentity',
    advanced: true,
    summary: 'count',
  },

  // --- diagnostics ------------------------------------------------------
  { attr: 'debug-mode', id: 'ws-debug-mode', group: 'diagnostics', advanced: true },
  { attr: 'slack-channel', id: 'ws-slack-channel', group: 'diagnostics', advanced: true, summary: 'setOrUnset' },
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
 * The one-line summary of a collapsed group, derived from the values alone.
 *
 * Returns the per-field fragments a caller joins; an empty array means the
 * group holds nothing worth announcing, so its summary shows only its name.
 * Never consults a defaults table -- see `Summary`.
 */
export function groupSummary(
  group: GroupId,
  engine: string,
  values: Record<string, unknown>,
  label: (attr: string) => string,
): { attr: string; text: string }[] {
  const out: { attr: string; text: string }[] = []
  for (const spec of groupFields(group, engine)) {
    if (!spec.summary || spec.summary === 'none') continue
    if (!dependencyMet(spec, values)) continue
    const v = values[spec.attr]
    if (spec.summary === 'count') {
      const n = Array.isArray(v) ? v.length : 0
      if (n > 0) out.push({ attr: spec.attr, text: `${label(spec.attr)} ${n}` })
    } else if (spec.summary === 'setOrUnset') {
      const set = v !== undefined && v !== null && v !== '' && v !== 0
      if (set) out.push({ attr: spec.attr, text: label(spec.attr) })
    }
  }
  return out
}

/**
 * One field: its label, and either the stored value or the control.
 *
 * The two forms are structurally different -- the edit tab is a `<dl>` whose
 * `<dd>` becomes an `<input>` while editing, and the create form is a
 * `<label>` plus a control that is always editable. That is why this takes a
 * `mode` rather than the markup being lifted as-is: `view` renders the `<dt>`
 * and `<dd>` pair, and `edit`/`create` render a label and the caller's
 * control. Collapsing the two into one shape would have meant giving up the
 * edit tab's read view, which is the thing most visits to that page want.
 *
 * `min-w-0` is on the wrapper deliberately. A grid item has an intrinsic
 * minimum width and refuses to shrink below its content without it, which is
 * the single CSS line behind a phone-width overflow this repo has met before.
 */
export function WsField({
  id,
  label,
  mode,
  display,
  hint,
  children,
}: {
  id: string
  label: ReactNode
  mode: FieldMode
  /** What `view` mode shows. Ignored in the other two. */
  display?: ReactNode
  hint?: ReactNode
  /** The control, rendered in `edit` and `create`. */
  children?: ReactNode
}) {
  if (mode === 'view') {
    return (
      <div className="min-w-0">
        <dt className="text-xs text-slate-500">{label}</dt>
        <dd className="mt-1 text-sm text-slate-200 break-words" data-testid={`${id}-value`}>
          {display}
        </dd>
        {hint}
      </div>
    )
  }
  const labelClass =
    mode === 'create'
      ? 'block text-sm font-medium text-slate-300 mb-1'
      : 'text-xs text-slate-500'
  return (
    <div className="min-w-0">
      <label htmlFor={id} className={labelClass}>
        {label}
      </label>
      {children}
      {hint}
    </div>
  )
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
  'terragrunt-enabled': 'fields.terragruntEnabledAgent',
  'terragrunt-version': 'fields.terragruntEnabledVersion',
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
  'inventory-include-platform': 'fields.inventoryIncludePlatform',
  'inventory-paths': 'fields.inventoryPaths',
  'inventory-ignore-paths': 'fields.inventoryIgnorePaths',
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
