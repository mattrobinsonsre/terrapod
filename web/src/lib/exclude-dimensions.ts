/**
 * The `exclude` contract, as data and logic (#2548).
 *
 * Separate from the component that renders it so it can be tested without a
 * DOM — and because what is here is the SERVER's contract, which is worth
 * reading on its own: which dimensions an exclusion entry accepts, how many
 * entries are allowed, and what counts as setting no selector.
 */

/** One exclusion entry, in the caller's own key spelling. */
export type ExcludeEntry = Record<string, unknown>

/** Mirrors `MAX_EXCLUDE_ENTRIES` in `workspace_search_service.py`. */
export const MAX_EXCLUDE_ENTRIES = 20

export type ExcludeKind = 'text' | 'select' | 'bool' | 'labels' | 'idlist'

export type ExcludeDimension = {
  key: string
  kind: ExcludeKind
  options?: string[]
  placeholder?: string
}

/**
 * Every dimension the server accepts in an exclude entry, canonically spelled.
 *
 * `engine_version` rather than the legacy `terraform_version`: the alias is
 * folded only for evaluation (`canonical_filter_keys`), never on the path that
 * STORES a rule, so a row written with the old spelling would be stored as
 * written — fine today, and a trap the day the alias is retired. The rule
 * editor's own top-level control keeps `terraform_version`, because changing a
 * stored spelling is what produces provider drift on every plan.
 */
export const EXCLUDE_DIMENSIONS: ExcludeDimension[] = [
  { key: 'labels', kind: 'labels' },
  { key: 'workspace_ids', kind: 'idlist', placeholder: 'ws-...' },
  { key: 'name_prefix', kind: 'text' },
  { key: 'name_glob', kind: 'text', placeholder: '*-prod-*' },
  { key: 'execution_backend', kind: 'select', options: ['terraform', 'tofu'] },
  { key: 'execution_mode', kind: 'select', options: ['local', 'agent'] },
  { key: 'engine_version', kind: 'text' },
  { key: 'agent_pool_id', kind: 'text', placeholder: 'apool-...' },
  { key: 'vcs_connection_id', kind: 'text', placeholder: 'vcs-...' },
  { key: 'autodiscovery_rule_id', kind: 'text', placeholder: 'adr-...' },
  { key: 'owner_email', kind: 'text' },
  { key: 'drift_status', kind: 'select', options: ['drifted', 'no_drift'] },
  { key: 'locked', kind: 'bool' },
  { key: 'has_vcs', kind: 'bool' },
]

/** Bulk update: every dimension the server accepts. */
export const BULK_EXCLUDE_DIMENSIONS = EXCLUDE_DIMENSIONS.map((d) => d.key)

/**
 * Assignment rules: the server's set minus the five it refuses.
 *
 * `drift_status`, `locked` and `autodiscovery_rule_id` are
 * `RULE_DIMENSIONS_REFUSED`, applied recursively into `exclude` to any depth;
 * `workspace_ids` is refused in an entry on its own grounds ("exclude by an
 * attribute so that membership re-evaluates as workspaces change"). Offering
 * one would mean an operator filling a control whose only possible outcome is
 * a 422 on save.
 */
const RULE_REFUSED = new Set(['drift_status', 'locked', 'autodiscovery_rule_id', 'workspace_ids'])

export const RULE_EXCLUDE_DIMENSIONS = EXCLUDE_DIMENSIONS.map((d) => d.key).filter(
  (k) => !RULE_REFUSED.has(k),
)

/** The caller's spelling for a canonical dimension key. */
export function spellDimension(key: string, style: 'snake' | 'kebab'): string {
  return style === 'kebab' ? key.replace(/_/g, '-') : key
}

/**
 * Whether an entry sets no selector at all — the `exclude[i] sets no selector`
 * refusal, recognised here so the server never has to raise it.
 *
 * The blank-value asymmetry is the server's, mirrored rather than invented: a
 * blank `name_prefix`/`name_glob` is not a selector (they are gated on
 * truthiness), but a blank `owner_email` IS — the column is NOT NULL defaulting
 * to `""`, so `{"owner_email": ""}` is the only way to select unowned
 * workspaces, and smoothing that over would make it unexpressible.
 */
export function entryIsEmpty(entry: ExcludeEntry): boolean {
  return !Object.entries(entry).some(([k, v]) => {
    if (v === undefined || v === null) return false
    const canonical = k.replace(/-/g, '_')
    if (canonical === 'name_prefix' || canonical === 'name_glob') return String(v).trim() !== ''
    if (Array.isArray(v)) return v.filter((x) => String(x).trim() !== '').length > 0
    if (typeof v === 'object') return Object.keys(v as object).length > 0
    return true
  })
}
