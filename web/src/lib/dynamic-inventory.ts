/**
 * Dynamic inventory: one ordered control over two booleans (#2413 F19).
 *
 * The two flags sat adjacent as `dynamic-inventory-enabled` and
 * `inventory-scripts-enabled`, and the combination is what nobody could reason
 * about — two checkboxes whose four states an operator has to work out, one of
 * which permits an arbitrary executable from the repository to run inside the
 * runner Job.
 *
 * NOT *the only* settings that let third-party code run there, which is how the
 * finding framed it and what an earlier version of this comment said.
 * `docs/security-hardening.md` is explicit that a runner Job already executes
 * the operator's own playbook — strictly more powerful than the inventory
 * plugin it is being trusted with — so turning either switch on does not widen
 * what somebody who can write to the repository could already do. What they
 * widen is the SHAPE of it: an arbitrary executable rather than a declared
 * configuration that can be read before it runs.
 *
 * ## Four states, not three
 *
 * The finding proposed a tri-state — *nothing, plugins, plugins+scripts* —
 * which assumes the states are ordered and that scripts imply the plugin
 * dispatcher. They do not. Measured against the runner's own
 * `phases/configure._enabled_plugins`, which appends the two INDEPENDENTLY:
 *
 * | plugins | scripts | `ANSIBLE_INVENTORY_ENABLED` |
 * |---|---|---|
 * | false | false | `yaml,ini` |
 * | false | **true** | **`script,yaml,ini`** |
 * | true | false | `auto,yaml,ini` |
 * | true | true | `script,auto,yaml,ini` |
 *
 * So `scripts` without `plugins` is a real configuration — executable inventory
 * scripts permitted, the `auto` dispatcher not — and nothing refuses it
 * anywhere. The model's own docstring says it outright: *"the two are not one
 * decision ... so an operator can take declared plugins without also
 * permitting arbitrary executables."* A declared plugin config is inspectable;
 * a script is not.
 *
 * A tri-state would therefore delete a capability, so this is a FOUR-option
 * field. It keeps what F19 is about — one ordered control rather than two
 * booleans — and removes nothing.
 *
 * ## Where it is composed, and where it deliberately is not
 *
 * On the surfaces that set a CONCRETE value — workspace create, the workspace
 * Configuration tab, and the autodiscovery rule template — the composition is a
 * strict improvement.
 *
 * **Bulk update keeps its two separate tri-states**, and that is the same
 * reasoning that settled F4 there: its third state is "leave unchanged", and it
 * is PER FIELD. Composing would make "permit declared plugins across the fleet
 * and leave scripts as each workspace has them" unexpressible — nine
 * combinations collapsed to five. The structure is shared; what an empty value
 * means is per surface.
 */

/** Ordered by what it permits, so the dangerous end is last. */
export const DYNAMIC_INVENTORY_MODES = ['none', 'plugins', 'scripts', 'both'] as const

export type DynamicInventoryMode = (typeof DYNAMIC_INVENTORY_MODES)[number]

/** The two flags a mode writes. */
export function flagsFromMode(mode: DynamicInventoryMode): {
  plugins: boolean
  scripts: boolean
} {
  return {
    plugins: mode === 'plugins' || mode === 'both',
    scripts: mode === 'scripts' || mode === 'both',
  }
}

/**
 * The mode two flags read as.
 *
 * Total over all four combinations by construction, which is the property that
 * matters: a pair this could not name would render the control blank and lose
 * the operator's value on the next save.
 */
export function modeFromFlags(plugins: boolean, scripts: boolean): DynamicInventoryMode {
  if (plugins && scripts) return 'both'
  if (plugins) return 'plugins'
  if (scripts) return 'scripts'
  return 'none'
}
