/**
 * Which long hints are two-tier, and what their short lead is (#2413 F15).
 *
 * One registry so a hint cannot be two-tier on one surface and a wall of text
 * on another — the same reason the field table exists. Several of these render
 * on four surfaces.
 *
 * ## The invariant, and why it is derived rather than listed
 *
 * `hint-tiers.test.ts` reads `messages/en.json` and requires **every** hint
 * string at or over `LONG` characters to appear here. So a new long hint fails
 * the build until it is split, rather than shipping as the thing F15 is about
 * and being found by a reader.
 *
 * The threshold is the finding's own: it counted the strings at 250 and up. It
 * is a floor on when a split is REQUIRED, not a style rule — a shorter hint may
 * be split if the split helps.
 *
 * ## What goes in the lead
 *
 * What the control does, and — for anything that relaxes a protection — the
 * consequence of relaxing it. Mechanism, precedence and alternatives go behind
 * the icon. So an operator never has to open anything to learn that a setting
 * lets third-party code run on a runner; they open it to learn how.
 */

/** The length at or above which a hint must carry a lead. */
export const LONG = 250

/**
 * Long hint key → its short lead key, both relative to the same namespace.
 *
 * Keyed by the hint rather than by the field, because a hint is what has a
 * length and some fields carry more than one.
 */
export const HINT_LEADS: Record<string, string> = {
  // workspaceDetail
  'fields.oidcAudiencesHint': 'fields.oidcAudiencesLead',
  'allowForkPrPlans.hint': 'allowForkPrPlans.lead',
  'fields.driftIgnoreRulesHint': 'fields.driftIgnoreRulesLead',
  'fields.dynamicInventoryEnabledHint': 'fields.dynamicInventoryEnabledLead',
  'fields.inventoryScriptsEnabledHint': 'fields.inventoryScriptsEnabledLead',
  'fields.ansibleHostKeyCheckingHint': 'fields.ansibleHostKeyCheckingLead',
  'fields.bindPlanHelp': 'fields.bindPlanLead',
  // A hover `title` until now, which `AGENTS.md` forbids for explanation: it is
  // unreachable on touch and invisible to a keyboard. 257 characters of it.
  'fields.configDirectoryTitle': 'fields.configDirectoryLead',
  'fields.ansibleCollectionsHint': 'fields.ansibleCollectionsLead',
  'variables.vaultFileHint': 'variables.vaultFileLead',
  'variables.vaultFileTemplateHint': 'variables.vaultFileTemplateLead',
  'variables.gitSshHint': 'variables.gitSshLead',
  // ansibleCredentials
  knownHostsHint: 'knownHostsLead',
  machineHint: 'machineLead',
}

/**
 * Hint strings at or over `LONG` that are deliberately NOT two-tier.
 *
 * Each entry says why, and the gate requires a reason — an empty string fails.
 * The four here are not field hints at all: two are `confirm()` bodies, where an
 * ⓘ cannot be opened because the dialog is modal; one is an empty-state
 * description; and one is a standing explainer block rather than a line under a
 * control.
 */
export const NOT_TIERED: Record<string, string> = {
  'adminDeletedWorkspaces.restoreConfirm':
    'a confirm() body; a modal dialog cannot host a disclosure',
  'adminDeletedWorkspaces.restoreAgainConfirm':
    'a confirm() body; a modal dialog cannot host a disclosure',
  'catalogDetail.orphan.description':
    'an empty-state description, not a hint under a control',
  'adminModuleAutodiscovery.howItWorks':
    'a standing explainer block, which is already the detail tier',
}
