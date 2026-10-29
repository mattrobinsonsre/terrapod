'use client'

import { useEffect, useState, useCallback, Suspense, type ReactNode } from 'react'
import { useRouter, useParams, useSearchParams } from 'next/navigation'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { ConnectionStatus } from '@/components/connection-status'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { LabelsEditor } from '@/components/labels-editor'
import {
  EDIT_INPUT,
  FIELD_BY_ATTR,
  LABEL_KEY,
  dependencyMet,
} from '@/components/workspace-fields'
import { Field, Group, type GroupCtx } from '@/components/workspace-field-groups'
import { categoryKey, nativeCategoryKey } from '@/lib/variable-category'
import { HealthConditions } from '@/components/health-conditions'
import { PlanSummaryBadges } from '@/components/plan-summary-badges'
import { WorkspacePicker } from '@/components/workspace-picker'
import { SensitiveValueInput } from '@/components/sensitive-value-input'
import { PhaseSlotFlags, PhaseSlotNote } from '@/components/phase-slot-flags'
import { MobileCardList, MobileCard } from '@/components/mobile-card-list'
import { StateGraphTab } from '@/components/state-graph-tab'
import { CostPanel } from '@/components/cost-panel'
import { ResourceAccessPanel } from '@/components/resource-access-panel'
import { ArchitectureCritiquePanel } from '@/components/architecture-critique-panel'
import { StackOutputsPanel } from '@/components/stack-outputs-panel'
import { InventoryPanel, useInventoryPresence } from '@/components/inventory-panel'
import { useIsTouch } from '@/lib/use-media-query'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch, fetchAllPages, parseApiError } from '@/lib/api'
import { versionToolFor } from '@/lib/engine-version'
import { PHASE_STATUSES, engineWord, phaseKey } from '@/lib/phase-vocabulary'
import { VaultValueDisplay } from '@/components/vault-value-display'
import {
  VaultReferenceFields,
  buildVaultReference,
  emptyVaultReference,
  parseVaultReference,
  type VaultReferenceValue,
} from '@/components/vault-reference-fields'
import {
  OidcAudiencesEditor,
  StringListEditor,
  sanitizeOidcAudiences,
  partitionOidcAudiences,
  type OidcAudiences,
} from '@/components/template-editors'
import { useOidcAudienceDefaults } from '@/lib/use-oidc-audience-defaults'
import {
  VariableEditPanel,
} from '@/components/variable-edit-panel'
import {
  ANSIBLE_MACHINE_CATEGORY,
  ANSIBLE_VAULT_CATEGORY,
  AnsibleCredentialFields,
  isAnsibleAuthCategory,
} from '@/components/ansible-credential-fields'
import { ApplicableVarsets } from '@/components/applicable-varsets'
import { useSortable } from '@/lib/use-sortable'
import { useRunEvents } from '@/lib/use-run-events'

interface WorkspacePermissions {
  'can-update': boolean
  'can-destroy': boolean
  'can-queue-run': boolean
  // Plan-only vs apply are separate grants, so the UI needs both to know
  // whether to offer the apply button at all (#1340).
  'can-queue-apply'?: boolean
  // Destroy runs are a separate grant again (run:apply-destroy, #1634).
  'can-queue-destroy'?: boolean
  'can-read-state-versions': boolean
  'can-create-state-versions': boolean
  'can-read-variable': boolean
  'can-update-variable': boolean
  'can-lock': boolean
  'can-unlock': boolean
  'can-force-unlock': boolean
  'can-read-settings': boolean
}

interface WorkspaceAttrs {
  name: string
  // Which engine runs this workspace (#1407). Absent from an older API: Terraform.
  engine?: string
  // Pulumi only (#1553): bind the update to the approved preview.
  'pulumi-bind-plan'?: boolean
  'execution-mode': string
  'execution-backend': string
  'auto-apply': boolean
  'auto-apply-mode': string
  'terraform-version': string
  'terragrunt-enabled': boolean
  'terragrunt-version': string
  'working-directory': string
  // Which operations this workspace HAS (#1966) and, below, where the
  // configure one runs (#1929). Native-only: both are deliberately absent from
  // `/api/tfe/v2`, which is why this page reads and writes the workspace
  // through `/api/v1`.
  //
  // Optional only because an older API omits the keys; the fallbacks below
  // mirror the column defaults, which ARE the compatibility story -- plan/apply
  // on, configure off.
  'plan-apply-enabled'?: boolean
  'configure-enabled'?: boolean
  // How a configure starts (#1974). Both opt-in, both default false, and
  // neither does anything without `configure-enabled`.
  'auto-configure-enabled'?: boolean
  'configure-after-apply'?: boolean
  'configure-all-hosts'?: boolean
  // An ordinary repo-relative path, `''` meaning the configuration version's
  // root -- `working-directory`'s shape exactly. It has NO null state: whether
  // there is a configure operation is `configure-enabled` above (#1966), not
  // the absence of a path. Optional only for an older API.
  'config-directory'?: string
  'inventory-paths'?: string[]
  // Which inventory SOURCES ansible may resolve on the runner (#2033). The
  // API never runs either kind, in either state. Absent from an older API,
  // where the answer is the column default -- false.
  'dynamic-inventory-enabled'?: boolean
  'inventory-scripts-enabled'?: boolean
  'default-playbook'?: string
  'default-limit'?: string
  locked: boolean
  // Why the workspace is locked and who locked it (#1705). Null when unlocked,
  // and absent from an older API.
  'lock-reason'?: string | null
  'locked-by'?: string | null
  'resource-cpu': string
  parallelism: number
  'resource-memory': string
  'debug-mode': boolean
  'allow-fork-pr-plans': boolean
  'agent-pool-id': string | null
  // The full pool set (#1085). Flat — a run is offered to every pool at once
  // and whichever has a live runner claims it first.
  'agent-pool-ids': string[]
  'agent-pool-name': string | null
  /** Names for the whole set, positionally matching `agent-pool-ids`. */
  'agent-pool-names'?: string[]
  labels: Record<string, string>
  'owner-email': string
  'var-files': string[]
  'trigger-prefixes': string[]
  'drift-ignore-rules': string[]
  // The audiences a run identity token is minted for (#1901), keyed on the
  // provider configuration the token is for — `aws`, or `aws.west` for one
  // aliased configuration.
  //
  // **This is the MERGED map, not the workspace's own override**: the server
  // returns the deployment catalogue with the workspace's map merged over it
  // per key, carrying no marker saying which is which. So it must never be
  // written back wholesale — that would promote every inherited entry into an
  // override. `partitionOidcAudiences` subtracts the catalogue (fetched
  // separately) to recover what the workspace actually owns, and only that is
  // sent. Empty here means nothing is in force at all, and the runs then
  // authenticate with the agent pool's own identity exactly as before.
  'oidc-audiences': OidcAudiences
  'vcs-repo-url': string
  'vcs-branch': string
  'vcs-connection-id': string | null
  'vcs-connection-name': string | null
  'vcs-workflow': 'merge_then_apply' | 'apply_then_merge'
  'auto-merge': boolean
  'auto-merge-strategy': 'merge' | 'squash' | 'rebase'
  'ai-summary-mode': 'default' | 'enabled' | 'disabled'
  'ai-summary-context': string
  'ai-policy-mode': 'default' | 'enabled' | 'disabled'
  'security-scan-enforcement': 'off' | 'advisory' | 'enforced'
  'security-scan-engine': 'checkov' | 'trivy' | 'both'
  'security-scan-severity-threshold': 'critical' | 'high' | 'medium' | 'low'
  'security-scan-skip-rules': string[]
  'slack-channel': string
  'drift-detection-enabled': boolean
  'drift-detection-interval-seconds': number
  'plan-expiry-seconds': number | null
  'drift-last-checked-at': string
  'drift-status': string
  'state-diverged': boolean
  'health-conditions': { code: string; severity: 'error' | 'warning'; title: string; detail: string }[]
  'lifecycle-state': 'active' | 'pending_deletion' | 'archived'
  'lifecycle-reason': string
  'vcs-last-polled-at': string | null
  'vcs-last-attempted-at': string | null
  'vcs-last-error': string | null
  'vcs-last-error-at': string | null
  'created-at': string
  'updated-at': string
  permissions: WorkspacePermissions
}

interface AgentPool {
  id: string
  attributes: { name: string; permission?: string }
}

interface Workspace {
  id: string
  attributes: WorkspaceAttrs
}

interface Variable {
  id: string
  attributes: {
    key: string
    value: string
    category: string
    structured: boolean
    sensitive: boolean
    'apply-only'?: boolean
    /** The third phase slot (#2190): a configure sees it, a plan and an apply do not. */
    'configure-only'?: boolean
    description: string
    /** "static" or "vault" (#1439). */
    'value-source'?: string
  }
}

interface RunItem {
  id: string
  attributes: {
    status: string
    source: string
    message: string
    'plan-only': boolean
    // `tofu plan -out=FILE` (#1903): apply-capable, apply deferred. Shown
    // because it is the only reason two runs on one workspace can sit at
    // `planned` at once -- an ordinary planned run holds the workspace.
    'save-plan': boolean
    'is-destroy': boolean
    // The Ansible configure half (#1974). `is-configure` means the run carries
    // one; `configure-after-apply` tells a COMPOSITE (an apply then a configure
    // on one run) from a STANDALONE configure, which matters because the first
    // also plans and applies.
    'is-configure': boolean
    'configure-after-apply': boolean
    'created-at': string
    'created-by': string
    'plan-started-at': string | null
    'apply-finished-at': string | null
    // The post-plan gate holding the run, if any (#1725).
    'blocked-by'?: string | null
    'plan-summary': {
      add: number
      change: number
      destroy: number
      replace: number
      import: number
    } | null
    actions?: {
      'is-confirmable': boolean
      'is-discardable': boolean
    }
  }
}

interface StateVersionItem {
  id: string
  attributes: {
    serial: number
    lineage: string
    md5: string
    size: number
    'created-at': string
    'created-by': string | null
  }
  relationships?: {
    run?: { data: { id: string; type: string } | null }
  }
}

interface ConfigurationVersionItem {
  id: string
  attributes: {
    source: string
    status: string
    'auto-queue-runs': boolean
    speculative: boolean
    'created-at': string
  }
}

interface CVDiffFile {
  path: string
  type: 'added' | 'removed' | 'modified' | 'binary-changed'
  diff?: string
}

interface CVDiffResult {
  'from-id': string
  'to-id': string
  files: CVDiffFile[]
  oversized: string[]
  'total-files-changed': number
}

interface RunTaskItem {
  id: string
  attributes: {
    name: string
    url: string
    enabled: boolean
    stage: string
    'enforcement-level': string
    'has-hmac-key': boolean
    'created-at': string
    'updated-at': string
  }
}

interface DeliveryResponse {
  status: number
  body: string
  success: boolean
  delivered_at: string
}

interface NotificationConfig {
  id: string
  attributes: {
    name: string
    'destination-type': string
    url: string
    enabled: boolean
    'has-token': boolean
    triggers: string[]
    'email-addresses': string[]
    'delivery-responses': DeliveryResponse[]
    'created-at': string
    'updated-at': string
  }
}

const ALL_TRIGGERS = [
  'run:created', 'run:planning', 'run:needs_attention',
  'run:planned', 'run:applying', 'run:completed', 'run:errored',
  'run:drift_detected',
]

const ALL_STAGES = ['pre_plan', 'post_plan', 'pre_apply'] as const
const ALL_ENFORCEMENT_LEVELS = ['mandatory', 'advisory'] as const

type Tab = 'configuration' | 'variables' | 'runs' | 'state' | 'state-graph' | 'cost' | 'architecture' | 'versions' | 'notifications' | 'run-tasks' | 'run-triggers' | 'inventory' | 'sharing' | 'access'

const VALID_TABS: Set<string> = new Set(['configuration', 'variables', 'runs', 'state', 'state-graph', 'cost', 'architecture', 'versions', 'notifications', 'run-tasks', 'run-triggers', 'inventory', 'sharing', 'access'])

// Views that read TERRAFORM state specifically, so they have nothing to show on
// a Pulumi workspace and 404 if asked (#1568). Their tabs are absent there
// rather than shown and broken, and a `?tab=` naming one falls back to
// Configuration so a stale deep link renders a page instead of a blank pane.
//
// The state GRAPH is deliberately NOT in this set — it reads the engine-neutral
// resource graph and works for both engines.
const TERRAFORM_ONLY_TABS: Set<Tab> = new Set<Tab>(['cost', 'architecture'])


/** Mode value -> i18n key. The API value is snake_case; the key is camel. */
function autoApplyModeKey(mode: string): string {
  return mode === 'create_update' ? 'createUpdate' : mode
}

export default function WorkspaceDetailPage() {
  return (
    <Suspense fallback={<><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>}>
      <WorkspaceDetailContent />
    </Suspense>
  )
}

function WorkspaceDetailContent() {
  const t = useTranslations('workspaceDetail')
  // Shared mode labels live in the top-level `common` namespace because
  // the run page renders the same four values.
  const tMode = useTranslations('common.autoApplyMode')
  // Root namespace, for the per-engine phase vocabulary (#1911). The runs tab
  // names a phase in a dozen places — the buttons, the options heading, the
  // type pills, the status pills — and all of them read from here so this page
  // and the run page it links to can never disagree about one run.
  const tPhase = useTranslations()
  // The platform's own run-status words, shared with the workspace list.
  const tStatus = useTranslations('status')
  // Ansible credential copy (#1933), in its own top-level namespace because
  // the same three surfaces write one — this page, the variable-set page and
  // the shared edit panel — and a second copy under `adminVariableSets` is a
  // second thing to translate into thirty-three locales and keep in step.
  const ta = useTranslations('ansibleCredentials')
  const router = useRouter()
  const params = useParams()
  const searchParams = useSearchParams()
  const workspaceId = params.id as string
  // The Vault reference "Check" action (#1663) posts here.
  const vaultCheckUrl = `/api/terrapod/v1/workspaces/${workspaceId}/vault-reference-checks`

  const tabParam = searchParams.get('tab') || 'configuration'
  const requestedTab: Tab = VALID_TABS.has(tabParam) ? (tabParam as Tab) : 'configuration'

  function setActiveTab(tab: Tab) {
    router.replace(`?tab=${tab}`, { scroll: false })
  }

  const [workspace, setWorkspace] = useState<Workspace | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [lastQueuedRunId, setLastQueuedRunId] = useState<string | null>(null)

  // Settings and views that only mean something to Terraform are hidden for
  // Pulumi rather than shown and ignored (#1555, #1568). `workspace` is null
  // while loading, which reads as "not Pulumi" — harmless, because the page
  // renders a spinner until it resolves.
  const isPulumi = workspace?.attributes.engine === 'pulumi'
  // The workspace's engine is authoritative for every run it holds, so one
  // lookup serves the whole runs tab (#1911).
  const phaseWord = (name: string) =>
    engineWord(tPhase, workspace?.attributes.engine, name)
  // The same lookup as a KEY, for the sites that need next-intl itself to do
  // the work: rich text with tag chunks, and a string with an ICU placeholder.
  // Both must go through `tPhase.rich`/`tPhase(key, values)` rather than
  // post-processing `phaseWord`'s output, or the tags stop rendering and the
  // placeholder stops being formatted in the reader's locale.
  const phaseKeyFor = (name: string) =>
    phaseKey(workspace?.attributes.engine, 'words', name)
  // A run status that names a phase belongs to the engine; the rest (queued,
  // errored, canceled…) are the platform's own.
  const runStatusLabel = (status: string) =>
    PHASE_STATUSES.has(status)
      ? tPhase(phaseKey(workspace?.attributes.engine, 'runStatus', status))
      : tStatus.has(status)
        ? tStatus(status)
        : status
  // Resolved once here so the loaders, the SSE handler, the tab strip and the
  // render sites all agree on which tab is showing.
  // The Inventory tab is DATA-gated, not configuration-gated (#1967, #1968,
  // #1969). A workspace with no hosts, no groups and no Ansible config
  // directory has nothing to show, so the tab is simply absent — there is
  // nothing for a terraform/tofu-only deployment to turn off, which is the
  // mechanism rather than a flag (#1986).
  //
  // The probe lives with the panel because the three questions it asks are the
  // panel's own; on any failure it answers false and the tab stays hidden, so a
  // 403 from a role without `inventory:read` and a transport blip both mean "do
  // not offer a surface this reader cannot use".
  //
  // A stale `?tab=inventory` on a workspace with no inventory falls back the
  // same way a Terraform-only tab does on Pulumi: Configuration renders a page
  // instead of a blank pane. The probe answers false on the first frame too,
  // but the page shows a spinner until the workspace resolves, so the fallback
  // only bites once the probe has had its chance.
  const hasInventory = useInventoryPresence(workspaceId)
  const activeTab: Tab =
    (isPulumi && TERRAFORM_ONLY_TABS.has(requestedTab)) ||
    (requestedTab === 'inventory' && !hasInventory)
      ? 'configuration'
      : requestedTab

  // Overview editing
  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState('')
  const [nameChanged, setNameChanged] = useState(false)
  const [editCpu, setEditCpu] = useState('')
  const [editMemory, setEditMemory] = useState('')
  const [editParallelism, setEditParallelism] = useState('10')
  // #1274: the four-value mode supersedes the old boolean. Rendered as its
  // raw enum value, matching `execution-mode` immediately above it — these
  // are API identifiers, not prose, so they are not translated.
  const [editAutoApplyMode, setEditAutoApplyMode] = useState('never')
  const [editExecMode, setEditExecMode] = useState('')
  const [editBackend, setEditBackend] = useState('')
  const [editVersion, setEditVersion] = useState('')
  const [editTerragruntEnabled, setEditTerragruntEnabled] = useState(false)
  const [editTerragruntVersion, setEditTerragruntVersion] = useState('')
  const [editPoolIds, setEditPoolIds] = useState<string[]>([])
  const [editLabels, setEditLabels] = useState<Record<string, string>>({})
  const [editOwner, setEditOwner] = useState('')
  const [editVarFiles, setEditVarFiles] = useState<string[]>([])
  const [newVarFile, setNewVarFile] = useState('')
  const [editTriggerPrefixes, setEditTriggerPrefixes] = useState<string[]>([])
  const [newTriggerPrefix, setNewTriggerPrefix] = useState('')
  const [editDriftIgnoreRules, setEditDriftIgnoreRules] = useState<string[]>([])
  const [newDriftIgnoreRule, setNewDriftIgnoreRule] = useState('')
  // Only the entries this workspace OWNS; `null` until the deployment
  // catalogue has settled, because the partition is meaningless without it.
  //
  // The sentinel is load-bearing rather than tidiness. Partitioning against a
  // catalogue that has not arrived yet classifies EVERY inherited entry as
  // owned, and the next save would then write them all into the override
  // column — the precise promotion this split exists to prevent. So nothing is
  // seeded until the probe settles (on success or failure), the editor is not
  // offered before then, and an unseeded save omits the attribute entirely,
  // which the API treats as leave-alone.
  const [editOidcAudiences, setEditOidcAudiences] = useState<OidcAudiences | null>(null)
  const [editWorkingDir, setEditWorkingDir] = useState('')
  // Which operations this workspace has (#1966). Two checkboxes because the
  // two are independent: all four combinations are meaningful except
  // both-false, which the server refuses -- so the pair cannot be a radio
  // group or a single select without inventing a state the column does not
  // have. The initial values mirror the column defaults, which is what a
  // workspace that predates these attributes reads back as.
  const [editPlanApplyEnabled, setEditPlanApplyEnabled] = useState(true)
  const [editConfigureEnabled, setEditConfigureEnabled] = useState(false)
  const [editAutoConfigureEnabled, setEditAutoConfigureEnabled] = useState(false)
  const [editConfigureAfterApply, setEditConfigureAfterApply] = useState(false)
  const [editConfigureAllHosts, setEditConfigureAllHosts] = useState(false)
  // Where the configure operation runs (#1929) -- a plain text box now, because
  // the path is just a path: `''` is the configuration version's root, the way
  // it is for `working-directory` beside it, and there is no third state to
  // carry. #1929 shipped this as a mode plus a path because NULL meant "no
  // configure operation"; that declaration moved to `editConfigureEnabled`
  // above (#1966), so the mode had nothing left to decide.
  const [editConfigDir, setEditConfigDir] = useState('')
  // The one combination the server refuses (#1966), caught here so the operator
  // reads it beside the checkboxes rather than as a 422 after a save. Derived,
  // never stored: a second copy of the rule is a second thing to keep in step,
  // and the server's check against the merged row is still the authority.
  const editHasNoOperations = !editPlanApplyEnabled && !editConfigureEnabled
  const [editInventoryPaths, setEditInventoryPaths] = useState<string[]>([])
  const [editDynamicInventoryEnabled, setEditDynamicInventoryEnabled] = useState(false)
  const [editInventoryScriptsEnabled, setEditInventoryScriptsEnabled] = useState(false)
  const [editDefaultPlaybook, setEditDefaultPlaybook] = useState('')
  const [editDefaultLimit, setEditDefaultLimit] = useState('')
  const [editBindPlan, setEditBindPlan] = useState(false)
  const [editVcsConnectionId, setEditVcsConnectionId] = useState<string | null>(null)
  const [editVcsRepoUrl, setEditVcsRepoUrl] = useState('')
  const [editVcsBranch, setEditVcsBranch] = useState('')
  const [editVcsWorkflow, setEditVcsWorkflow] = useState<'merge_then_apply' | 'apply_then_merge'>(
    'merge_then_apply'
  )
  const [editAutoMerge, setEditAutoMerge] = useState(false)
  const [editAutoMergeStrategy, setEditAutoMergeStrategy] = useState<
    'merge' | 'squash' | 'rebase'
  >('merge')
  const [saving, setSaving] = useState(false)

  // Agent pools. Two lists on purpose: `agentPools` is what the caller may
  // ASSIGN (pool:assign), `visiblePools` is everything they can read and is
  // used only to resolve names for display — a workspace can legitimately name
  // a pool the current user has no write on.
  const [agentPools, setAgentPools] = useState<AgentPool[]>([])
  const [poolsLoaded, setPoolsLoaded] = useState(false)
  const [visiblePools, setVisiblePools] = useState<AgentPool[]>([])

  // VCS connections
  const [vcsConnections, setVcsConnections] = useState<{ id: string; attributes: { name: string; provider: string } }[]>([])
  const [vcsConnectionsLoaded, setVcsConnectionsLoaded] = useState(false)

  // Version suggestions. Keyed on the *tool* the version pins, which the engine
  // decides — not on the execution backend, which only means anything within the
  // Terraform engine (#1559).
  const [versionSuggestions, setVersionSuggestions] = useState<string[]>([])
  const [versionsTool, setVersionsTool] = useState('')

  // Variables
  const [variables, setVariables] = useState<Variable[]>([])
  const [varsLoading, setVarsLoading] = useState(false)
  const [showAddVar, setShowAddVar] = useState(false)
  const [varKey, setVarKey] = useState('')
  const [varValue, setVarValue] = useState('')
  const [varCategory, setVarCategory] = useState('native')
  const [varSensitive, setVarSensitive] = useState(false)
  const [varApplyOnly, setVarApplyOnly] = useState(false)
  const [varConfigureOnly, setVarConfigureOnly] = useState(false)
  const [varHcl, setVarHcl] = useState(false)
  // Git module-source auth builder (#1028): when the category is git_http_auth /
  // git_ssh_auth the form swaps to discrete credential fields that serialize into
  // the JSON value on submit (see handleAddVariable). git creds are always sensitive.
  const [gitSource, setGitSource] = useState<'vcs_connection' | 'static'>('vcs_connection')
  const [gitVcsConnId, setGitVcsConnId] = useState('')
  const [gitUsername, setGitUsername] = useState('')
  const [gitToken, setGitToken] = useState('')
  const [gitPrivateKey, setGitPrivateKey] = useState('')
  const [gitKnownHosts, setGitKnownHosts] = useState('')
  const [gitRewrite, setGitRewrite] = useState<'none' | 'to_https' | 'to_ssh'>('none')
  const isGitCat = varCategory === 'git_http_auth' || varCategory === 'git_ssh_auth'
  // Ansible credentials (#1933): the same shape of thing — discrete fields
  // serialising into a JSON value — but the builder is shared with the
  // variable-set form and the edit panel, so it needs no state of its own
  // here. `varValue` carries the whole envelope and the builder owns the
  // parse/serialise, which is why the raw value field is simply swapped out
  // rather than mirrored by seven more useState calls.
  const isAnsibleCat = isAnsibleAuthCategory(varCategory)
  // An envelope category and a plain one cannot share typed-in content: a
  // value typed for `native` is not JSON, and it would reach the builder as an
  // unreadable envelope (so: blank fields) and then be submitted as whatever
  // survived. Clearing on the way in or out of an envelope category is the
  // honest reset.
  function changeVarCategory(next: string) {
    if (isAnsibleAuthCategory(next) !== isAnsibleCat) setVarValue('')
    setVarCategory(next)
    if (next === 'git_http_auth') ensureVcsConnections()
  }
  const varKeyLabel = isGitCat
    ? t('variables.gitUrlPattern')
    : varCategory === ANSIBLE_MACHINE_CATEGORY
      ? ta('name')
      : varCategory === ANSIBLE_VAULT_CATEGORY
        ? ta('vaultLabel')
        : t('variables.key')
  const varKeyHint =
    varCategory === ANSIBLE_MACHINE_CATEGORY
      ? ta('nameHint')
      : varCategory === ANSIBLE_VAULT_CATEGORY
        ? ta('vaultLabelHint')
        : ''
  // Identifiers, not copy: a repository prefix, a credential name an inventory
  // would refer to, and a vault label an operator would recognise.
  const varKeyPlaceholder = isGitCat
    ? 'github.com/my-org'
    : varCategory === ANSIBLE_MACHINE_CATEGORY
      ? 'web-key'
      : varCategory === ANSIBLE_VAULT_CATEGORY
        ? 'prod'
        : 'AWS_REGION'
  /** The category as a person reads it, in this workspace's engine's words. */
  const categoryLabel = (category: string, engine?: string) => {
    const key = categoryKey(category, engine)
    return key ? t(`variables.${key}`) : category
  }

  // Vault value source (#1439): the variable holds a *reference*, resolved
  // server-side at run time. Discrete fields, never raw JSON.
  const [varSource, setVarSource] = useState<'static' | 'vault'>('static')
  const [vaultRef, setVaultRef] = useState<VaultReferenceValue>(emptyVaultReference)
  // An Ansible credential is an envelope too, and the API refuses a Vault
  // value source on it for the same reason it refuses one on a git credential
  // (#1933): a reference resolves to a single field.
  const isVaultSource = varSource === 'vault' && !isGitCat && !isAnsibleCat
  // Offered only where the deployment has Vault configured — presenting a
  // source that is not set up produces a variable that fails its first run.
  const [vaultAvailable, setVaultAvailable] = useState(false)
  const [vaultInstances, setVaultInstances] = useState<string[]>([])
  const [vaultDefaultInstance, setVaultDefaultInstance] = useState('')
  // ...and only on an agent workspace. A vault reference is resolved on the
  // listener claim path, so under local execution it would deliver nothing —
  // the API refuses to store one (`_reject_vault_on_local`). Offering the
  // source anyway means filling in the whole reference builder and then
  // meeting a 422 on save, so it is not offered at all.
  const vaultOfferable =
    vaultAvailable && workspace?.attributes['execution-mode'] === 'agent'

  // The deployment's audience catalogue: what this workspace's `oidc-audiences`
  // is merged OVER, and the only way to tell an inherited entry from an
  // override (#1901).
  const oidcDefaults = useOidcAudienceDefaults()

  const [editVarSource, setEditVarSource] = useState<'static' | 'vault'>('static')
  // The whole stored reference, not just the fields on screen — so an edit
  // carries `method`, `data` and anything else it does not render (#1619).
  const [editVaultRef, setEditVaultRef] = useState<VaultReferenceValue>(emptyVaultReference)
  const [addingVar, setAddingVar] = useState(false)

  const isTouch = useIsTouch()

  // #719 two-tier confirm policy (see AGENTS.md → Responsive → Touch model):
  // an irreversible delete/remove prompts in BOTH modes; any other single-tap
  // mutation prompts on touch only (a mis-tap is easy on a phone).
  const confirmDelete = (msg: string) => window.confirm(msg)
  const confirmTouchMutation = (msg: string) => !isTouch || window.confirm(msg)

  // Runs
  const [runs, setRuns] = useState<RunItem[]>([])
  const [runsLoading, setRunsLoading] = useState(false)
  const [queueingPlan, setQueueingPlan] = useState(false)
  const [showPlanOptions, setShowPlanOptions] = useState(false)
  const [planTargets, setPlanTargets] = useState('')
  const [planReplaces, setPlanReplaces] = useState('')
  const [planRefreshOnly, setPlanRefreshOnly] = useState(false)
  const [planRefresh, setPlanRefresh] = useState(true)
  const [planAllowEmpty, setPlanAllowEmpty] = useState(false)
  const [queueingDestroy, setQueueingDestroy] = useState(false)
  // The configure trigger's own state (#1974). The two option fields are
  // per-press overrides of the workspace's `default-playbook` / `default-limit`
  // -- empty means "use the workspace's", which is also what a git-driven
  // configure runs, because a push carries no run options.
  const [queueingConfigure, setQueueingConfigure] = useState(false)
  const [configurePlaybook, setConfigurePlaybook] = useState('')
  const [configureLimit, setConfigureLimit] = useState('')
  const [showDestroyConfirm, setShowDestroyConfirm] = useState(false)

  // VCS ref picker
  const [vcsRef, setVcsRef] = useState('')
  const [vcsRefType, setVcsRefType] = useState<'branch' | 'tag'>('branch')
  const [vcsBranches, setVcsBranches] = useState<{ name: string; sha: string }[]>([])
  const [vcsTags, setVcsTags] = useState<{ name: string; sha: string }[]>([])
  const [vcsDefaultBranch, setVcsDefaultBranch] = useState('')
  const [vcsRefsLoading, setVcsRefsLoading] = useState(false)
  const [vcsRefsLoaded, setVcsRefsLoaded] = useState(false)

  // State versions
  const [stateVersions, setStateVersions] = useState<StateVersionItem[]>([])
  const [stateLoading, setStateLoading] = useState(false)
  const [stateActionLoading, setStateActionLoading] = useState<string | null>(null)
  const [confirmStateAction, setConfirmStateAction] = useState<{ action: 'delete' | 'rollback'; sv: StateVersionItem } | null>(null)

  // Configuration versions (Configurations tab)
  const [cvs, setCvs] = useState<ConfigurationVersionItem[]>([])
  const [cvCurrentId, setCvCurrentId] = useState<string | null>(null)
  const [cvLoading, setCvLoading] = useState(false)
  const [cvSelected, setCvSelected] = useState<Set<string>>(new Set())
  const [cvDiff, setCvDiff] = useState<CVDiffResult | null>(null)
  const [cvDiffLoading, setCvDiffLoading] = useState(false)
  const [cvDiffError, setCvDiffError] = useState('')

  // Variable editing
  const [editingVarId, setEditingVarId] = useState<string | null>(null)
  const [editVarKey, setEditVarKey] = useState('')
  const [editVarValue, setEditVarValue] = useState('')
  const [editVarCategory, setEditVarCategory] = useState('native')
  const [editVarSensitive, setEditVarSensitive] = useState(false)
  const [editVarApplyOnly, setEditVarApplyOnly] = useState(false)
  const [editVarConfigureOnly, setEditVarConfigureOnly] = useState(false)
  const [editVarHcl, setEditVarHcl] = useState(false)
  const [savingVar, setSavingVar] = useState(false)

  // Drift detection
  const [savingDrift, setSavingDrift] = useState(false)
  const [savingPlanExpiry, setSavingPlanExpiry] = useState(false)
  const [checkingDrift, setCheckingDrift] = useState(false)
  const [dismissingDrift, setDismissingDrift] = useState(false)

  // AI plan summary (#401). Local draft for the context textarea so we
  // can autosave on blur rather than every keystroke; mode is saved on
  // dropdown change directly.
  const [savingAiSummary, setSavingAiSummary] = useState(false)
  const [savingSecurityScan, setSavingSecurityScan] = useState(false)
  const [scanSkipRulesDraft, setScanSkipRulesDraft] = useState<string | null>(null)
  const [aiSummaryContextDraft, setAiSummaryContextDraft] = useState<string | null>(null)

  // Runner debug mode (#1764). A held pod keeps the run's credentials, so the
  // toggle is deliberate rather than a convenience — hence the touch confirm.
  const [savingDebugMode, setSavingDebugMode] = useState(false)
  const [savingForkPlans, setSavingForkPlans] = useState(false)

  // Slack run notifications (#556). Local draft for the channel input,
  // autosaved on blur. Opt-in: empty channel = this workspace stays silent.
  const [savingSlackChannel, setSavingSlackChannel] = useState(false)
  const [slackChannelDraft, setSlackChannelDraft] = useState<string | null>(null)

  // Delete confirmation
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [deleting, setDeleting] = useState(false)

  // Label lockout warning
  const [lockoutWarning, setLockoutWarning] = useState('')

  // Remote-state sharing (#344, #349) — producer-controlled consumer allowlist
  interface RemoteStateEdge {
    id: string
    producerName: string
    producerId: string
    consumerName: string
    consumerId: string
    createdAt: string
    createdBy: string
  }
  const [rscOutbound, setRscOutbound] = useState<RemoteStateEdge[]>([])
  const [rscInbound, setRscInbound] = useState<RemoteStateEdge[]>([])
  const [rscLoading, setRscLoading] = useState(false)
  const [rscAddingId, setRscAddingId] = useState('')
  const [rscAdding, setRscAdding] = useState(false)

  // Run Triggers — cross-workspace apply-fires-plan dependency edges
  interface RunTriggerEdge {
    id: string
    workspaceId: string
    workspaceName: string
    sourceableId: string
    sourceableName: string
    createdAt: string
  }
  const [trgInbound, setTrgInbound] = useState<RunTriggerEdge[]>([])
  const [trgOutbound, setTrgOutbound] = useState<RunTriggerEdge[]>([])
  const [trgLoading, setTrgLoading] = useState(false)
  const [trgAddingId, setTrgAddingId] = useState('')
  const [trgAdding, setTrgAdding] = useState(false)

  // Notifications
  const [notifications, setNotifications] = useState<NotificationConfig[]>([])
  const [notifLoading, setNotifLoading] = useState(false)
  const [showAddNotif, setShowAddNotif] = useState(false)
  const [notifType, setNotifType] = useState<'generic' | 'slack' | 'email'>('generic')
  const [notifName, setNotifName] = useState('')
  const [notifUrl, setNotifUrl] = useState('')
  const [notifToken, setNotifToken] = useState('')
  const [notifEmails, setNotifEmails] = useState('')
  const [notifTriggers, setNotifTriggers] = useState<Set<string>>(new Set())
  const [addingNotif, setAddingNotif] = useState(false)
  const [verifyingId, setVerifyingId] = useState<string | null>(null)
  const [expandedNotifId, setExpandedNotifId] = useState<string | null>(null)

  // Run Tasks
  const [runTasks, setRunTasks] = useState<RunTaskItem[]>([])
  const [runTasksLoading, setRunTasksLoading] = useState(false)
  const [showAddRunTask, setShowAddRunTask] = useState(false)
  const [rtName, setRtName] = useState('')
  const [rtUrl, setRtUrl] = useState('')
  const [rtStage, setRtStage] = useState<string>('post_plan')
  const [rtEnforcement, setRtEnforcement] = useState<string>('mandatory')
  const [rtHmacKey, setRtHmacKey] = useState('')
  const [addingRunTask, setAddingRunTask] = useState(false)

  // Sorting for runs tab
  type RunSortKey = 'id' | 'status' | 'type' | 'source' | 'created-by' | 'created-at'
  const { sortedItems: sortedRuns, sortState: runSortState, toggleSort: toggleRunSort } = useSortable<RunItem, RunSortKey>(
    runs, 'created-at', 'desc',
    useCallback((item: RunItem, key: RunSortKey) => {
      switch (key) {
        case 'id': return item.id
        case 'status': return item.attributes.status
        // A configure comes FIRST, before the plan-only test: a configure run
        // is never plan-only (the server refuses the pair), so it would
        // otherwise sort and filter as a plan + apply -- which is what it is
        // not (#1974). A COMPOSITE reads as both, because it is.
        case 'type': return (item.attributes['is-destroy'] ? 'destroy' : item.attributes['is-configure'] ? (item.attributes['configure-after-apply'] ? 'plan + apply + configure' : 'configure') : item.attributes['plan-only'] ? 'plan only' : 'plan + apply') + (item.attributes['save-plan'] ? ' (saved)' : '')
        case 'source': return item.attributes.source
        case 'created-by': return item.attributes['created-by'] || ''
        case 'created-at': return item.attributes['created-at']
      }
    }, []),
  )

  // Sorting for state tab
  type StateSortKey = 'serial' | 'lineage' | 'size' | 'created-at' | 'created-by'
  const { sortedItems: sortedState, sortState: stateSortState, toggleSort: toggleStateSort } = useSortable<StateVersionItem, StateSortKey>(
    stateVersions, 'serial', 'desc',
    useCallback((item: StateVersionItem, key: StateSortKey) => {
      switch (key) {
        case 'serial': return item.attributes.serial
        case 'lineage': return item.attributes.lineage
        case 'size': return item.attributes.size
        case 'created-at': return item.attributes['created-at']
        case 'created-by': return item.attributes['created-by'] || ''
      }
    }, []),
  )

  // Sorting for variables tab
  type VarSortKey = 'key' | 'value' | 'category'
  const { sortedItems: sortedVars, sortState: varSortState, toggleSort: toggleVarSort } = useSortable<Variable, VarSortKey>(
    variables, 'key', 'asc',
    useCallback((item: Variable, key: VarSortKey) => {
      switch (key) {
        case 'key': return item.attributes.key
        case 'value': return item.attributes.sensitive ? '' : item.attributes.value
        case 'category': return item.attributes.category
      }
    }, []),
  )

  const loadWorkspace = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}`)
      if (!res.ok) throw new Error(await parseApiError(res, t('errors.loadWorkspace')))
      const data = await res.json()
      setWorkspace(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadWorkspace'))
    } finally {
      setLoading(false)
    }
  }, [workspaceId, t])

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadWorkspace()
  }, [router, loadWorkspace])

  const loadRuns = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}/runs`)
      if (!res.ok) throw new Error(await parseApiError(res, t('errors.loadRuns')))
      const data = await res.json()
      setRuns(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadRuns'))
    } finally {
      setRunsLoading(false)
    }
  }, [workspaceId, t])

  const loadVcsRefs = useCallback(async () => {
    if (vcsRefsLoaded || vcsRefsLoading) return
    setVcsRefsLoading(true)
    try {
      const res = await apiFetch(`/api/terrapod/v1/workspaces/${workspaceId}/vcs-refs`)
      if (!res.ok) return
      const data = await res.json()
      setVcsBranches(data.branches || [])
      setVcsTags(data.tags || [])
      setVcsDefaultBranch(data['default-branch'] || '')
      setVcsRefsLoaded(true)
    } catch {
      // Non-critical — picker just won't show refs
    } finally {
      setVcsRefsLoading(false)
    }
  }, [workspaceId, vcsRefsLoaded, vcsRefsLoading])

  // Load tab data when tab changes
  useEffect(() => {
    if (!workspace) return
    if (activeTab === 'variables') loadVariables()
    if (activeTab === 'runs') loadRuns()
    if (activeTab === 'state') loadStateVersions()
    if (activeTab === 'sharing') loadRemoteStateConsumers()
    if (activeTab === 'versions') loadConfigurations()
    if (activeTab === 'notifications') loadNotifications()
    if (activeTab === 'run-tasks') loadRunTasks()
    if (activeTab === 'run-triggers') loadRunTriggers()
  // eslint-disable-next-line react-hooks/exhaustive-deps -- loads are dispatched by the active tab; the loaders are hoisted function declarations recreated each render, so depending on them would re-fetch on every render
  }, [activeTab, workspace, loadRuns])

  // Seed the owned half of `oidc-audiences` once the catalogue has settled
  // (#1901). Deliberately NOT done where edit mode opens: the probe may still
  // be in flight there, and partitioning against an absent catalogue marks
  // every inherited entry as owned, which the next save would then write into
  // the override column. Guarded on `=== null` so it seeds once per edit
  // session and never clobbers what the operator has since typed.
  useEffect(() => {
    if (!editing || !workspace || !oidcDefaults.loaded) return
    setEditOidcAudiences((current) =>
      current === null
        ? partitionOidcAudiences(
            workspace.attributes['oidc-audiences'] || {},
            oidcDefaults.audiences,
          ).owned
        : current,
    )
  }, [editing, workspace, oidcDefaults.loaded, oidcDefaults.audiences])

  // Load VCS refs when plan options panel opens on a VCS-connected workspace
  useEffect(() => {
    if (showPlanOptions && workspace?.attributes['vcs-repo-url']) {
      loadVcsRefs()
    }
  }, [showPlanOptions, workspace, loadVcsRefs])

  // Real-time workspace events via SSE (run status, lock/unlock, state, settings)
  const { connected: sseConnected } = useRunEvents(workspaceId, useCallback((event) => {
    loadWorkspace()
    const ev = event.event
    const reconnect = ev === 'reconnect'
    if (activeTab === 'runs') loadRuns()
    if (activeTab === 'state' && (ev === 'state_version_created' || reconnect)) loadStateVersions()
    if (activeTab === 'variables' && (ev === 'workspace_variable_change' || reconnect)) loadVariables()
    if (activeTab === 'notifications' && (ev === 'workspace_notification_change' || reconnect)) loadNotifications()
    if (activeTab === 'run-tasks' && (ev === 'workspace_run_task_change' || reconnect)) loadRunTasks()
    if (activeTab === 'run-triggers' && (ev === 'run_trigger_change' || reconnect)) loadRunTriggers()
    if (activeTab === 'sharing' && (ev === 'remote_state_consumer_change' || reconnect)) loadRemoteStateConsumers()
    // Architecture critique lifecycle rides the same workspace SSE channel;
    // bump the panel's refresh key so it re-fetches when the critic finishes.
    if (ev && ev.startsWith('architecture_critique_')) setArchRefresh((n) => n + 1)
  // eslint-disable-next-line react-hooks/exhaustive-deps -- the callback is deliberately referentially stable — adding the loader would re-create it every render and churn the SSE subscription
  }, [activeTab, loadRuns, loadWorkspace]))
  const [archRefresh, setArchRefresh] = useState(0)
  // Whether the AI architecture critic is enabled on this deployment. The tab is
  // hidden entirely when it's off (#1036). We probe the critique endpoint once:
  // a 404 whose detail says "not enabled" ⇒ feature off; anything else (200, or
  // a 404 for "no critique yet" / "no state") ⇒ enabled, show the tab.
  const [archEnabled, setArchEnabled] = useState(false)
  useEffect(() => {
    if (!workspaceId) return
    let cancelled = false
    apiFetch(`/api/terrapod/v1/workspaces/${workspaceId}/architecture-critique`)
      .then(async (res) => {
        let off = false
        if (res.status === 404) {
          try {
            const b = await res.json()
            off = /not enabled/i.test(b?.detail ?? '')
          } catch {
            /* treat unparseable 404 as enabled */
          }
        }
        if (!cancelled) setArchEnabled(!off)
      })
      .catch(() => {
        /* transport error — leave the tab hidden rather than guess */
      })
    return () => {
      cancelled = true
    }
  }, [workspaceId])

  async function loadVariables() {
    try {
      setVariables(await fetchAllPages<Variable>(`/api/v1/workspaces/${workspaceId}/vars`))
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadVariables'))
    } finally {
      setVarsLoading(false)
    }
  }

  async function loadStateVersions() {
    try {
      setStateVersions(await fetchAllPages<StateVersionItem>(`/api/v1/workspaces/${workspaceId}/state-versions`))
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadStateVersions'))
    } finally {
      setStateLoading(false)
    }
  }

  async function downloadStateVersion(sv: StateVersionItem) {
    try {
      const resp = await apiFetch(`/api/v1/state-versions/${sv.id}/download`)
      const blob = await resp.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `state-${sv.attributes.serial}.json`
      a.click()
      URL.revokeObjectURL(url)
    } catch {
      alert(t('errors.downloadStateFile'))
    }
  }

  async function loadConfigurations() {
    setCvLoading(true)
    setCvDiff(null)
    setCvDiffError('')
    try {
      const res = await apiFetch(
        `/api/v1/workspaces/${workspaceId}/configuration-versions?page%5Bsize%5D=100`,
      )
      if (!res.ok) throw new Error(await parseApiError(res, t('errors.loadConfigurations')))
      const data = await res.json()
      setCvs(data.data || [])
      setCvCurrentId(data.meta?.['current-id'] ?? null)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadConfigurations'))
    } finally {
      setCvLoading(false)
    }
  }

  function toggleCvSelected(id: string) {
    setCvSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      // Cap at two selections — clears the oldest when a third is picked
      if (next.size > 2) {
        const first = Array.from(next)[0]
        next.delete(first)
      }
      return next
    })
    setCvDiff(null)
    setCvDiffError('')
  }

  // Mint a short-lived ticket and let the browser stream the download
  // natively to the user's save dialog. Plain navigation can't carry an
  // Authorization header, so the ticket goes in the URL — bounded by a
  // 5-min TTL and signed for this CV only. Avoids loading multi-MB
  // tarballs into JS memory as a blob.
  async function downloadCv(cvId: string) {
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/configuration-versions/${cvId}/download-ticket`,
        { method: 'POST' },
      )
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}))
        throw new Error(errBody.detail || t('errors.downloadFailedStatus', { status: res.status }))
      }
      const body = await res.json()
      const url = body?.data?.attributes?.url
      if (!url) throw new Error(t('errors.downloadTicketMissingUrl'))
      // Trigger via an anchor so the page stays put — the
      // Content-Disposition: attachment on the response makes the
      // browser download rather than navigate.
      const a = document.createElement('a')
      a.href = url
      a.rel = 'noopener'
      document.body.appendChild(a)
      a.click()
      a.remove()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.downloadFailed'))
    }
  }

  async function compareSelectedCvs() {
    if (cvSelected.size !== 2) return
    setCvDiffLoading(true)
    setCvDiffError('')
    try {
      // Order from-id (older) → to-id (newer) so the diff reads left-to-right
      // chronologically. CVs are sorted desc by created-at; we reverse-find.
      const ids = cvs
        .map(c => c.id)
        .filter(id => cvSelected.has(id))
      // ids[] preserves cv list order (newest first); reverse to get older first
      const [toId, fromId] = ids
      const res = await apiFetch('/api/terrapod/v1/configuration-versions/diff', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'configuration-version-diffs', attributes: { 'from-id': fromId, 'to-id': toId } },
        }),
      })
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}))
        throw new Error(errBody.detail || t('errors.diffFailedStatus', { status: res.status }))
      }
      const data = await res.json()
      setCvDiff(data.data.attributes)
    } catch (err) {
      setCvDiffError(err instanceof Error ? err.message : t('errors.diffFailed'))
    } finally {
      setCvDiffLoading(false)
    }
  }

  async function loadNotifications() {
    setNotifLoading(true)
    try {
      setNotifications(await fetchAllPages<NotificationConfig>(`/api/terrapod/v1/workspaces/${workspaceId}/notification-configurations`))
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadNotifications'))
    } finally {
      setNotifLoading(false)
    }
  }

  function _rscFromRow(row: { id: string; attributes: Record<string, string>; relationships: Record<string, { data: { id: string } }> }): RemoteStateEdge {
    return {
      id: row.id,
      producerName: row.attributes['producer-workspace-name'] || '',
      producerId: row.relationships?.producer?.data?.id || '',
      consumerName: row.attributes['consumer-workspace-name'] || '',
      consumerId: row.relationships?.consumer?.data?.id || '',
      createdAt: row.attributes['created-at'] || '',
      createdBy: row.attributes['created-by'] || '',
    }
  }

  async function loadRemoteStateConsumers() {
    setRscLoading(true)
    try {
      const base = `/api/terrapod/v1/workspaces/${workspaceId}/remote-state-consumers`
      const [outRows, inRows] = await Promise.all([
        fetchAllPages<Parameters<typeof _rscFromRow>[0]>(`${base}?filter[remote-state-consumer][type]=outbound`),
        fetchAllPages<Parameters<typeof _rscFromRow>[0]>(`${base}?filter[remote-state-consumer][type]=inbound`),
      ])
      setRscOutbound(outRows.map(_rscFromRow))
      setRscInbound(inRows.map(_rscFromRow))
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadConsumers'))
    } finally {
      setRscLoading(false)
    }
  }

  async function addRemoteStateConsumer(consumerId: string) {
    if (!consumerId) return
    setRscAddingId(consumerId)
    setRscAdding(true)
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/workspaces/${workspaceId}/remote-state-consumers`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/vnd.api+json' },
          body: JSON.stringify({
            data: { relationships: { consumer: { data: { id: consumerId, type: 'workspaces' } } } },
          }),
        },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: t('errors.authorizeConsumer') }))
        throw new Error(err.detail || t('errors.authorizeConsumer'))
      }
      await loadRemoteStateConsumers()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.authorizeConsumer'))
    } finally {
      setRscAdding(false)
      setRscAddingId('')
    }
  }

  async function revokeRemoteStateConsumer(edgeId: string) {
    // Irreversible: the consumer loses access to this workspace's state.
    if (!confirmDelete(t('sharing.revokeConfirm'))) return
    try {
      const res = await apiFetch(`/api/terrapod/v1/remote-state-consumers/${edgeId}`, { method: 'DELETE' })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: t('errors.revoke') }))
        throw new Error(err.detail || t('errors.revoke'))
      }
      await loadRemoteStateConsumers()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.revokeConsumer'))
    }
  }

  function _trgFromRow(row: { id: string; relationships?: { workspace?: { data?: { id: string } }; sourceable?: { data?: { id: string } } }; attributes: { 'workspace-name'?: string; 'sourceable-name'?: string; 'created-at'?: string } }): RunTriggerEdge {
    return {
      id: row.id,
      workspaceId: row.relationships?.workspace?.data?.id || '',
      workspaceName: row.attributes['workspace-name'] || '',
      sourceableId: row.relationships?.sourceable?.data?.id || '',
      sourceableName: row.attributes['sourceable-name'] || '',
      createdAt: row.attributes['created-at'] || '',
    }
  }

  async function loadRunTriggers() {
    setTrgLoading(true)
    try {
      const base = `/api/terrapod/v1/workspaces/${workspaceId}/run-triggers`
      const [inRows, outRows] = await Promise.all([
        fetchAllPages<Parameters<typeof _trgFromRow>[0]>(`${base}?filter[run-trigger][type]=inbound`),
        fetchAllPages<Parameters<typeof _trgFromRow>[0]>(`${base}?filter[run-trigger][type]=outbound`),
      ])
      setTrgInbound(inRows.map(_trgFromRow))
      setTrgOutbound(outRows.map(_trgFromRow))
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadTriggers'))
    } finally {
      setTrgLoading(false)
    }
  }

  async function addRunTrigger(sourceId: string) {
    if (!sourceId) return
    setTrgAddingId(sourceId)
    setTrgAdding(true)
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/workspaces/${workspaceId}/run-triggers`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/vnd.api+json' },
          body: JSON.stringify({
            data: { relationships: { sourceable: { data: { id: sourceId, type: 'workspaces' } } } },
          }),
        },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: t('errors.addTrigger') }))
        throw new Error(err.detail || t('errors.addTrigger'))
      }
      await loadRunTriggers()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.addTrigger'))
    } finally {
      setTrgAdding(false)
      setTrgAddingId('')
    }
  }

  async function removeRunTrigger(triggerId: string) {
    // Irreversible: removes the cross-workspace trigger edge.
    if (!confirmDelete(t('runTriggers.removeConfirm'))) return
    try {
      const res = await apiFetch(`/api/terrapod/v1/run-triggers/${triggerId}`, { method: 'DELETE' })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: t('errors.removeTrigger') }))
        throw new Error(err.detail || t('errors.removeTrigger'))
      }
      await loadRunTriggers()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.removeTrigger'))
    }
  }

  async function loadRunTasks() {
    setRunTasksLoading(true)
    try {
      setRunTasks(await fetchAllPages<RunTaskItem>(`/api/terrapod/v1/workspaces/${workspaceId}/run-tasks`))
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadRunTasks'))
    } finally {
      setRunTasksLoading(false)
    }
  }

  async function handleAddRunTask(e: React.FormEvent) {
    e.preventDefault()
    setAddingRunTask(true)
    setError('')
    try {
      const attrs: Record<string, unknown> = {
        name: rtName,
        url: rtUrl,
        stage: rtStage,
        'enforcement-level': rtEnforcement,
        enabled: true,
      }
      if (rtHmacKey) attrs['hmac-key'] = rtHmacKey

      const res = await apiFetch(`/api/terrapod/v1/workspaces/${workspaceId}/run-tasks`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'run-tasks', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.createRunTaskStatus', { status: res.status }))
      }
      setRtName('')
      setRtUrl('')
      setRtStage('post_plan')
      setRtEnforcement('mandatory')
      setRtHmacKey('')
      setShowAddRunTask(false)
      await loadRunTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.createRunTask'))
    } finally {
      setAddingRunTask(false)
    }
  }

  async function handleToggleRunTask(rt: RunTaskItem) {
    if (!confirmTouchMutation(rt.attributes.enabled ? t('runTasks.disableConfirm') : t('runTasks.enableConfirm'))) return
    try {
      const res = await apiFetch(`/api/terrapod/v1/run-tasks/${rt.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'run-tasks', attributes: { enabled: !rt.attributes.enabled } } }),
      })
      if (!res.ok) throw new Error(await parseApiError(res, t('errors.update')))
      await loadRunTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.toggleRunTask'))
    }
  }

  async function handleDeleteRunTask(rtId: string) {
    // Irreversible delete → confirm in both modes.
    if (!confirmDelete(t('runTasks.deleteConfirm', { name: runTasks.find(r => r.id === rtId)?.attributes.name ?? '' }))) return
    try {
      const res = await apiFetch(`/api/terrapod/v1/run-tasks/${rtId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(await parseApiError(res, t('errors.delete')))
      await loadRunTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.deleteRunTask'))
    }
  }

  function startEditing() {
    if (!workspace) return
    setEditName(workspace.attributes.name)
    setNameChanged(false)
    setEditCpu(workspace.attributes['resource-cpu'])
    setEditParallelism(String(workspace.attributes.parallelism ?? 10))
    setEditMemory(workspace.attributes['resource-memory'])
    setEditAutoApplyMode(workspace.attributes['auto-apply-mode'] || (workspace.attributes['auto-apply'] ? 'always' : 'never'))
    setEditExecMode(workspace.attributes['execution-mode'])
    setEditBackend(workspace.attributes['execution-backend'] || 'tofu')
    setEditVersion(workspace.attributes['terraform-version'] || '')
    setEditTerragruntEnabled(workspace.attributes['terragrunt-enabled'] ?? false)
    setEditTerragruntVersion(workspace.attributes['terragrunt-version'] || '')
    setEditPoolIds(workspace.attributes['agent-pool-ids'] || [])
    setEditLabels(workspace.attributes.labels || {})
    setEditOwner(workspace.attributes['owner-email'] || '')
    setEditVarFiles(workspace.attributes['var-files'] || [])
    setNewVarFile('')
    setEditTriggerPrefixes(workspace.attributes['trigger-prefixes'] || [])
    setNewTriggerPrefix('')
    setEditDriftIgnoreRules(workspace.attributes['drift-ignore-rules'] || [])
    setNewDriftIgnoreRule('')
    setEditOidcAudiences(null)
    setEditWorkingDir(workspace.attributes['working-directory'] || '')
    // `?? default` rather than `|| default` on both booleans: a stored `false`
    // is a value, and `||` would read it as absent and seed the column default
    // instead -- which for `plan-apply-enabled` means the editor would offer to
    // turn back on exactly the operation the operator turned off (#1966).
    setEditPlanApplyEnabled(workspace.attributes['plan-apply-enabled'] ?? true)
    setEditConfigureEnabled(workspace.attributes['configure-enabled'] ?? false)
    setEditAutoConfigureEnabled(workspace.attributes['auto-configure-enabled'] ?? false)
    setEditConfigureAfterApply(workspace.attributes['configure-after-apply'] ?? false)
    setEditConfigureAllHosts(workspace.attributes['configure-all-hosts'] ?? false)
    // `?? ''` and nothing else to decide: the stored `''` is the configuration
    // version's root and seeds an empty box, which is the same box an older
    // API's missing key seeds (#1966).
    setEditConfigDir(workspace.attributes['config-directory'] ?? '')
    setEditInventoryPaths(workspace.attributes['inventory-paths'] || [])
    setEditDynamicInventoryEnabled(workspace.attributes['dynamic-inventory-enabled'] ?? false)
    setEditInventoryScriptsEnabled(workspace.attributes['inventory-scripts-enabled'] ?? false)
    setEditDefaultPlaybook(workspace.attributes['default-playbook'] || '')
    setEditDefaultLimit(workspace.attributes['default-limit'] || '')
    setEditBindPlan(workspace.attributes['pulumi-bind-plan'] ?? false)
    setEditVcsConnectionId(workspace.attributes['vcs-connection-id'] || null)
    setEditVcsRepoUrl(workspace.attributes['vcs-repo-url'] || '')
    setEditVcsBranch(workspace.attributes['vcs-branch'] || '')
    setEditVcsWorkflow(workspace.attributes['vcs-workflow'] || 'merge_then_apply')
    setEditAutoMerge(workspace.attributes['auto-merge'] || false)
    setEditAutoMergeStrategy(workspace.attributes['auto-merge-strategy'] || 'merge')
    setEditing(true)
    if (!poolsLoaded) {
      fetchAllPages<AgentPool>('/api/terrapod/v1/agent-pools').then(allPools => {
        setVisiblePools(allPools)
        setAgentPools(allPools.filter(p => p.attributes.permission === 'write' || p.attributes.permission === 'admin'))
        setPoolsLoaded(true)
      }).catch(() => {})
    }
    if (!vcsConnectionsLoaded) {
      fetchAllPages<{ id: string; attributes: { name: string; provider: string } }>('/api/terrapod/v1/vcs-connections').then(conns => {
        setVcsConnections(conns)
        setVcsConnectionsLoaded(true)
      }).catch(() => {})
    }
    // From the engine, not the execution backend: a Pulumi workspace carries a
    // backend of `tofu` that nothing reads, so keying on it would offer OpenTofu
    // releases for a field that picks the Pulumi CLI (#1559).
    const tool = versionToolFor(workspace.attributes.engine, workspace.attributes['execution-backend'])
    if (versionsTool !== tool) {
      apiFetch(`/api/terrapod/v1/binary-cache/versions?tool=${tool}`)
        .then(res => res.ok ? res.json() : { data: [] })
        .then(data => {
          setVersionSuggestions(data.data || [])
          setVersionsTool(tool)
        })
        .catch(() => {})
    }
  }

  async function handleSave(force = false) {
    setSaving(true)
    setError('')
    setLockoutWarning('')
    try {
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'workspaces',
            attributes: {
              name: editName,
              'resource-cpu': editCpu,
              parallelism: Number(editParallelism),
              'resource-memory': editMemory,
              'auto-apply-mode': editAutoApplyMode,
              'execution-mode': editExecMode,
              'execution-backend': editBackend,
              'terraform-version': editVersion,
              'terragrunt-enabled': editTerragruntEnabled,
              'terragrunt-version': editTerragruntVersion || '1.0',
              // Only the plural form is sent — the API rejects a body carrying
              // both, and the singular would replace the whole set.
              'agent-pool-ids': editPoolIds,
              'working-directory': editWorkingDir,
              // Which operations this workspace has (#1966). Both are always
              // sent, because both are always shown: the form holds the
              // workspace's own values, so there is no "did not touch this" to
              // express, and the server checks the pair it ends up with.
              'plan-apply-enabled': editPlanApplyEnabled,
              'configure-enabled': editConfigureEnabled,
              'auto-configure-enabled': editAutoConfigureEnabled,
              'configure-after-apply': editConfigureAfterApply,
              'configure-all-hosts': editConfigureAllHosts,
              // Where the configure operation runs (#1929). Sent verbatim, and
              // never `null` -- an explicit null is a 422 naming the field
              // (#1966), and `''` is the value that means the configuration
              // version's root.
              //
              // NOT trimmed, and no slash is stripped. The server REFUSES a
              // path carrying leading or trailing whitespace, a leading or
              // trailing slash, or a `..` segment, rather than normalising it
              // -- so normalising here would hide a refusal the operator needs
              // to read, and would store something other than what they typed.
              'config-directory': editConfigDir,
              // Blank-free but NOT trimmed, exactly as `oidc-audiences` below:
              // the list editor appends an empty row when Add is pressed and
              // the server refuses an empty entry, so a row left blank would
              // 422 the whole save; a kept entry is sent byte-for-byte.
              'inventory-paths': editInventoryPaths.filter((entry) => entry !== ''),
              'dynamic-inventory-enabled': editDynamicInventoryEnabled,
              'inventory-scripts-enabled': editInventoryScriptsEnabled,
              'default-playbook': editDefaultPlaybook,
              'default-limit': editDefaultLimit,
              'var-files': editVarFiles,
              'trigger-prefixes': editTriggerPrefixes,
              'drift-ignore-rules': editDriftIgnoreRules,
              // Only the OWNED entries, never the merged map the read
              // returned — sending the merge back would turn every inherited
              // entry into an override, and an entry dropped from here falls
              // back to the deployment's own value, which is the point.
              //
              // Blank-free but NOT trimmed: the server refuses a blank entry
              // rather than dropping it (so an empty row left in the editor
              // would 422 the whole save), while a kept audience is an opaque
              // string stored byte-for-byte, so trimming one would store
              // something other than what was typed.
              ...(editOidcAudiences !== null
                ? { 'oidc-audiences': sanitizeOidcAudiences(editOidcAudiences) }
                : {}),
              'vcs-repo-url': editVcsRepoUrl,
              'vcs-branch': editVcsBranch,
              'vcs-workflow': editVcsWorkflow,
              'auto-merge': editAutoMerge,
              'auto-merge-strategy': editAutoMergeStrategy,
              labels: editLabels,
              // Pulumi only: the API refuses it on any other engine.
              ...(workspace?.attributes.engine === 'pulumi' ? { 'pulumi-bind-plan': editBindPlan } : {}),
              ...(isAdmin() ? { 'owner-email': editOwner } : {}),
              ...(force ? { force: true } : {}),
            },
            relationships: {
              'vcs-connection': {
                data: editVcsConnectionId ? { id: editVcsConnectionId, type: 'vcs-connections' } : null,
              },
            },
          },
        }),
      })
      if (res.status === 409) {
        const errData = await res.json()
        const detail = errData.errors?.[0]?.detail || t('errors.labelLockout')
        setLockoutWarning(detail)
        return
      }
      if (!res.ok) throw new Error(await parseApiError(res, t('errors.updateWorkspace')))
      const data = await res.json()
      const wasRenamed = workspace && data.data.attributes.name !== workspace.attributes.name
      setWorkspace(data.data)
      setEditing(false)
      if (wasRenamed) setNameChanged(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.updateWorkspace'))
    } finally {
      setSaving(false)
    }
  }

  async function handleLockToggle() {
    if (!workspace) return
    const action = workspace.attributes.locked ? 'unlock' : 'lock'
    if (!confirmTouchMutation(action === 'unlock' ? t('lock.unlockConfirm') : t('lock.lockConfirm'))) return
    try {
      // Natively, so the padlock works on every engine (#1911). The same
      // paths are still served on /api/tfe/v2 for the CLI — they did not move.
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}/actions/${action}`, {
        method: 'POST',
      })
      if (!res.ok) {
        throw new Error(
          await parseApiError(res, action === 'unlock' ? t('errors.unlockWorkspace') : t('errors.lockWorkspace'))
        )
      }
      await loadWorkspace()
    } catch (err) {
      setError(err instanceof Error ? err.message : action === 'unlock' ? t('errors.unlockWorkspace') : t('errors.lockWorkspace'))
    }
  }

  // AI plan summary (#401), and the policy gate layered on it (#1766)
  async function handleAiSummaryAttrUpdate(patch: {
    'ai-summary-mode'?: string
    'ai-summary-context'?: string
    'ai-policy-mode'?: string
  }) {
    if (!workspace) return
    setSavingAiSummary(true)
    try {
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'workspaces', attributes: patch } }),
      })
      if (!res.ok) {
        throw new Error(await parseApiError(res, t('errors.updateAiSummary')))
      }
      const data = await res.json()
      setWorkspace(data.data)
      setAiSummaryContextDraft(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.updateAiSummary'))
    } finally {
      setSavingAiSummary(false)
    }
  }

  // Security scanning (#1036). Settable here as of #1763 — it was managed
  // through the API and the provider only, so an operator with the UI in front
  // of them had no way to see or change it.
  async function handleSecurityScanAttrUpdate(patch: Record<string, unknown>) {
    if (!workspace) return
    setSavingSecurityScan(true)
    try {
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'workspaces', attributes: patch } }),
      })
      if (!res.ok) {
        throw new Error(await parseApiError(res, t('errors.updateSecurityScan')))
      }
      const data = await res.json()
      setWorkspace(data.data)
      setScanSkipRulesDraft(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.updateSecurityScan'))
    } finally {
      setSavingSecurityScan(false)
    }
  }

  // Runner debug mode (#1764). Turning it ON is the direction that costs
  // something — a failed pod then lingers holding this workspace's decrypted
  // variables — so the touch confirm guards that direction only.
  async function handleDebugModeUpdate(next: boolean) {
    if (!workspace) return
    if (next && isTouch && !window.confirm(t('debugMode.confirmEnable'))) return
    setSavingDebugMode(true)
    try {
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'workspaces', attributes: { 'debug-mode': next } },
        }),
      })
      if (!res.ok) {
        throw new Error(await parseApiError(res, t('errors.updateDebugMode')))
      }
      const data = await res.json()
      setWorkspace(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.updateDebugMode'))
    } finally {
      setSavingDebugMode(false)
    }
  }

  // Speculative plans for pull requests opened from a fork
  // (GHSA-gp5w-76rw-c452). A fork author has no write access and cannot
  // merge, so the plan is the only way their code runs with this workspace's
  // credentials — turning it ON is the direction that costs something, and
  // the touch confirm guards that one.
  async function handleForkPlansUpdate(next: boolean) {
    if (!workspace) return
    if (next && isTouch && !window.confirm(t('allowForkPrPlans.confirmEnable'))) return
    setSavingForkPlans(true)
    try {
      // `/api/v1`, not the `/api/v2` this arrived from the 1.9 line carrying. The
      // TFE surface serves only Terraform, so on a Pulumi workspace the toggle
      // would 404 with nothing shown (#1905/#1910) — and unlike on 1.9, where the
      // native spelling genuinely did not exist for this verb, main does serve
      // `PATCH /api/v1/workspaces/{id}`. Checked against api_route_contract.json.
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'workspaces', attributes: { 'allow-fork-pr-plans': next } },
        }),
      })
      if (!res.ok) {
        throw new Error(await parseApiError(res, t('errors.updateForkPlans')))
      }
      const data = await res.json()
      setWorkspace(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.updateForkPlans'))
    } finally {
      setSavingForkPlans(false)
    }
  }

  // Slack run notifications (#556)
  async function handleSlackChannelUpdate(channel: string) {
    if (!workspace) return
    setSavingSlackChannel(true)
    try {
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'workspaces', attributes: { 'slack-channel': channel } },
        }),
      })
      if (!res.ok) {
        throw new Error(await parseApiError(res, t('errors.updateSlackChannel')))
      }
      const data = await res.json()
      setWorkspace(data.data)
      setSlackChannelDraft(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.updateSlackChannel'))
    } finally {
      setSavingSlackChannel(false)
    }
  }

  async function handleDriftToggle() {
    if (!workspace) return
    setSavingDrift(true)
    try {
      const newEnabled = !attrs['drift-detection-enabled']
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'workspaces', attributes: { 'drift-detection-enabled': newEnabled } },
        }),
      })
      if (!res.ok) throw new Error(await parseApiError(res, t('errors.updateDriftSettings')))
      const data = await res.json()
      setWorkspace(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.updateDriftSettings'))
    } finally {
      setSavingDrift(false)
    }
  }

  async function handleDriftIntervalChange(seconds: number) {
    setSavingDrift(true)
    try {
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'workspaces', attributes: { 'drift-detection-interval-seconds': seconds } },
        }),
      })
      if (!res.ok) throw new Error(await parseApiError(res, t('errors.updateDriftInterval')))
      const data = await res.json()
      setWorkspace(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.updateDriftInterval'))
    } finally {
      setSavingDrift(false)
    }
  }

  // Plan expiry TTL (#646): 0 / empty disables (sent as null).
  async function handlePlanExpiryChange(seconds: number) {
    setSavingPlanExpiry(true)
    try {
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'workspaces',
            attributes: { 'plan-expiry-seconds': seconds > 0 ? seconds : null },
          },
        }),
      })
      if (!res.ok) throw new Error(await parseApiError(res, t('errors.updatePlanExpiry')))
      const data = await res.json()
      setWorkspace(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.updatePlanExpiry'))
    } finally {
      setSavingPlanExpiry(false)
    }
  }

  async function handleCheckDriftNow() {
    setCheckingDrift(true)
    setError('')
    try {
      const res = await apiFetch(`/api/v1/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'runs',
            attributes: {
              'plan-only': true,
              'is-drift-detection': true,
              message: t('runMessages.manualDriftCheck'),
            },
            relationships: {
              workspace: { data: { type: 'workspaces', id: workspaceId } },
            },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.queueDriftCheckStatus', { status: res.status }))
      }
      const runData = await res.json().catch(() => null)
      const newRunId = runData?.data?.id as string | undefined
      if (newRunId) {
        setLastQueuedRunId(newRunId)
        setTimeout(() => setLastQueuedRunId((prev) => prev === newRunId ? null : prev), 8000)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.queueDriftCheck'))
    } finally {
      setCheckingDrift(false)
    }
  }

  async function handleDismissDrift() {
    setDismissingDrift(true)
    setError('')
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/workspaces/${workspaceId}/actions/dismiss-drift`,
        { method: 'POST' }
      )
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.dismissDriftStatus', { status: res.status }))
      }
      await loadWorkspace()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.dismissDrift'))
    } finally {
      setDismissingDrift(false)
    }
  }

  async function handleDelete() {
    setDeleting(true)
    try {
      const res = await apiFetch(`/api/terrapod/v1/workspaces/${workspaceId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(await parseApiError(res, t('errors.deleteWorkspace')))
      router.push('/workspaces')
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.deleteWorkspace'))
      setDeleting(false)
    }
  }

  // The git-auth VCS-connection picker needs the connection list; it's otherwise
  // only fetched when editing workspace settings. Load it on demand when a git
  // category is picked (any connection is selectable — module auth can use a
  // different connection than the one fetching the workspace's own code).
  function ensureVcsConnections() {
    if (vcsConnectionsLoaded) return
    fetchAllPages<{ id: string; attributes: { name: string; provider: string } }>(
      '/api/terrapod/v1/vcs-connections'
    )
      .then((conns) => {
        setVcsConnections(conns)
        setVcsConnectionsLoaded(true)
      })
      .catch(() => {})
  }

  // Serialize the git-auth credential builder into the JSON value the API stores
  // (#1028). git creds are always sensitive. Non-git categories use the raw value.
  function buildGitAuthValue(): string {
    if (varCategory === 'git_http_auth') {
      return JSON.stringify(
        gitSource === 'vcs_connection'
          ? { source: 'vcs_connection', vcs_connection_id: gitVcsConnId, rewrite: gitRewrite }
          : { source: 'static', username: gitUsername || 'x-access-token', token: gitToken, rewrite: gitRewrite }
      )
    }
    return JSON.stringify({ private_key: gitPrivateKey, known_hosts: gitKnownHosts, rewrite: gitRewrite })
  }

  async function ensureVaultAvailability() {
    if (vaultAvailable || vaultInstances.length) return
    try {
      const res = await apiFetch('/api/terrapod/v1/vault/availability')
      if (!res.ok) return
      const attrs = (await res.json()).data?.attributes ?? {}
      setVaultAvailable(Boolean(attrs.enabled))
      setVaultInstances(attrs.instances ?? [])
      setVaultDefaultInstance(attrs['default-instance'] ?? '')
    } catch {
      // An affordance, not a gate: a failed probe just means the option is not
      // offered, never a broken form.
    }
  }

  // The ADD form's two phase-slot flags, bridged to the shared control so the
  // mutual exclusion (#2190) is the one in `PhaseSlotFlags` rather than a
  // second copy of the rule written out here.
  const phaseSlots = { applyOnly: varApplyOnly, configureOnly: varConfigureOnly }

  function patchPhaseSlots(patch: Partial<typeof phaseSlots>) {
    if (patch.applyOnly !== undefined) setVarApplyOnly(patch.applyOnly)
    if (patch.configureOnly !== undefined) setVarConfigureOnly(patch.configureOnly)
  }

  // Flat edit state, shaped for the shared edit panel.
  const editPanelState = {
    key: editVarKey,
    value: editVarValue,
    category: editVarCategory,
    sensitive: editVarSensitive,
    applyOnly: editVarApplyOnly,
    configureOnly: editVarConfigureOnly,
    hcl: editVarHcl,
    source: editVarSource,
    vault: editVaultRef,
  }

  function patchEditPanel(patch: Partial<typeof editPanelState>) {
    if (patch.key !== undefined) setEditVarKey(patch.key)
    if (patch.value !== undefined) setEditVarValue(patch.value)
    if (patch.category !== undefined) setEditVarCategory(patch.category)
    if (patch.sensitive !== undefined) setEditVarSensitive(patch.sensitive)
    if (patch.applyOnly !== undefined) setEditVarApplyOnly(patch.applyOnly)
    if (patch.configureOnly !== undefined) setEditVarConfigureOnly(patch.configureOnly)
    if (patch.hcl !== undefined) setEditVarHcl(patch.hcl)
    if (patch.source !== undefined) setEditVarSource(patch.source)
    if (patch.vault) setEditVaultRef(patch.vault)
  }

  async function handleAddVariable(e: React.FormEvent) {
    e.preventDefault()
    setAddingVar(true)
    setError('')
    try {
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}/vars`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'vars',
            attributes: {
              key: varKey,
              value: isGitCat
                ? buildGitAuthValue()
                : isVaultSource
                  ? buildVaultReference(vaultRef)
                  : varValue,
              category: varCategory,
              // An Ansible credential is forced sensitive by the API (#1933),
              // so send what will be stored rather than a checkbox the form
              // does not show for it.
              sensitive: isGitCat || isAnsibleCat || isVaultSource ? true : varSensitive,
              'apply-only': varApplyOnly,
              'configure-only': varConfigureOnly,
              hcl: varHcl,
              'value-source': isVaultSource ? 'vault' : 'static',
            },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.addVariableStatus', { status: res.status }))
      }
      setVarKey('')
      setVarValue('')
      setVarCategory('native')
      setVarSensitive(false)
      setVarApplyOnly(false)
      setVarConfigureOnly(false)
      setVarHcl(false)
      setGitSource('vcs_connection')
      setGitVcsConnId('')
      setGitUsername('')
      setGitToken('')
      setGitPrivateKey('')
      setGitKnownHosts('')
      setGitRewrite('none')
      setVarSource('static')
      setVaultRef(emptyVaultReference())
      setShowAddVar(false)
      await loadVariables()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.addVariable'))
    } finally {
      setAddingVar(false)
    }
  }

  async function handleDeleteVariable(varId: string) {
    // Irreversible delete → confirm in both modes.
    if (!confirmDelete(t('variables.deleteConfirm', { key: variables.find(v => v.id === varId)?.attributes.key ?? '' }))) return
    try {
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}/vars/${varId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(await parseApiError(res, t('errors.deleteVariable')))
      await loadVariables()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.deleteVariable'))
    }
  }

  // Queue a STANDALONE configure (#1974): the workspace's playbook against its
  // inventory, with no plan and no apply.
  //
  // `confirm()` in BOTH pointer modes, not touch only. The two-tier rule puts
  // an irreversible action in both, and this is one: a playbook can restart
  // services, rotate credentials and reboot machines, and unlike an apply there
  // is no plan to review first. The message names what it will reach, because
  // an empty limit means the whole inventory.
  async function handleQueueConfigure() {
    const reach = (configureLimit || attrs['default-limit'] || '').trim()
    const msg = reach
      ? t('runs.queueConfigureConfirmLimited', { limit: reach })
      : t('runs.queueConfigureConfirmAll')
    if (!window.confirm(msg)) return
    setQueueingConfigure(true)
    setError('')
    try {
      const runAttrs: Record<string, unknown> = {
        'is-configure': true,
        'plan-only': false,
        message: t('runMessages.queuedConfigure'),
      }
      // Sent only when the operator typed one: empty means "the workspace's
      // default", and sending "" would be indistinguishable from naming it.
      if (configurePlaybook.trim()) runAttrs['playbook'] = configurePlaybook.trim()
      if (configureLimit.trim()) runAttrs['limit'] = configureLimit.trim()

      const res = await apiFetch(`/api/v1/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'runs',
            attributes: runAttrs,
            relationships: {
              workspace: { data: { type: 'workspaces', id: workspaceId } },
            },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.queueConfigureStatus', { status: res.status }))
      }
      const runData = await res.json().catch(() => null)
      const newRunId = runData?.data?.id as string | undefined
      if (newRunId) {
        setLastQueuedRunId(newRunId)
        setTimeout(() => setLastQueuedRunId((prev) => prev === newRunId ? null : prev), 8000)
      }
      setShowPlanOptions(false)
      setConfigurePlaybook('')
      setConfigureLimit('')
      await loadRuns()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.queueConfigure'))
    } finally {
      setQueueingConfigure(false)
    }
  }

  // `planOnly` is the button pressed, not a stored toggle (#1340) — the choice
  // is per-press, so nothing persists between runs to be misread later.
  async function handleQueuePlan(planOnly: boolean) {
    // A non-default VCS ref is forced plan-only server-side. The apply button
    // is disabled in that state, so reaching here with both set would mean the
    // guard failed — refuse rather than silently queue the wrong kind of run.
    if (!planOnly && vcsRef) return
    // Touch fat-finger guard — queuing a run is a state change (and a
    // `plan + apply` run can change infrastructure), so a stray tap on a touch
    // device (any width) gets a native confirm; a precise pointer runs
    // immediately.
    if (isTouch) {
      const msg = planOnly
        ? phaseWord('queuePlanConfirm')
        : phaseWord('queueApplyConfirm')
      if (!window.confirm(msg)) return
    }
    setQueueingPlan(true)
    setError('')
    try {
      const attrs: Record<string, unknown> = {
        'plan-only': planOnly,
        message: planOnly ? t('runMessages.queuedSpeculative') : t('runMessages.queued'),
      }
      const targets = planTargets.split(',').map(s => s.trim()).filter(Boolean)
      const replaces = planReplaces.split(',').map(s => s.trim()).filter(Boolean)
      if (targets.length) attrs['target-addrs'] = targets
      if (replaces.length) attrs['replace-addrs'] = replaces
      if (planRefreshOnly) attrs['refresh-only'] = true
      if (!planRefresh) attrs['refresh'] = false
      // Never sent on a Pulumi workspace: the option has no Pulumi equivalent
      // and the server stores it without ever reading it (#1911). The control
      // is absent there, so this only guards a stale tick from an engine change.
      if (planAllowEmpty && !isPulumi) attrs['allow-empty-apply'] = true
      if (vcsRef) attrs['vcs-ref'] = vcsRef

      const res = await apiFetch(`/api/v1/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'runs',
            attributes: attrs,
            relationships: {
              workspace: { data: { type: 'workspaces', id: workspaceId } },
            },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || tPhase(phaseKeyFor('errorsQueuePlanStatus'), {
          status: res.status,
        }))
      }
      const runData = await res.json().catch(() => null)
      const newRunId = runData?.data?.id as string | undefined
      if (newRunId) {
        setLastQueuedRunId(newRunId)
        setTimeout(() => setLastQueuedRunId((prev) => prev === newRunId ? null : prev), 8000)
      }
      setShowPlanOptions(false)
      setPlanTargets('')
      setPlanReplaces('')
      setPlanRefreshOnly(false)
      setPlanRefresh(true)
      setPlanAllowEmpty(false)
      setVcsRef('')
      await loadRuns()
    } catch (err) {
      setError(err instanceof Error ? err.message : phaseWord('errorsQueuePlan'))
    } finally {
      setQueueingPlan(false)
    }
  }

  async function handleQueueDestroy() {
    setQueueingDestroy(true)
    setError('')
    try {
      const res = await apiFetch(`/api/v1/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'runs',
            attributes: {
              'is-destroy': true,
              message: t('runMessages.destroyQueued'),
            },
            relationships: {
              workspace: { data: { type: 'workspaces', id: workspaceId } },
            },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.queueDestroyStatus', { status: res.status }))
      }
      const runData = await res.json().catch(() => null)
      const newRunId = runData?.data?.id as string | undefined
      if (newRunId) {
        setLastQueuedRunId(newRunId)
        setTimeout(() => setLastQueuedRunId((prev) => prev === newRunId ? null : prev), 8000)
      }
      setShowDestroyConfirm(false)
      await loadRuns()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.queueDestroy'))
    } finally {
      setQueueingDestroy(false)
    }
  }

  function startEditingVar(v: Variable) {
    setEditingVarId(v.id)
    setEditVarKey(v.attributes.key)
    setEditVarCategory(v.attributes.category)
    setEditVarSensitive(v.attributes.sensitive)
    setEditVarApplyOnly(v.attributes['apply-only'] ?? false)
    setEditVarConfigureOnly(v.attributes['configure-only'] ?? false)
    setEditVarHcl(v.attributes.structured)
    ensureVaultAvailability()

    const source = v.attributes['value-source'] === 'vault' ? 'vault' : 'static'
    setEditVarSource(source)
    if (source === 'vault') {
      // The reference is not a secret, so it is loaded back into the fields
      // rather than blanked like a sensitive value — otherwise every edit would
      // silently rebuild it from nothing. The whole reference is kept, so keys
      // the form does not render (method, data, …) survive the save (#1619).
      setEditVaultRef(parseVaultReference(v.attributes.value))
      setEditVarValue('')
    } else {
      setEditVarValue(v.attributes.sensitive ? '' : v.attributes.value)
    }
  }

  async function handleSaveVar() {
    if (!editingVarId) return
    setSavingVar(true)
    setError('')
    try {
      // The category is editable inside the panel, so a variable can arrive
      // here as `vault`-sourced and structured and leave as an Ansible
      // credential, which the API refuses on both counts (#1933). The panel
      // hides those controls for a credential category; this is the other half
      // — not sending the state they left behind, which would spend the edit on
      // a 422 about a control the operator can no longer see.
      const isEditAnsible = isAnsibleAuthCategory(editVarCategory)
      const isEditVault = editVarSource === 'vault' && !isEditAnsible
      const attrs: Record<string, unknown> = {
        key: editVarKey,
        category: editVarCategory,
        sensitive: isEditVault || isEditAnsible ? true : editVarSensitive,
        'apply-only': editVarApplyOnly,
        'configure-only': editVarConfigureOnly,
        hcl: isEditAnsible ? false : editVarHcl,
        'value-source': isEditVault ? 'vault' : 'static',
      }
      if (isEditVault) {
        // Always sent: the reference is rebuilt from the fields on screen, so
        // omitting it when unchanged would drop the edit just made.
        attrs.value = buildVaultReference(editVaultRef)
      } else if (editVarValue !== '') {
        attrs.value = editVarValue
      }
      const res = await apiFetch(`/api/v1/workspaces/${workspaceId}/vars/${editingVarId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'vars', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.updateVariable'))
      }
      setEditingVarId(null)
      await loadVariables()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.updateVariable'))
    } finally {
      setSavingVar(false)
    }
  }

  // Notification handlers
  async function handleAddNotification(e: React.FormEvent) {
    e.preventDefault()
    setAddingNotif(true)
    setError('')
    try {
      const attrs: Record<string, unknown> = {
        name: notifName,
        'destination-type': notifType,
        triggers: Array.from(notifTriggers),
        enabled: false,
      }
      if (notifType !== 'email') attrs.url = notifUrl
      if (notifType === 'generic' && notifToken) attrs.token = notifToken
      if (notifType === 'email') attrs['email-addresses'] = notifEmails.split(',').map(s => s.trim()).filter(Boolean)

      const res = await apiFetch(`/api/terrapod/v1/workspaces/${workspaceId}/notification-configurations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'notification-configurations', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.createNotificationStatus', { status: res.status }))
      }
      setNotifName('')
      setNotifUrl('')
      setNotifToken('')
      setNotifEmails('')
      setNotifTriggers(new Set())
      setShowAddNotif(false)
      await loadNotifications()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.createNotification'))
    } finally {
      setAddingNotif(false)
    }
  }

  async function handleToggleNotif(nc: NotificationConfig) {
    if (!confirmTouchMutation(nc.attributes.enabled ? t('notifications.disableConfirm') : t('notifications.enableConfirm'))) return
    try {
      const res = await apiFetch(`/api/terrapod/v1/notification-configurations/${nc.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'notification-configurations', attributes: { enabled: !nc.attributes.enabled } } }),
      })
      if (!res.ok) throw new Error(await parseApiError(res, t('errors.update')))
      await loadNotifications()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.toggleNotification'))
    }
  }

  async function handleDeleteNotif(ncId: string) {
    // Irreversible delete → confirm in both modes.
    if (!confirmDelete(t('notifications.deleteConfirm', { name: notifications.find(n => n.id === ncId)?.attributes.name ?? '' }))) return
    try {
      const res = await apiFetch(`/api/terrapod/v1/notification-configurations/${ncId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(await parseApiError(res, t('errors.delete')))
      await loadNotifications()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.deleteNotification'))
    }
  }

  async function handleVerifyNotif(ncId: string) {
    setVerifyingId(ncId)
    setError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/notification-configurations/${ncId}/actions/verify`, { method: 'POST' })
      if (!res.ok) throw new Error(await parseApiError(res, t('errors.verificationFailed')))
      const data = await res.json()
      const success = data?.data?.attributes?.success
      if (success) {
        setError('')
      } else {
        setError(t('errors.verificationDeliveryFailed', { detail: data?.data?.attributes?.body || t('errors.unknownError') }))
      }
      await loadNotifications()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.verificationFailed'))
    } finally {
      setVerifyingId(null)
    }
  }

  function toggleTrigger(t: string) {
    setNotifTriggers(prev => {
      const next = new Set(prev)
      if (next.has(t)) next.delete(t)
      else next.add(t)
      return next
    })
  }

  // Top-level tab GROUPS (#1036 UX). Related sub-views are merged under one
  // parent tab with a segmented sub-toggle, so the strip stays short:
  //   State      = State + State Graph
  //   Insights   = Cost + Architecture (Architecture only when the AI critic is on)
  //   Automation = Run Tasks + Run Triggers
  // A group's `key` is its DEFAULT sub-view (what clicking the parent opens);
  // `members` are the `?tab=` values it owns. The URL stays the source of truth.
  //
  // Insights is empty on a Pulumi workspace — both of its members read
  // Terraform state — and a group with no members is dropped from the strip
  // entirely (#1568). The engine test is deliberately independent of the
  // `archEnabled` probe above: the engine filter answers 404 "Workspace not
  // found", which that probe reads as "critic enabled", so relying on it would
  // leave the tab visible and broken.
  const insightsMembers: Tab[] = isPulumi
    ? []
    : archEnabled
      ? ['cost', 'architecture']
      : ['cost']
  // Empty until this workspace actually has an inventory, which is what hides
  // the tab from every workspace that does not (#1986).
  const inventoryMembers: Tab[] = hasInventory ? ['inventory'] : []
  const tabGroups: { key: Tab; label: string; members: Tab[] }[] = ([
    { key: 'configuration', label: t('tabs.configuration'), members: ['configuration'] },
    { key: 'variables', label: t('tabs.variables'), members: ['variables'] },
    { key: 'runs', label: t('tabs.runs'), members: ['runs'] },
    { key: 'state', label: t('tabs.state'), members: ['state', 'state-graph'] },
    { key: 'cost', label: t('tabs.insights'), members: insightsMembers },
    { key: 'versions', label: t('tabs.versions'), members: ['versions'] },
    { key: 'notifications', label: t('tabs.notifications'), members: ['notifications'] },
    { key: 'run-tasks', label: t('tabs.automation'), members: ['run-tasks', 'run-triggers'] },
    // Data-gated: the probe answers false until a row exists, and a
    // group with no members is dropped from the strip entirely — the same
    // mechanism Insights uses on a Pulumi workspace (#1967, #1968).
    { key: 'inventory', label: t('tabs.inventory'), members: inventoryMembers },
    { key: 'sharing', label: t('tabs.sharing'), members: ['sharing'] },
    { key: 'access', label: t('tabs.access'), members: ['access'] },
  ] as { key: Tab; label: string; members: Tab[] }[]).filter((g) => g.members.length > 0)
  const activeGroup = tabGroups.find((g) => g.members.includes(activeTab)) ?? tabGroups[0]
  const subTabLabel = (tab: Tab): string =>
    ({
      state: t('subtabs.state'),
      'state-graph': t('subtabs.stateGraph'),
      cost: t('subtabs.cost'),
      architecture: t('subtabs.architecture'),
      'run-tasks': t('subtabs.runTasks'),
      'run-triggers': t('subtabs.runTriggers'),
    })[tab as string] ?? tab
  // A merged group that collapses to a single visible member (e.g. Insights
  // when the architecture critic is disabled → just Cost) shows that member's
  // OWN label, not the collective group name — otherwise "Insights" would
  // stand in for a lone "Cost". Always-single groups keep their group label.
  const groupLabel = (g: { label: string; members: Tab[] }): string => {
    if (g.members.length > 1) return g.label
    const only = g.members[0]
    const sub = subTabLabel(only)
    return sub !== only ? sub : g.label
  }

  function statusColor(status: string): string {
    switch (status) {
      case 'applied': return 'bg-green-900/50 text-green-300'
      case 'planned': return 'bg-blue-900/50 text-blue-300'
      case 'planning': case 'applying': case 'canceling': return 'bg-yellow-900/50 text-yellow-300'
      case 'errored': return 'bg-red-900/50 text-red-300'
      case 'canceled': case 'discarded': return 'bg-slate-700 text-slate-400'
      default: return 'bg-slate-700 text-slate-400'
    }
  }

  function driftStatusBadge(s: string): { cls: string; label: string } {
    switch (s) {
      case 'no_drift': return { cls: 'bg-green-900/50 text-green-300', label: t('drift.statusNoDrift') }
      case 'drifted': return { cls: 'bg-amber-900/50 text-amber-300', label: t('drift.statusDrifted') }
      case 'errored': return { cls: 'bg-red-900/50 text-red-300', label: t('drift.statusErrored') }
      default: return { cls: 'bg-slate-700 text-slate-400', label: t('drift.statusUnchecked') }
    }
  }

  const DRIFT_INTERVALS = [
    { label: t('intervals.1hour'), value: 3600 },
    { label: t('intervals.4hours'), value: 14400 },
    { label: t('intervals.12hours'), value: 43200 },
    { label: t('intervals.24hours'), value: 86400 },
    { label: t('intervals.48hours'), value: 172800 },
    { label: t('intervals.7days'), value: 604800 },
  ]

  // #646 plan-expiry TTL choices; 0 = disabled (the default).
  const PLAN_EXPIRY_OPTIONS = [
    { label: t('intervals.disabled'), value: 0 },
    { label: t('intervals.1hour'), value: 3600 },
    { label: t('intervals.4hours'), value: 14400 },
    { label: t('intervals.12hours'), value: 43200 },
    { label: t('intervals.24hours'), value: 86400 },
    { label: t('intervals.3days'), value: 259200 },
    { label: t('intervals.7days'), value: 604800 },
  ]

  function stageBadge(s: string): string {
    switch (s) {
      case 'pre_plan': return 'bg-amber-900/50 text-amber-300'
      case 'post_plan': return 'bg-blue-900/50 text-blue-300'
      case 'pre_apply': return 'bg-purple-900/50 text-purple-300'
      default: return 'bg-slate-700 text-slate-400'
    }
  }

  function enforcementBadge(e: string): string {
    switch (e) {
      case 'mandatory': return 'bg-red-900/50 text-red-300'
      case 'advisory': return 'bg-yellow-900/50 text-yellow-300'
      default: return 'bg-slate-700 text-slate-400'
    }
  }

  function destTypeBadge(t: string): string {
    switch (t) {
      case 'generic': return 'bg-blue-900/50 text-blue-300'
      case 'slack': return 'bg-purple-900/50 text-purple-300'
      case 'email': return 'bg-cyan-900/50 text-cyan-300'
      default: return 'bg-slate-700 text-slate-400'
    }
  }

  if (loading) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>
  if (!workspace) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><ErrorBanner message={t('notFound')} /></main></>

  const attrs = workspace.attributes
  const perms = attrs.permissions || {} as WorkspacePermissions

  // `oidc-audiences` arrives MERGED, so provenance only exists by subtracting
  // the deployment catalogue. Derived on every render rather than stored: the
  // catalogue arrives asynchronously, and a value captured before it landed
  // would show every entry as workspace-owned for ever. The edit state is
  // seeded from `.owned` separately, when edit mode opens.
  const oidcAudienceSplit = partitionOidcAudiences(
    attrs['oidc-audiences'] || {},
    oidcDefaults.audiences,
  )

  // VCS polling is stalled when the most recent ATTEMPT is newer than the most
  // recent SUCCESS. Comparing the two needs no knowledge of the poll interval,
  // and it catches the case an error message alone misses (#1089).
  const vcsPollStalled =
    !attrs['vcs-last-error'] &&
    !!attrs['vcs-last-attempted-at'] &&
    (!attrs['vcs-last-polled-at'] ||
      new Date(attrs['vcs-last-attempted-at']) > new Date(attrs['vcs-last-polled-at']))

  /** A pool's display name: from the server's positional `agent-pool-names`
   *  first, then the fetched pool list, and only then the raw id.
   *
   *  The server list is what makes the READ-ONLY view work: the pool list is
   *  fetched on entering edit mode, so before that `visiblePools` is empty and
   *  every pool rendered as a bare `apool-<uuid>`. It also covers a caller who
   *  can read the workspace but not list pools. */
  function poolLabel(id: string): string {
    const ids = attrs['agent-pool-ids'] || []
    const names = attrs['agent-pool-names'] || []
    const i = ids.indexOf(id)
    if (i >= 0 && names[i]) return names[i]
    return visiblePools.find((p) => p.id === id)?.attributes.name ?? id
  }

  // Rows for the pool-set editor: everything the caller may assign, PLUS any
  // pool already on the workspace that they may not. Without the second half a
  // caller lacking pool:assign on one of the pools would silently drop it just
  // by saving unrelated settings — the set is replaced wholesale on write.
  const poolRows = [
    ...agentPools.map((p) => ({ id: p.id, name: p.attributes.name, assignable: true })),
    ...(attrs['agent-pool-ids'] || [])
      .filter((id) => !agentPools.some((p) => p.id === id))
      .map((id) => ({
        id,
        name: poolLabel(id),
        assignable: false,
      })),
  ]

  // ---------------------------------------------------------------------
  // The Configuration tab's field groups (#2253)
  //
  // `workspace-fields.tsx` owns which attribute belongs to which group and
  // `workspace-field-groups.tsx` owns the markup. What lives here is the only
  // part this page can answer: what each value currently IS, and what changing
  // it means. That is exactly why a group takes `(mode, values, onChange)`
  // rather than owning a save — this tab has THREE save semantics and they
  // have to stay distinct:
  //
  //   the settings card   batched into the `edit*` buffer behind Edit / Save
  //   the drift card      per-field action buttons, plus `Check now`
  //   the six others      PATCH immediately on change
  //
  // A group that assumed any one of those would be unusable by the other two.

  /** The engine as `FIELDS` names it; an older workspace carries none. */
  const engineId = attrs.engine || 'terraform'

  /** The stored row, as the groups want it: keyed by attribute name.
   *
   *  Cast because the attributes interface has no index signature, and giving
   *  it one would stop every misspelled key being a type error — which is the
   *  more valuable of the two properties. */
  const storedValues = attrs as unknown as Record<string, unknown>

  /** An attribute's label, from the one shared map.
   *
   *  The single engine-specific exception is `auto-merge`: a Pulumi workspace
   *  merges after an UPDATE rather than an apply, so that label comes from the
   *  phase vocabulary instead of `fields.autoMerge` (#1911). Every other label
   *  is engine-neutral, which is why this is an exception rather than a second
   *  lookup table. */
  const fieldLabel = (attr: string): string =>
    attr === 'auto-merge' ? phaseWord('fieldsAutoMerge') : t(LABEL_KEY[attr])

  // NO GROUP IN THIS CARD PASSES `advanced`, and that is measured rather than
  // an oversight. A collapsed `<details>` is HIDDEN to Playwright, and the only
  // two groups here whose every field is advanced are each asserted visible by
  // an existing spec: `oidc-audiences` by responsive.spec.ts ("both the
  // provider keys and every audience stay visible, not hidden behind a
  // breakpoint") and `Drift Ignore Rules` by pulumi-workspace.spec.ts, which
  // uses its presence to tell the two engines apart. Collapsing either means
  // opening the `<details>` in those specs first, which is a change to the
  // suite rather than to this page.
  //
  // `metadata` and `vcs` are mixed — `owner-email` and `vcs-repo-url` are not
  // advanced — so neither was ever a candidate: folding a group because ONE of
  // its fields is advanced would put the repository URL out of sight.

  // Every write path in the settings card already guarded on `can-update` as
  // well as `editing`; the Edit button is itself behind that permission, so
  // the two agree in practice and one name makes the card uniform.
  const settingsEditing = editing && !!perms['can-update']

  /** What the settings card shows: the stored row, overlaid with the edit
   *  buffer while editing.
   *
   *  OVERLAID rather than replaced, so a field with no edit state of its own —
   *  and, far more important, every dependency GATE — still reads a real value
   *  instead of `undefined`, which `dependencyMet` fails closed on. */
  const settingsValues: Record<string, unknown> = settingsEditing
    ? {
        ...storedValues,
        name: editName,
        'execution-mode': editExecMode,
        'execution-backend': editBackend,
        'engine-version': editVersion,
        'working-directory': editWorkingDir,
        'auto-apply-mode': editAutoApplyMode,
        'pulumi-bind-plan': editBindPlan,
        parallelism: editParallelism,
        'resource-cpu': editCpu,
        'resource-memory': editMemory,
        'terragrunt-enabled': editTerragruntEnabled,
        'terragrunt-version': editTerragruntVersion,
        'plan-apply-enabled': editPlanApplyEnabled,
        'configure-enabled': editConfigureEnabled,
        'auto-configure-enabled': editAutoConfigureEnabled,
        'configure-after-apply': editConfigureAfterApply,
        'configure-all-hosts': editConfigureAllHosts,
        'config-directory': editConfigDir,
        'default-playbook': editDefaultPlaybook,
        'default-limit': editDefaultLimit,
        'inventory-paths': editInventoryPaths,
        'dynamic-inventory-enabled': editDynamicInventoryEnabled,
        'inventory-scripts-enabled': editInventoryScriptsEnabled,
        'agent-pool-ids': editPoolIds,
        'owner-email': editOwner,
        labels: editLabels,
        'var-files': editVarFiles,
        'vcs-connection-id': editVcsConnectionId,
        'vcs-repo-url': editVcsRepoUrl,
        'vcs-branch': editVcsBranch,
        'vcs-workflow': editVcsWorkflow,
        'auto-merge': editAutoMerge,
        'auto-merge-strategy': editAutoMergeStrategy,
        'trigger-prefixes': editTriggerPrefixes,
        'drift-ignore-rules': editDriftIgnoreRules,
        'oidc-audiences': editOidcAudiences ?? {},
      }
    : storedValues

  /** The hint under a settings field, where the old markup carried one. */
  const SETTINGS_HINTS: Record<string, string> = {
    'plan-apply-enabled': 'fields.planApplyEnabledHint',
    'configure-enabled': 'fields.configureEnabledHint',
    'configure-after-apply': 'fields.configureAfterApplyHint',
    'configure-all-hosts': 'fields.configureAllHostsHint',
    'config-directory': 'fields.configDirectoryRootHint',
    'inventory-paths': 'fields.inventoryPathsHint',
    'dynamic-inventory-enabled': 'fields.dynamicInventoryEnabledHint',
    'inventory-scripts-enabled': 'fields.inventoryScriptsEnabledHint',
    'agent-pool-ids': 'fields.agentPoolsHint',
    'trigger-prefixes': 'fields.triggerPrefixesHint',
    'oidc-audiences': 'fields.oidcAudiencesHint',
  }

  const settingsHint = (attr: string): ReactNode => {
    // `auto-configure-enabled` says what it WILL target rather than only that
    // it is on: an empty default limit means the whole inventory, and that is
    // the fact worth putting in front of somebody turning it on.
    if (attr === 'auto-configure-enabled') {
      const limit = String(settingsValues['default-limit'] ?? '').trim()
      return (
        <p className="mt-1 text-xs text-slate-400">
          {limit
            ? t('fields.autoConfigureEnabledHintLimited', { limit })
            : t('fields.autoConfigureEnabledHint')}
        </p>
      )
    }
    const key = SETTINGS_HINTS[attr]
    // A hint that only makes sense beside a control, which is where the old
    // markup put it too — a read view that repeated it would be noise.
    const editOnly = attr === 'config-directory'
    if (!key || (editOnly && !settingsEditing)) return null
    return <p className="mt-1 text-xs text-slate-400">{t(key)}</p>
  }

  /** Where each settings field's change goes.
   *
   *  One table rather than a prop per field, because a group hands the
   *  attribute name back and this page is what knows the buffer. An attribute
   *  absent from here cannot be changed in the settings card, by construction
   *  rather than by remembering to render it read-only. */
  const SETTINGS_SETTERS: Record<string, (v: unknown) => void> = {
    name: (v) => setEditName(String(v)),
    'execution-mode': (v) => setEditExecMode(String(v)),
    'execution-backend': (v) => setEditBackend(String(v)),
    'engine-version': (v) => setEditVersion(String(v)),
    'working-directory': (v) => setEditWorkingDir(String(v)),
    'auto-apply-mode': (v) => setEditAutoApplyMode(String(v)),
    'pulumi-bind-plan': (v) => setEditBindPlan(Boolean(v)),
    // Kept a STRING, because the input is one: parsing here would turn a
    // half-typed value into `NaN` under the operator's cursor, and the save
    // path already coerces it.
    parallelism: (v) => setEditParallelism(String(v)),
    'resource-cpu': (v) => setEditCpu(String(v)),
    'resource-memory': (v) => setEditMemory(String(v)),
    'terragrunt-enabled': (v) => setEditTerragruntEnabled(Boolean(v)),
    'terragrunt-version': (v) => setEditTerragruntVersion(String(v)),
    'plan-apply-enabled': (v) => setEditPlanApplyEnabled(Boolean(v)),
    'configure-enabled': (v) => setEditConfigureEnabled(Boolean(v)),
    'auto-configure-enabled': (v) => setEditAutoConfigureEnabled(Boolean(v)),
    'configure-after-apply': (v) => setEditConfigureAfterApply(Boolean(v)),
    'configure-all-hosts': (v) => setEditConfigureAllHosts(Boolean(v)),
    'config-directory': (v) => setEditConfigDir(String(v)),
    'default-playbook': (v) => setEditDefaultPlaybook(String(v)),
    'default-limit': (v) => setEditDefaultLimit(String(v)),
    'inventory-paths': (v) => setEditInventoryPaths(v as string[]),
    'dynamic-inventory-enabled': (v) => setEditDynamicInventoryEnabled(Boolean(v)),
    'inventory-scripts-enabled': (v) => setEditInventoryScriptsEnabled(Boolean(v)),
    'agent-pool-ids': (v) => setEditPoolIds(v as string[]),
    'owner-email': (v) => setEditOwner(String(v)),
    labels: (v) => setEditLabels(v as Record<string, string>),
    'var-files': (v) => setEditVarFiles(v as string[]),
    // `''` is "no connection", which the column stores as NULL.
    'vcs-connection-id': (v) => setEditVcsConnectionId(String(v) || null),
    'vcs-repo-url': (v) => setEditVcsRepoUrl(String(v)),
    'vcs-branch': (v) => setEditVcsBranch(String(v)),
    'vcs-workflow': (v) =>
      setEditVcsWorkflow(String(v) as 'merge_then_apply' | 'apply_then_merge'),
    'auto-merge': (v) => setEditAutoMerge(Boolean(v)),
    'auto-merge-strategy': (v) =>
      setEditAutoMergeStrategy(String(v) as 'merge' | 'squash' | 'rebase'),
    'trigger-prefixes': (v) => setEditTriggerPrefixes(v as string[]),
    'drift-ignore-rules': (v) => setEditDriftIgnoreRules(v as string[]),
    'oidc-audiences': (v) => setEditOidcAudiences(v as OidcAudiences),
  }

  /** The settings card's context. One `mode` for the whole card, because one
   *  Edit button governs all of it. */
  const settingsCtx: GroupCtx = {
    mode: settingsEditing ? 'edit' : 'view',
    values: settingsValues,
    onChange: (attr, v) => SETTINGS_SETTERS[attr]?.(v),
    engine: engineId,
    label: fieldLabel,
    hint: settingsHint,
    busy: saving,
  }

  /** The Ansible group's context.
   *
   *  `FIELDS` declares every Ansible attribute a dependent of
   *  `configure-enabled`, which is right for the CREATE form — there is nothing
   *  to configure before the operation is declared. On the edit tab it is not,
   *  and that is a decision already taken and already asserted: the config
   *  directory is "always present rather than revealed" in
   *  `responsive.spec.ts`'s own words (#1966), and `inventory.spec.ts` fills
   *  the path, the playbook and the limit on a fresh workspace without ticking
   *  anything. Hiding them would also hide the only place an operator can SEE
   *  what a workspace is configured to run.
   *
   *  So the gate is reported satisfied for this group alone. The three
   *  TRIGGERS — which genuinely do nothing without the operation — keep theirs,
   *  in the operations group above, where `configure-trigger.spec.ts` asserts
   *  they are absent until configure is on.
   *
   *  The shared table has no way to say "a dependent on create, always shown on
   *  edit", which is the real gap; this is the local answer until it does. */
  const ansibleCtx: GroupCtx = {
    ...settingsCtx,
    values: { ...settingsValues, 'configure-enabled': true },
  }

  /** The same card, forced read-only, for the two fields an operator may see
   *  and not change: `engine` is identity and immutable after create, and
   *  ownership grants workspace admin so only a platform admin may move it. */
  const settingsViewCtx: GroupCtx = { ...settingsCtx, mode: 'view' }

  /** A context for one of the cards that PATCHes immediately on change.
   *
   *  `values` is the STORED row, never an edit buffer: there is no buffer to
   *  read, because the change has already been sent by the time the next
   *  render happens. */
  const instantCtx = (
    onChange: (attr: string, value: unknown) => void,
    busy: boolean,
    hint?: (attr: string) => ReactNode,
  ): GroupCtx => ({
    mode: perms['can-update'] ? 'edit' : 'view',
    values: storedValues,
    onChange,
    engine: engineId,
    label: fieldLabel,
    hint,
    busy,
  })

  /** A card's own hint line, with its in-flight indicator. The indicator sits
   *  in the hint rather than beside the control because that is where the old
   *  markup put it, and it is the only feedback an instant PATCH gives. */
  const savingHint = (key: string, busy: boolean): ReactNode => (
    <p className="text-xs text-slate-500 mt-1">
      {t(key)}
      {busy && <span className="ms-2 text-brand-400">{t('actions.saving')}</span>}
    </p>
  )

  /** Whether a settings field's gate is on.
   *
   *  `<Field>` applies this itself; this is for the eight fields that span
   *  both grid columns, which keep their own `sm:col-span-2` wrapper because
   *  `WsField` has no way to widen one — a list editor squeezed into half a
   *  row is a worse outcome than a hand-written wrapper. */
  const settingsShows = (attr: string): boolean =>
    dependencyMet(FIELD_BY_ATTR[attr], settingsValues)

  /** The Enabled/Disabled toggle every boolean in this card renders.
   *
   *  One helper rather than the ten near-identical copies it replaces. Two
   *  details in it are load-bearing rather than decorative:
   *
   *    - the wrapping `<label>` IS the tap target. A bare 16px checkbox is not
   *      one at phone width, and `min-h-11` gives the row 44px while
   *      collapsing to nothing on a precise pointer.
   *    - the explicit `aria-label`. Without it the accessible name is the
   *      CONCATENATION of the field's own label and this row's Enabled /
   *      Disabled text, so the name would change as the control toggles — and
   *      the e2e suite locates several of these by their exact label. */
  const settingsToggle = (attr: string): ReactNode => {
    const id = FIELD_BY_ATTR[attr]?.id ?? attr
    const checked = Boolean(settingsValues[attr])
    return (
      <label className="mt-1 flex items-center gap-2 min-h-11 sm:min-h-0">
        <input
          id={id}
          data-testid={id}
          type="checkbox"
          checked={checked}
          disabled={saving}
          aria-label={fieldLabel(attr)}
          onChange={(e) => SETTINGS_SETTERS[attr]?.(e.target.checked)}
          className="rounded border-slate-600 bg-slate-700 text-brand-600"
        />
        <span className="text-sm text-slate-200">
          {checked ? t('common.enabled') : t('common.disabled')}
        </span>
      </label>
    )
  }

  /** A boolean's READ view.
   *
   *  The `?? fallback` mirrors the column default, so a workspace read from an
   *  API that predates the attribute reads as what it actually does. The span
   *  carries the testid because `WsField` puts its own `<id>-value` on the
   *  `<dd>`, and the e2e specs assert the Enabled / Disabled text by the
   *  shorter name these controls have always had. */
  const toggleDisplay = (attr: string, fallback: boolean): ReactNode => (
    <span data-testid={FIELD_BY_ATTR[attr]?.id ?? attr}>
      {Boolean(settingsValues[attr] ?? fallback) ? t('common.enabled') : t('common.disabled')}
    </span>
  )

  /** The Enabled/Disabled toggle for a card that PATCHes on change.
   *
   *  The same two load-bearing details as `settingsToggle` — the wrapping
   *  label as the 44px tap target, and the explicit `aria-label` so the name
   *  does not become "<field> Enabled" and change as it toggles. */
  const instantToggle = (attr: string, ctx: GroupCtx, checked: boolean): ReactNode => {
    const id = FIELD_BY_ATTR[attr]?.id ?? attr
    return (
      <label className="mt-1 flex items-center gap-2 min-h-11 sm:min-h-0">
        <input
          id={id}
          data-testid={id}
          type="checkbox"
          checked={checked}
          aria-label={fieldLabel(attr)}
          disabled={ctx.busy}
          onChange={(e) => ctx.onChange(attr, e.target.checked)}
          className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
        />
        <span className="text-sm text-slate-200">
          {checked ? t('common.enabled') : t('common.disabled')}
        </span>
      </label>
    )
  }

  /** The three-way AI mode words, shared by the summary and the policy gate. */
  const aiModeLabel = (v: string | null | undefined): string =>
    v === 'enabled'
      ? t('aiSummary.modeEnabled')
      : v === 'disabled'
        ? t('aiSummary.modeDisabled')
        : t('aiSummary.modeDefault')

  /** The drift card. Only the INTERVAL is a field here: the enable toggle, the
   *  dismiss and `Check now` are actions and stay exactly what they were. */
  const driftCtx: GroupCtx = instantCtx(
    (_attr, v) => handleDriftIntervalChange(Number(v)),
    savingDrift,
  )

  const planExpiryCtx: GroupCtx = instantCtx(
    (_attr, v) => handlePlanExpiryChange(Number(v)),
    savingPlanExpiry,
  )

  const aiCtx: GroupCtx = {
    ...instantCtx(
      (attr, v) => handleAiSummaryAttrUpdate({ [attr]: v }),
      savingAiSummary,
      (attr) =>
        attr === 'ai-policy-mode' ? (
          // Says plainly what `disabled` does NOT do. A workspace admin
          // setting it and believing they have opted out of a fleet-wide
          // blocking control is the misunderstanding this line prevents.
          <p className="mt-1 text-xs text-slate-500">{t('aiSummary.policyModeNote')}</p>
        ) : null,
    ),
    // Both columns are nullable and `default` is what NULL means, so a select
    // needs a concrete value to match one of its options against.
    values: {
      ...storedValues,
      'ai-summary-mode': attrs['ai-summary-mode'] || 'default',
      'ai-policy-mode': attrs['ai-policy-mode'] || 'default',
    },
  }

  const scanCtx: GroupCtx = {
    ...instantCtx((attr, v) => handleSecurityScanAttrUpdate({ [attr]: v }), savingSecurityScan),
    values: {
      ...storedValues,
      'security-scan-enforcement': attrs['security-scan-enforcement'] || 'advisory',
      'security-scan-engine': attrs['security-scan-engine'] || 'checkov',
      'security-scan-severity-threshold': attrs['security-scan-severity-threshold'] || 'high',
    },
  }

  const debugCtx: GroupCtx = instantCtx(
    (_attr, v) => handleDebugModeUpdate(Boolean(v)),
    savingDebugMode,
    () => savingHint('debugMode.hint', savingDebugMode),
  )

  const forkCtx: GroupCtx = {
    ...instantCtx(
      (_attr, v) => handleForkPlansUpdate(Boolean(v)),
      savingForkPlans,
      () => savingHint('allowForkPrPlans.hint', savingForkPlans),
    ),
    // See the card below: the attribute is declared a dependent of
    // `vcs-connection-id` for the create form's sake, and this surface shows
    // the switch regardless.
    // `true` rather than a fake id: `dependencyMet` takes a boolean gate at
    // face value, and nothing here can mistake it for a connection.
    values: { ...storedValues, 'vcs-connection-id': true },
  }

  const slackCtx: GroupCtx = instantCtx(
    (_attr, v) => handleSlackChannelUpdate(String(v).trim()),
    savingSlackChannel,
    () => savingHint('slack.hint', savingSlackChannel),
  )

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={attrs.name}
          description={t('header.executionMode', { mode: attrs['execution-mode'] })}
          actions={
            <div className="flex items-center gap-3">
              <a
                href={`/workspaces/${workspaceId}/onboarding`}
                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 whitespace-nowrap"
              >
                {t('header.onboard')}
              </a>
              <ConnectionStatus connected={sseConnected} />
            </div>
          }
        />

        {error && <ErrorBanner message={error} />}

        {attrs['lifecycle-state'] === 'pending_deletion' && (
          <div className="mb-4 p-4 rounded-lg bg-amber-900/30 border border-amber-700/50">
            <p className="text-sm font-semibold text-amber-300">{t('lifecycle.pendingDeletion')}</p>
            <p className="text-sm text-amber-200/80 mt-1">
              {attrs['lifecycle-reason'] || t('lifecycle.pendingDeletionDefault')}
            </p>
          </div>
        )}

        {attrs['lifecycle-state'] === 'archived' && (
          <div className="mb-4 p-4 rounded-lg bg-slate-800/60 border border-slate-600/50">
            <p className="text-sm font-semibold text-slate-300">{t('lifecycle.archived')}</p>
            <p className="text-sm text-slate-400 mt-1">
              {attrs['lifecycle-reason'] || t('lifecycle.archivedDefault')}
            </p>
          </div>
        )}

        {/* Debug mode is a standing exposure, not a per-run one, so it is said
            here rather than only on the Configuration tab (#1764). */}
        {attrs['debug-mode'] && (
          <div
            data-testid="debug-mode-banner"
            className="mb-4 p-4 rounded-lg bg-amber-900/30 border border-amber-700/50"
          >
            <p className="text-sm font-semibold text-amber-300">{t('debugMode.bannerTitle')}</p>
            <p className="text-sm text-amber-200/80 mt-1">{t('debugMode.bannerBody')}</p>
          </div>
        )}

        {lastQueuedRunId && (
          <div className="mb-4 p-3 bg-brand-900/30 rounded-lg border border-brand-700/50 flex items-center justify-between">
            <p className="text-sm text-brand-300">
              {t('runQueuedBanner.message')}
            </p>
            <button
              onClick={() => { setLastQueuedRunId(null); router.push(`/workspaces/${workspaceId}/runs/${lastQueuedRunId}`) }}
              className="text-sm font-medium text-brand-400 hover:text-brand-300 transition-colors"
            >
              {t('runQueuedBanner.viewRun')} &rarr;
            </button>
          </div>
        )}

        {/* Tabs. Nine sections overflow a phone-width strip, so below md they
            collapse to a native <select> picker (same pattern as the run page);
            the tab bar returns at md+. One source (`tabs`), two viewport-driven
            presentations; the URL (?tab=) stays the source of truth either way. */}
        <div className="mb-6 md:hidden">
          <label htmlFor="ws-tab-select" className="sr-only">
            {t('tabs.selectLabel')}
          </label>
          <select
            id="ws-tab-select"
            value={activeGroup.key}
            onChange={(e) => setActiveTab(e.target.value as Tab)}
            className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2.5 text-sm font-medium text-slate-100 focus:border-brand-500 focus:outline-none"
          >
            {tabGroups.map((g) => (
              <option key={g.key} value={g.key}>
                {groupLabel(g)}
              </option>
            ))}
          </select>
        </div>
        <div className="hidden border-b border-slate-700/50 mb-6 md:block overflow-x-auto">
          <div className="flex gap-1 -mb-px whitespace-nowrap">
            {tabGroups.map((g) => (
              <button
                key={g.key}
                onClick={() => setActiveTab(g.key)}
                className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                  activeGroup.key === g.key
                    ? 'border-brand-500 text-brand-400'
                    : 'border-transparent text-slate-400 hover:text-slate-200 hover:border-slate-600'
                }`}
              >
                {groupLabel(g)}
              </button>
            ))}
          </div>
        </div>

        {/* Sub-view toggle — shown only for a merged group (State / Insights /
            Automation). A segmented control switching the ?tab= within the group. */}
        {activeGroup.members.length > 1 && (
          <div className="mb-6 flex flex-wrap gap-1 rounded-lg bg-slate-800/60 p-1 w-fit">
            {activeGroup.members.map((m) => (
              <button
                key={m}
                onClick={() => setActiveTab(m)}
                className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                  activeTab === m
                    ? 'bg-slate-700 text-slate-100'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {subTabLabel(m)}
              </button>
            ))}
          </div>
        )}

        {/* Configuration Tab (workspace settings) */}
        {activeTab === 'configuration' && (
          <div className="space-y-6">
            <HealthConditions conditions={attrs['health-conditions'] || []} />
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-sm font-medium text-slate-300">{t('settings.title')}</h3>
                {!editing ? (
                  perms['can-update'] && <button onClick={startEditing} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200">
                    {t('actions.edit')}
                  </button>
                ) : (
                  <div className="flex gap-2">
                    <button
                      onClick={() => setEditing(false)}
                      className="px-4 py-2 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors"
                    >
                      {t('actions.cancel')}
                    </button>
                    <button
                      onClick={() => handleSave()}
                      // A workspace with neither operation is refused by the
                      // server (#1966), so the save is blocked here rather than
                      // sent to fail. The banner below says why.
                      disabled={saving || editHasNoOperations}
                      className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
                    >
                      {saving ? t('actions.saving') : t('settings.saveChanges')}
                    </button>
                  </div>
                )}
              </div>
              {nameChanged && (
                <div className="mb-4 rounded-lg border border-blue-500/50 bg-blue-500/10 p-3 text-sm text-blue-300">
                  {t.rich('settings.renamedNotice', {
                    name: () => <code className="bg-slate-700 px-1 rounded">name</code>,
                    cloud: () => <code className="bg-slate-700 px-1 rounded">cloud {'{'}{'}' }</code>,
                  })}
                </div>
              )}
              {/* Nine groups rather than one flat list of thirty-two controls
                  (#2253). The field set, the grouping and the markup are all
                  shared with the create form; what stays here is the save —
                  everything below writes into the `edit*` buffer and lands on
                  one `Save changes`. */}
              <div className="space-y-6 min-w-0">
                <Group id="identity" ctx={settingsCtx} title={t('groups.identity')}>
                  <Field
                    attr="name"
                    ctx={settingsCtx}
                    kind="text"
                    display={attrs.name}
                    control={
                      <input
                        id="ws-name"
                        data-testid="ws-name"
                        type="text"
                        value={editName}
                        onChange={(e) => setEditName(e.target.value)}
                        // A Pulumi workspace is named project::stack, which the plain rule rejects.
                        pattern={isPulumi ? '[a-zA-Z0-9][a-zA-Z0-9_\\-]*::[a-zA-Z0-9][a-zA-Z0-9_\\-]*' : '[a-zA-Z0-9][a-zA-Z0-9_\\-]*'}
                        maxLength={90}
                        title={isPulumi ? t('fields.nameTitlePulumi') : t('fields.nameTitle')}
                        className={EDIT_INPUT}
                      />
                    }
                  />
                  {/* Identity, and immutable after create — so it is shown
                      read-only even mid-edit. Absent on Terraform, where it is
                      the default and naming it would be noise. */}
                  {attrs.engine && attrs.engine !== 'terraform' && (
                    <Field
                      attr="engine"
                      ctx={settingsViewCtx}
                      kind="text"
                      display={
                        <span data-testid="ws-engine">
                          {isPulumi ? t('fields.enginePulumi') : attrs.engine}
                        </span>
                      }
                    />
                  )}
                </Group>

                <Group id="execution" ctx={settingsCtx} title={t('groups.execution')}>
                  <Field
                    attr="execution-mode"
                    ctx={settingsCtx}
                    kind="select"
                    display={attrs['execution-mode']}
                    options={[
                      { value: 'local', label: t('fields.execModeLocal') },
                      { value: 'agent', label: t('fields.execModeAgent') },
                    ]}
                  />
                  <Field
                    attr="auto-apply-mode"
                    ctx={settingsCtx}
                    kind="select"
                    display={tMode(
                      autoApplyModeKey(
                        attrs['auto-apply-mode'] || (attrs['auto-apply'] ? 'always' : 'never'),
                      ),
                    )}
                    // Values are the API enum; the labels are translated.
                    options={[
                      { value: 'never', label: tMode('never') },
                      { value: 'always', label: tMode('always') },
                      { value: 'create', label: tMode('create') },
                      { value: 'create_update', label: tMode('createUpdate') },
                    ]}
                  />
                  <Field
                    attr="resource-cpu"
                    ctx={settingsCtx}
                    kind="text"
                    display={attrs['resource-cpu']}
                    control={
                      <input
                        id="ws-cpu"
                        data-testid="ws-cpu"
                        type="text"
                        value={editCpu}
                        onChange={(e) => setEditCpu(e.target.value)}
                        pattern="[0-9]+m|[0-9]+(\.[0-9]+)?"
                        title={t('fields.cpuRequestTitle')}
                        className={EDIT_INPUT}
                      />
                    }
                  />
                  <Field
                    attr="resource-memory"
                    ctx={settingsCtx}
                    kind="text"
                    display={attrs['resource-memory']}
                    control={
                      <input
                        id="ws-mem"
                        data-testid="ws-mem"
                        type="text"
                        value={editMemory}
                        onChange={(e) => setEditMemory(e.target.value)}
                        pattern="[0-9]+(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E|m)?"
                        title={t('fields.memoryRequestTitle')}
                        className={EDIT_INPUT}
                      />
                    }
                  />
                  <Field
                    attr="parallelism"
                    ctx={settingsCtx}
                    kind="number"
                    display={attrs.parallelism ?? 10}
                    control={
                      <input
                        id="ws-parallelism"
                        data-testid="ws-parallelism"
                        type="number"
                        min={1}
                        max={256}
                        value={editParallelism}
                        onChange={(e) => setEditParallelism(e.target.value)}
                        title={t('fields.parallelismTitle')}
                        className={EDIT_INPUT}
                      />
                    }
                  />
                  {/* Terraform-only: the execution backend picks the binary
                      *within* the Terraform engine, so it has no meaning on
                      Pulumi. Guarded here rather than by `engines` in the
                      shared table, which does not carry one for it. */}
                  {!isPulumi && (
                    <Field
                      attr="execution-backend"
                      ctx={settingsCtx}
                      kind="select"
                      display={attrs['execution-backend'] === 'terraform' ? 'Terraform' : 'OpenTofu'}
                      options={[
                        { value: 'tofu', label: 'OpenTofu' },
                        { value: 'terraform', label: 'Terraform' },
                      ]}
                    />
                  )}
                  {/* Every engine pins a version — since #1559 a Pulumi
                      workspace's is the Pulumi CLI its runs use — so this is
                      shown on all of them, with suggestions keyed on the engine. */}
                  <Field
                    attr="engine-version"
                    ctx={settingsCtx}
                    kind="text"
                    display={attrs['terraform-version'] || t('common.default')}
                    control={
                      <>
                        <input
                          id="ws-version"
                          data-testid="ws-version"
                          type="text"
                          list="edit-version-suggestions"
                          value={editVersion}
                          onChange={(e) => setEditVersion(e.target.value)}
                          placeholder={t('fields.versionPlaceholder')}
                          pattern="[0-9]+\.[0-9]+(\.[0-9]+)?"
                          title={t('fields.versionTitle')}
                          className={EDIT_INPUT}
                        />
                        <datalist id="edit-version-suggestions">
                          {versionSuggestions.map((v) => (
                            <option key={v} value={v} />
                          ))}
                        </datalist>
                      </>
                    }
                  />
                  {/* Hand-written rather than two `<Field>`s, and the one place
                      in this card that is: `LABEL_KEY` points both terragrunt
                      attributes at Enabled/Disabled STATE strings, and the
                      version one carries an unsatisfied `{version}` ICU
                      argument that would render as a raw key. One composite
                      control is also how it has always read — the version only
                      means anything once the checkbox is ticked. */}
                  {!isPulumi && (
                    <div className="min-w-0">
                      <dt className="text-xs text-slate-500">Terragrunt</dt>
                      {settingsEditing ? (
                        <div className="mt-1 space-y-2">
                          <label className="flex items-center gap-2 min-h-11 sm:min-h-0">
                            <input
                              type="checkbox"
                              checked={editTerragruntEnabled}
                              aria-label="Terragrunt" /* i18n-ignore: a product name, like the <dt> above it */
                              onChange={(e) => setEditTerragruntEnabled(e.target.checked)}
                              className="rounded border-slate-600 bg-slate-700 text-brand-600"
                            />
                            <span className="text-sm text-slate-200">
                              {editTerragruntEnabled
                                ? t('fields.terragruntEnabledAgent')
                                : t('common.disabled')}
                            </span>
                          </label>
                          {editTerragruntEnabled && (
                            <input
                              type="text"
                              value={editTerragruntVersion}
                              onChange={(e) => setEditTerragruntVersion(e.target.value)}
                              placeholder={t('fields.terragruntVersionPlaceholder')}
                              pattern="[0-9]+\.[0-9]+(\.[0-9]+)?"
                              title={t('fields.terragruntVersionTitle')}
                              className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                            />
                          )}
                        </div>
                      ) : (
                        <dd className="mt-1 text-sm text-slate-200">
                          {attrs['terragrunt-enabled']
                            ? t('fields.terragruntEnabledVersion', {
                                version: attrs['terragrunt-version'] || '1.0',
                              })
                            : t('common.disabled')}
                        </dd>
                      )}
                    </div>
                  )}
                  <Field
                    attr="working-directory"
                    ctx={settingsCtx}
                    kind="text"
                    display={attrs['working-directory'] || '/'}
                    placeholder={t('fields.workingDirectoryPlaceholder')}
                  />
                  {/* Full width, for the help paragraph's sake. */}
                  {isPulumi && (
                    <div className="sm:col-span-2 min-w-0">
                      <dt className="text-xs text-slate-500">{t('fields.bindPlan')}</dt>
                      {settingsEditing ? (
                        <dd className="mt-1">{settingsToggle('pulumi-bind-plan')}</dd>
                      ) : (
                        <dd className="mt-1 text-sm text-slate-200" data-testid="ws-bind-plan-value">
                          {attrs['pulumi-bind-plan'] ? t('common.enabled') : t('common.disabled')}
                        </dd>
                      )}
                      <p className="mt-1 text-xs text-slate-400">
                        {t.rich('fields.bindPlanHelp', {
                          docs: (chunks) => (
                            <a href="https://github.com/mattrobinsonsre/terrapod/blob/main/docs/pulumi-cli-surface.md" target="_blank" rel="noopener noreferrer" className="text-brand-400 hover:text-brand-300 underline">{chunks}</a>
                          ),
                        })}
                      </p>
                    </div>
                  )}
                </Group>

                {/* Which operations this workspace HAS (#1966). Sat
                    immediately above the Ansible group because
                    `configure-enabled` is what brings that group's whole
                    subject — and the Inventory tab — into existence. Reading
                    them the other way round is what left the config directory
                    looking like the declaration, which is the shape #1929
                    shipped and #1966 undid.

                    Two checkboxes, not one select or a radio group: the two are
                    independent, all four combinations are meaningful except
                    both-false, and anything that collapsed them into a single
                    control would have to invent a state the columns do not
                    have.

                    The three TRIGGERS below them (#1974) are dependents of
                    `configure-enabled`, so the shared table hides them while it
                    is off — offering a trigger for an operation the workspace
                    does not have would be three controls that do nothing. They
                    read the edit buffer, so ticking Configure reveals them
                    immediately rather than after a save. */}
                <Group
                  id="operations"
                  ctx={settingsCtx}
                  title={t('groups.operations')}
                  description={
                    <p className="text-xs text-slate-500 mb-3">{t('fields.operationsHint')}</p>
                  }
                >
                  <Field
                    attr="plan-apply-enabled"
                    ctx={settingsCtx}
                    kind="checkbox"
                    display={toggleDisplay('plan-apply-enabled', true)}
                    control={settingsToggle('plan-apply-enabled')}
                  />
                  <Field
                    attr="configure-enabled"
                    ctx={settingsCtx}
                    kind="checkbox"
                    display={toggleDisplay('configure-enabled', false)}
                    control={settingsToggle('configure-enabled')}
                  />
                  <Field
                    attr="auto-configure-enabled"
                    ctx={settingsCtx}
                    kind="checkbox"
                    display={toggleDisplay('auto-configure-enabled', false)}
                    control={settingsToggle('auto-configure-enabled')}
                  />
                  <Field
                    attr="configure-after-apply"
                    ctx={settingsCtx}
                    kind="checkbox"
                    display={toggleDisplay('configure-after-apply', false)}
                    control={settingsToggle('configure-after-apply')}
                  />
                  <Field
                    attr="configure-all-hosts"
                    ctx={settingsCtx}
                    kind="checkbox"
                    display={toggleDisplay('configure-all-hosts', false)}
                    control={settingsToggle('configure-all-hosts')}
                  />
                  {settingsEditing && editHasNoOperations && (
                    // The server refuses this pair (#1966); Save is disabled
                    // above, and this is the sentence that says why.
                    <div className="sm:col-span-2 min-w-0">
                      <p
                        className="mt-1 rounded-lg border border-red-500/50 bg-red-500/10 p-2 text-xs text-red-300"
                        data-testid="ws-operations-none"
                      >
                        {t('fields.operationsNone')}
                      </p>
                    </div>
                  )}
                </Group>

                {/* Where the configure operation runs, and what it does by
                    default (#1929). The whole group depends on
                    `configure-enabled`, so it is absent entirely until the
                    operation is declared. `inventory_settings` and its own VCS
                    binding are gone — the inventory now uses the workspace's
                    `vcs-*` attributes edited in the Version control group. */}
                <Group
                  id="ansible"
                  ctx={ansibleCtx}
                  title={t('groups.ansible')}
                  description={
                    <p className="text-xs text-slate-500 mb-3">{t('fields.ansibleConfigureHint')}</p>
                  }
                >
                  <Field
                    attr="config-directory"
                    ctx={ansibleCtx}
                    kind="text"
                    // TWO states, not three. The absent one went with the
                    // nullable column (#1966), so an empty value is the
                    // configuration version's root and says so — rendering it
                    // as `/` the way `working-directory` does would be true but
                    // less legible for a path whose being empty is a choice.
                    display={
                      <span data-testid="ws-config-directory">
                        {attrs['config-directory'] ? (
                          <code className="bg-slate-700 px-2 py-0.5 rounded text-xs">
                            {attrs['config-directory']}
                          </code>
                        ) : (
                          t('fields.configDirectoryRoot')
                        )}
                      </span>
                    }
                    control={
                      <input
                        id="ws-config-dir"
                        data-testid="ws-config-directory-input"
                        type="text"
                        value={editConfigDir}
                        onChange={(e) => setEditConfigDir(e.target.value)}
                        placeholder={t('fields.configDirectoryPlaceholder')}
                        title={t('fields.configDirectoryTitle')}
                        className="mt-1 w-full min-h-11 sm:min-h-0 px-2 py-1 text-base sm:text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                      />
                    }
                  />
                  <Field
                    attr="default-playbook"
                    ctx={ansibleCtx}
                    kind="text"
                    display={
                      attrs['default-playbook'] ? (
                        <code className="bg-slate-700 px-2 py-0.5 rounded text-xs">
                          {attrs['default-playbook']}
                        </code>
                      ) : (
                        <span className="text-slate-500">{t('common.none')}</span>
                      )
                    }
                    control={
                      <input
                        id="ws-default-playbook"
                        data-testid="ws-default-playbook"
                        type="text"
                        value={editDefaultPlaybook}
                        onChange={(e) => setEditDefaultPlaybook(e.target.value)}
                        placeholder={t('fields.defaultPlaybookPlaceholder')}
                        title={t('fields.defaultPlaybookTitle')}
                        className="mt-1 w-full min-h-11 sm:min-h-0 px-2 py-1 text-base sm:text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                      />
                    }
                  />
                  <Field
                    attr="default-limit"
                    ctx={ansibleCtx}
                    kind="text"
                    display={
                      attrs['default-limit'] ? (
                        <code className="bg-slate-700 px-2 py-0.5 rounded text-xs">
                          {attrs['default-limit']}
                        </code>
                      ) : (
                        <span className="text-slate-500">{t('common.none')}</span>
                      )
                    }
                    control={
                      <input
                        id="ws-default-limit"
                        data-testid="ws-default-limit"
                        type="text"
                        value={editDefaultLimit}
                        onChange={(e) => setEditDefaultLimit(e.target.value)}
                        placeholder={t('fields.defaultLimitPlaceholder')}
                        title={t('fields.defaultLimitTitle')}
                        className="mt-1 w-full min-h-11 sm:min-h-0 px-2 py-1 text-base sm:text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                      />
                    }
                  />
                  <div className="sm:col-span-2 min-w-0">
                    <dt className="text-xs text-slate-500">{t('fields.inventoryPaths')}</dt>
                    {settingsEditing ? (
                      <dd className="mt-1 space-y-2">
                        <p className="text-xs text-slate-400">{t('fields.inventoryPathsHint')}</p>
                        {/* The shared editor rather than this page's older
                            inline list: its row actions are real buttons with a
                            background and a 44px tap target, which bare
                            coloured text is not. */}
                        <StringListEditor
                          values={editInventoryPaths}
                          onChange={setEditInventoryPaths}
                          placeholder={t('fields.inventoryPathsPlaceholder')}
                          addLabel={t('fields.inventoryPathsAdd')}
                        />
                      </dd>
                    ) : (
                      // An ORDERED list, because the order is the precedence
                      // order — a later source wins per variable — so a read
                      // that lost it would misreport the merge.
                      <dd className="mt-1 text-sm text-slate-200">
                        {(attrs['inventory-paths'] || []).length > 0 ? (
                          <ol className="flex flex-col gap-1">
                            {(attrs['inventory-paths'] || []).map((path) => (
                              <li key={path}>
                                <code className="bg-slate-700 px-2 py-0.5 rounded text-xs">
                                  {path}
                                </code>
                              </li>
                            ))}
                          </ol>
                        ) : (
                          <span className="text-slate-500">{t('common.none')}</span>
                        )}
                      </dd>
                    )}
                  </div>
                  {/* Which inventory sources ansible may resolve on the
                      runner (#2033). Terrapod itself never runs either kind,
                      in either state -- off means a plugin source is refused
                      by name rather than resolving to a partial host set. */}
                  <Field
                    attr="dynamic-inventory-enabled"
                    ctx={ansibleCtx}
                    kind="checkbox"
                    display={toggleDisplay('dynamic-inventory-enabled', false)}
                    control={settingsToggle('dynamic-inventory-enabled')}
                  />
                  <Field
                    attr="inventory-scripts-enabled"
                    ctx={ansibleCtx}
                    kind="checkbox"
                    display={toggleDisplay('inventory-scripts-enabled', false)}
                    control={settingsToggle('inventory-scripts-enabled')}
                  />
                </Group>

                <Group id="pools" ctx={settingsCtx} title={t('groups.pools')}>
                  <Field
                    attr="agent-pool-ids"
                    ctx={settingsCtx}
                    kind="list"
                    display={
                      <span className="flex flex-wrap gap-1">
                        {(attrs['agent-pool-ids'] || []).length === 0
                          ? t('common.none')
                          : (attrs['agent-pool-ids'] || []).map((id) => (
                              <span
                                key={id}
                                className="rounded bg-slate-700 px-2 py-0.5 text-xs text-slate-200"
                              >
                                {poolLabel(id)}
                              </span>
                            ))}
                      </span>
                    }
                    control={
                      <div className="mt-1 space-y-1">
                        {poolRows.length === 0 ? (
                          <p className="text-sm text-slate-400">{t('common.none')}</p>
                        ) : (
                          <ul className="space-y-1">
                            {poolRows.map(({ id, name, assignable }) => {
                              const selected = editPoolIds.includes(id)
                              return (
                                <li key={id}>
                                  <label
                                    className={`flex min-h-11 items-center gap-2 rounded-lg px-3 py-2 text-sm ${
                                      assignable
                                        ? 'cursor-pointer bg-slate-700/60 text-slate-100 hover:bg-slate-700'
                                        : 'cursor-not-allowed bg-slate-800/60 text-slate-400'
                                    }`}
                                    title={assignable ? undefined : t('fields.agentPoolsLocked')}
                                  >
                                    <input
                                      type="checkbox"
                                      checked={selected}
                                      disabled={!assignable}
                                      onChange={() =>
                                        setEditPoolIds((prev) =>
                                          prev.includes(id)
                                            ? prev.filter((existing) => existing !== id)
                                            : // Append, so the stored order is
                                              // the order the operator picked.
                                              [...prev, id]
                                        )
                                      }
                                      className="h-4 w-4 rounded border-slate-500 bg-slate-800 text-brand-500 focus:ring-1 focus:ring-brand-500"
                                    />
                                    <span>{name}</span>
                                    {!assignable && (
                                      <span className="ms-auto text-xs">
                                        {t('fields.agentPoolsLocked')}
                                      </span>
                                    )}
                                  </label>
                                </li>
                              )
                            })}
                          </ul>
                        )}
                      </div>
                    }
                  />
                </Group>

                <Group id="metadata" ctx={settingsCtx} title={t('groups.metadata')}>
                  {/* Platform-admin only: ownership grants workspace admin, so
                      anyone else sees the value and cannot move it. */}
                  <Field
                    attr="owner-email"
                    ctx={isAdmin() ? settingsCtx : settingsViewCtx}
                    kind="text"
                    display={attrs['owner-email'] || t('common.none')}
                    control={
                      <input
                        id="ws-owner"
                        data-testid="ws-owner"
                        type="email"
                        value={editOwner}
                        onChange={(e) => setEditOwner(e.target.value)}
                        placeholder="user@example.com"
                        className={EDIT_INPUT}
                      />
                    }
                  />
                  <div className="sm:col-span-2 min-w-0">
                    <dt className="text-xs text-slate-500 mb-1" title={t('fields.labelsTitle')}>
                      {t('fields.labels')}
                    </dt>
                    {settingsEditing ? (
                      <dd className="mt-1">
                        <LabelsEditor labels={editLabels} onChange={setEditLabels} />
                      </dd>
                    ) : (
                      <dd className="mt-1">
                        <LabelsEditor labels={attrs.labels || {}} readOnly />
                      </dd>
                    )}
                  </div>
                  {!isPulumi && (
                    <div className="sm:col-span-2 min-w-0">
                      <dt className="text-xs text-slate-500 mb-1">{t('fields.varFiles')}</dt>
                      {settingsEditing ? (
                        <dd className="space-y-2">
                          {editVarFiles.map((f, i) => (
                            <div key={f} className="flex items-center gap-2">
                              <code className="text-sm text-slate-200 bg-slate-700 px-2 py-0.5 rounded flex-1 truncate">{f}</code>
                              <button
                                onClick={() => setEditVarFiles(editVarFiles.filter((_, j) => j !== i))}
                                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300"
                              >{t('actions.remove')}</button>
                            </div>
                          ))}
                          <div className="flex items-center gap-2">
                            <input
                              type="text"
                              value={newVarFile}
                              onChange={(e) => setNewVarFile(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter' && newVarFile.trim()) {
                                  e.preventDefault()
                                  const v = newVarFile.trim()
                                  if (!editVarFiles.includes(v)) {
                                    setEditVarFiles([...editVarFiles, v])
                                  }
                                  setNewVarFile('')
                                }
                              }}
                              placeholder={t('fields.varFilesPlaceholder')}
                              className="flex-1 px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                            />
                            <button
                              onClick={() => {
                                if (newVarFile.trim()) {
                                  const v = newVarFile.trim()
                                  if (!editVarFiles.includes(v)) {
                                    setEditVarFiles([...editVarFiles, v])
                                  }
                                  setNewVarFile('')
                                }
                              }}
                              className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200"
                            >{t('actions.add')}</button>
                          </div>
                        </dd>
                      ) : (
                        <dd className="mt-1 text-sm text-slate-200">
                          {(attrs['var-files'] || []).length > 0 ? (
                            <div className="flex flex-wrap gap-1">
                              {attrs['var-files'].map((f) => (
                                <code key={f} className="bg-slate-700 px-2 py-0.5 rounded text-xs">{f}</code>
                              ))}
                            </div>
                          ) : (
                            <span className="text-slate-500">{t('common.none')}</span>
                          )}
                        </dd>
                      )}
                    </div>
                  )}
                </Group>

                {/* The four fields below `vcs-connection-id` are its
                    DEPENDENTS, so with no connection they are absent rather
                    than rendered against a gate nobody set (#2253). The
                    connection selector itself is never hidden — hiding a
                    dependent removes something meaningless, while hiding the
                    gate removes the only way to reach the feature. */}
                <Group id="vcs" ctx={settingsCtx} title={t('groups.vcs')}>
                  <Field
                    attr="vcs-connection-id"
                    ctx={settingsCtx}
                    kind="select"
                    display={attrs['vcs-connection-name'] || t('common.none')}
                    options={[
                      { value: '', label: t('common.none') },
                      ...vcsConnections.map((c) => ({
                        value: c.id,
                        label: `${c.attributes.name} (${c.attributes.provider})`,
                      })),
                    ]}
                  />
                  <Field
                    attr="vcs-repo-url"
                    ctx={settingsCtx}
                    kind="text"
                    display={
                      attrs['vcs-repo-url'] ? (
                        <a href={attrs['vcs-repo-url']} target="_blank" rel="noopener noreferrer" className="text-brand-400 hover:text-brand-300">{attrs['vcs-repo-url']}</a>
                      ) : (
                        t('common.none')
                      )
                    }
                    control={
                      <input
                        id="ws-vcs-repo"
                        data-testid="ws-vcs-repo"
                        type="text"
                        value={editVcsRepoUrl}
                        onChange={(e) => setEditVcsRepoUrl(e.target.value)}
                        placeholder="https://github.com/org/repo"
                        pattern="https?://.+"
                        title={t('fields.vcsRepositoryTitle')}
                        className={EDIT_INPUT}
                      />
                    }
                  />
                  <Field
                    attr="vcs-branch"
                    ctx={settingsCtx}
                    kind="text"
                    display={attrs['vcs-branch'] || t('common.default')}
                    placeholder={t('fields.vcsBranchPlaceholder')}
                  />
                  <Field
                    attr="vcs-workflow"
                    ctx={settingsCtx}
                    kind="select"
                    display={attrs['vcs-workflow']}
                    options={[
                      { value: 'merge_then_apply', label: t('fields.vcsWorkflowMergeThenApply') },
                      { value: 'apply_then_merge', label: t('fields.vcsWorkflowApplyThenMerge') },
                    ]}
                  />
                  {settingsEditing && editVcsWorkflow === 'apply_then_merge' && (
                    <div className="sm:col-span-2 min-w-0 rounded border border-amber-700 bg-amber-900/30 p-3 text-xs text-amber-100">
                      <p className="font-medium">{t('vcsWorkflowWarning.title')}</p>
                      <p className="mt-1">
                        {tPhase.rich(phaseKeyFor('vcsWorkflowWarningRbac'), {
                          em: (chunks) => <em> {chunks}</em>,
                        })}
                      </p>
                      <p className="mt-1">
                        {tPhase.rich(phaseKeyFor('vcsWorkflowWarningRecommended'), {
                          strong: (chunks) => <strong>{chunks}</strong>,
                        })}
                      </p>
                      <p className="mt-1">
                        {t.rich('vcsWorkflowWarning.credit', {
                          atlantis: (chunks) => (
                            <a href="https://www.runatlantis.io/" target="_blank" rel="noopener noreferrer" className="underline">{chunks}</a>
                          ),
                        })}
                      </p>
                    </div>
                  )}
                  <Field
                    attr="auto-merge"
                    ctx={settingsCtx}
                    kind="checkbox"
                    display={toggleDisplay('auto-merge', false)}
                    control={settingsToggle('auto-merge')}
                  />
                  <Field
                    attr="auto-merge-strategy"
                    ctx={settingsCtx}
                    kind="select"
                    display={attrs['auto-merge-strategy']}
                    // Wire values, so they are not translated.
                    options={[
                      { value: 'merge', label: 'merge' },
                      { value: 'squash', label: 'squash' },
                      { value: 'rebase', label: 'rebase' },
                    ]}
                  />
                  {attrs['vcs-connection-name'] && !settingsEditing && (
                    <div className="min-w-0">
                      <dt className="text-xs text-slate-500">{t('fields.vcsPolling')}</dt>
                      <dd className="mt-1 text-sm">
                        {attrs['vcs-last-error'] ? (
                          <span className="text-red-400" title={attrs['vcs-last-error']}>{t('vcsPolling.error')}{attrs['vcs-last-error-at'] ? ` (${new Date(attrs['vcs-last-error-at']).toLocaleString()})` : ''}</span>
                        ) : vcsPollStalled ? (
                          // No error recorded, but the newest ATTEMPT is newer
                          // than the newest SUCCESS — the polls are failing
                          // without leaving a message. Reporting that as green
                          // was the misleading half of #1089.
                          <span className="text-amber-400">
                            {t('vcsPolling.stalled', {
                              attempted: new Date(attrs['vcs-last-attempted-at']!).toLocaleString(),
                              succeeded: attrs['vcs-last-polled-at']
                                ? new Date(attrs['vcs-last-polled-at']).toLocaleString()
                                : t('vcsPolling.never'),
                            })}
                          </span>
                        ) : attrs['vcs-last-polled-at'] ? (
                          <span className="text-green-400">{t('vcsPolling.ok', { time: new Date(attrs['vcs-last-polled-at']).toLocaleString() })}</span>
                        ) : (
                          <span className="text-slate-400">{t('vcsPolling.notPolled')}</span>
                        )}
                      </dd>
                    </div>
                  )}
                  {settingsShows('trigger-prefixes') && (
                    <div className="sm:col-span-2 min-w-0">
                      <dt className="text-xs text-slate-500 mb-1">{t('fields.triggerPrefixes')}</dt>
                      {settingsEditing ? (
                        <dd className="space-y-2">
                          <p className="text-xs text-slate-400">{t('fields.triggerPrefixesHint')}</p>
                          {editTriggerPrefixes.map((f, i) => (
                            <div key={f} className="flex items-center gap-2">
                              <code className="text-sm text-slate-200 bg-slate-700 px-2 py-0.5 rounded flex-1 truncate">{f}</code>
                              <button
                                onClick={() => setEditTriggerPrefixes(editTriggerPrefixes.filter((_, j) => j !== i))}
                                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300"
                              >{t('actions.remove')}</button>
                            </div>
                          ))}
                          <div className="flex items-center gap-2">
                            <input
                              type="text"
                              value={newTriggerPrefix}
                              onChange={(e) => setNewTriggerPrefix(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter' && newTriggerPrefix.trim()) {
                                  e.preventDefault()
                                  const v = newTriggerPrefix.trim().replace(/^\/|\/$/g, '')
                                  if (v && !editTriggerPrefixes.includes(v)) {
                                    setEditTriggerPrefixes([...editTriggerPrefixes, v])
                                  }
                                  setNewTriggerPrefix('')
                                }
                              }}
                              placeholder={t('fields.triggerPrefixesPlaceholder')}
                              className="flex-1 px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                            />
                            <button
                              onClick={() => {
                                if (newTriggerPrefix.trim()) {
                                  const v = newTriggerPrefix.trim().replace(/^\/|\/$/g, '')
                                  if (v && !editTriggerPrefixes.includes(v)) {
                                    setEditTriggerPrefixes([...editTriggerPrefixes, v])
                                  }
                                  setNewTriggerPrefix('')
                                }
                              }}
                              className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200"
                            >{t('actions.add')}</button>
                          </div>
                        </dd>
                      ) : (
                        <dd className="mt-1 text-sm text-slate-200">
                          {(attrs['trigger-prefixes'] || []).length > 0 ? (
                            <div className="flex flex-wrap gap-1">
                              {attrs['trigger-prefixes'].map((f) => (
                                <code key={f} className="bg-slate-700 px-2 py-0.5 rounded text-xs">{f}</code>
                              ))}
                            </div>
                          ) : (
                            <span className="text-slate-500">{t('fields.triggerPrefixesNone')}</span>
                          )}
                        </dd>
                      )}
                    </div>
                  )}
                </Group>

                {/* Terraform's alone (#1561): the rules are globs over
                    Terraform attribute paths, and a Pulumi drift run's artifact
                    is a preview digest the classifier cannot read. The server
                    declines to apply them for Pulumi, so offering the editor
                    here would only invite rules that silently do nothing.
                    Guarded here because the shared table carries no `engines`
                    for this attribute. Defining them in URN terms is the other
                    half of #1561. */}
                {!isPulumi && (
                  <Group id="governance" ctx={settingsCtx} title={t('groups.governance')}>
                    <div className="sm:col-span-2 min-w-0">
                      <dt className="text-xs text-slate-500 mb-1">{t('fields.driftIgnoreRules')}</dt>
                      {settingsEditing ? (
                        <dd className="space-y-2">
                          <p className="text-xs text-slate-400">
                            {t.rich('fields.driftIgnoreRulesHint', {
                              star: () => <code className="text-[10px] bg-slate-800 px-1">*</code>,
                              dot: () => <code className="text-[10px] bg-slate-800 px-1">.</code>,
                              n: () => <code className="text-[10px] bg-slate-800 px-1">[N]</code>,
                              starIdx: () => <code className="text-[10px] bg-slate-800 px-1">[*]</code>,
                              docs: (chunks) => <a href="https://github.com/mattrobinsonsre/terrapod/blob/main/docs/drift-ignore-rules.md" target="_blank" rel="noreferrer" className="text-brand-400 hover:underline">{chunks}</a>,
                            })}
                          </p>
                          {editDriftIgnoreRules.map((r, i) => (
                            <div key={`${r}-${i}`} className="flex items-center gap-2">
                              <code className="text-sm text-slate-200 bg-slate-700 px-2 py-0.5 rounded flex-1 truncate">{r}</code>
                              <button
                                onClick={() => setEditDriftIgnoreRules(editDriftIgnoreRules.filter((_, j) => j !== i))}
                                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300"
                              >{t('actions.remove')}</button>
                            </div>
                          ))}
                          <div className="flex items-center gap-2">
                            <input
                              type="text"
                              value={newDriftIgnoreRule}
                              onChange={(e) => setNewDriftIgnoreRule(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter' && newDriftIgnoreRule.trim()) {
                                  e.preventDefault()
                                  const v = newDriftIgnoreRule.trim()
                                  if (v && !editDriftIgnoreRules.includes(v)) {
                                    setEditDriftIgnoreRules([...editDriftIgnoreRules, v])
                                  }
                                  setNewDriftIgnoreRule('')
                                }
                              }}
                              placeholder="e.g. module.eks*.argocd_cluster.*.config.tls_client_config.ca_data"
                              className="flex-1 px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500 font-mono"
                            />
                            <button
                              onClick={() => {
                                if (newDriftIgnoreRule.trim()) {
                                  const v = newDriftIgnoreRule.trim()
                                  if (v && !editDriftIgnoreRules.includes(v)) {
                                    setEditDriftIgnoreRules([...editDriftIgnoreRules, v])
                                  }
                                  setNewDriftIgnoreRule('')
                                }
                              }}
                              className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200"
                            >{t('actions.add')}</button>
                          </div>
                        </dd>
                      ) : (
                        <dd className="mt-1 text-sm text-slate-200">
                          {(attrs['drift-ignore-rules'] || []).length > 0 ? (
                            <div className="flex flex-col gap-1">
                              {attrs['drift-ignore-rules'].map((r) => (
                                <code key={r} className="bg-slate-700 px-2 py-0.5 rounded text-xs font-mono">{r}</code>
                              ))}
                            </div>
                          ) : (
                            <span className="text-slate-500">{t('fields.driftIgnoreRulesNone')}</span>
                          )}
                        </dd>
                      )}
                    </div>
                  </Group>
                )}

                <Group id="cloudIdentity" ctx={settingsCtx} title={t('groups.cloudIdentity')}>
                  <div className="sm:col-span-2 min-w-0">
                    <dt className="text-xs text-slate-500 mb-1">{t('fields.oidcAudiences')}</dt>
                    {settingsEditing ? (
                      <dd className="space-y-2">
                        <p className="text-xs text-slate-400">{t('fields.oidcAudiencesHint')}</p>
                        {/* `value` is the owned set and `fallbacks` the
                            catalogue for the keys in force, so an inherited
                            entry is shown and offered for override rather than
                            edited in place, and only the owned set is saved. */}
                        {editOidcAudiences === null ? (
                          <p className="text-xs text-slate-500">
                            {t('fields.oidcAudiencesLoading')}
                          </p>
                        ) : (
                          <OidcAudiencesEditor
                            value={editOidcAudiences}
                            fallbacks={oidcAudienceSplit.fallbacks}
                            onChange={setEditOidcAudiences}
                            inert={!oidcDefaults.issuerEnabled}
                            audiencePlaceholder={t('fields.oidcAudiencesPlaceholder')}
                            addAudienceLabel={t('fields.oidcAudiencesAdd')}
                          />
                        )}
                      </dd>
                    ) : (
                      <dd className="mt-1 text-sm text-slate-200" data-testid="oidc-audiences">
                        {Object.keys(attrs['oidc-audiences'] || {}).length > 0 ? (
                          <OidcAudiencesEditor
                            value={oidcAudienceSplit.owned}
                            fallbacks={oidcAudienceSplit.fallbacks}
                            readOnly
                          />
                        ) : (
                          <span className="text-slate-500">{t('fields.oidcAudiencesNone')}</span>
                        )}
                      </dd>
                    )}
                  </div>
                </Group>
              </div>
              {lockoutWarning && (
                <div className="mt-4 p-3 bg-amber-900/30 border border-amber-700/50 rounded-lg">
                  <p className="text-sm text-amber-300 mb-2">{lockoutWarning}</p>
                  <div className="flex gap-2">
                    <button
                      onClick={() => { setLockoutWarning(''); setEditLabels(attrs.labels || {}); }}
                      className="px-3 py-1 rounded text-xs text-slate-300 hover:text-white bg-slate-700 hover:bg-slate-600"
                    >
                      {t('lockout.revertLabels')}
                    </button>
                    <button
                      onClick={() => handleSave(true)}
                      disabled={saving}
                      className="px-3 py-1 rounded text-xs text-amber-200 hover:text-white bg-amber-700 hover:bg-amber-600"
                    >
                      {saving ? t('actions.savingEllipsis') : t('lockout.saveAnyway')}
                    </button>
                  </div>
                </div>
              )}
            </div>

            {/* Lock / Unlock */}
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
              <div className="flex items-center justify-between gap-4">
                <div className="min-w-0">
                  <h3 className="text-sm font-medium text-slate-300">{t('lock.title')}</h3>
                  <p className="text-sm text-slate-400 mt-1">
                    {attrs.locked ? phaseWord('lockLockedDesc') : t('lock.unlockedDesc')}
                  </p>
                  {attrs.locked && attrs['locked-by'] && (
                    <p className="text-sm text-slate-400 mt-1 break-words" data-testid="lock-holder">
                      {t('lock.lockedBy', { holder: attrs['locked-by'] })}
                    </p>
                  )}
                  {attrs.locked && attrs['lock-reason'] && (
                    <p className="text-sm text-slate-300 mt-1 break-words whitespace-pre-wrap" data-testid="lock-reason">
                      {t('lock.reason', { reason: attrs['lock-reason'] })}
                    </p>
                  )}
                </div>
                {perms['can-lock'] && (
                  <button
                    onClick={handleLockToggle}
                    className={`shrink-0 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                      attrs.locked
                        ? 'bg-amber-600 hover:bg-amber-500 text-white'
                        : 'bg-slate-600 hover:bg-slate-500 text-slate-200'
                    }`}
                  >
                    {attrs.locked ? t('lock.unlock') : t('lock.lock')}
                  </button>
                )}
              </div>
            </div>

            {/* Drift Detection */}
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-sm font-medium text-slate-300">{t('drift.title')}</h3>
                {perms['can-update'] ? (
                  <button
                    onClick={handleDriftToggle}
                    disabled={savingDrift}
                    className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                      attrs['drift-detection-enabled']
                        ? 'bg-green-600 hover:bg-green-500 text-white'
                        : 'bg-slate-600 hover:bg-slate-500 text-slate-200'
                    }`}
                  >
                    {savingDrift ? t('actions.savingEllipsis') : attrs['drift-detection-enabled'] ? t('common.enabled') : t('common.disabled')}
                  </button>
                ) : (
                  <span className={`px-3 py-1.5 rounded-lg text-sm font-medium ${
                    attrs['drift-detection-enabled'] ? 'bg-green-900/50 text-green-300' : 'bg-slate-700 text-slate-400'
                  }`}>
                    {attrs['drift-detection-enabled'] ? t('common.enabled') : t('common.disabled')}
                  </span>
                )}
              </div>
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4 min-w-0">
                {/* The interval is a DEPENDENT of the toggle in the header, so
                    with drift off it is absent rather than shown holding an
                    `86400` nothing is using (#2253). The toggle itself is never
                    hidden — hiding a dependent removes something meaningless,
                    while hiding a feature's own switch removes the only way to
                    discover it. `Check now` goes the same way, which is what
                    retires its "Enable drift detection first" tooltip. */}
                <Field
                  attr="drift-detection-interval-seconds"
                  ctx={driftCtx}
                  kind="select"
                  display={
                    DRIFT_INTERVALS.find(
                      (di) => di.value === (attrs['drift-detection-interval-seconds'] || 86400),
                    )?.label ?? String(attrs['drift-detection-interval-seconds'])
                  }
                  control={
                    <select
                      id="ws-drift-interval"
                      data-testid="ws-drift-interval"
                      value={attrs['drift-detection-interval-seconds'] || 86400}
                      onChange={(e) => handleDriftIntervalChange(Number(e.target.value))}
                      disabled={savingDrift}
                      className={EDIT_INPUT}
                    >
                      {DRIFT_INTERVALS.map((di) => (
                        <option key={di.value} value={di.value}>{di.label}</option>
                      ))}
                    </select>
                  }
                />
                <div className="min-w-0">
                  <dt className="text-xs text-slate-500">{t('drift.status')}</dt>
                  <dd className="mt-1 flex items-center gap-2">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${driftStatusBadge(attrs['drift-status']).cls}`}>
                      {driftStatusBadge(attrs['drift-status']).label}
                    </span>
                    {perms['can-queue-run'] && (attrs['drift-status'] === 'drifted' || attrs['drift-status'] === 'errored') && (
                      <button
                        onClick={handleDismissDrift}
                        disabled={dismissingDrift}
                        title={t('drift.dismissTitle')}
                        className="text-xs text-slate-400 hover:text-slate-200 disabled:text-slate-600 transition-colors"
                      >
                        {dismissingDrift ? t('drift.dismissing') : t('drift.dismiss')}
                      </button>
                    )}
                  </dd>
                </div>
                <div className="min-w-0">
                  <dt className="text-xs text-slate-500">{t('drift.lastChecked')}</dt>
                  <dd className="mt-1 text-sm text-slate-200">
                    {attrs['drift-last-checked-at'] ? new Date(attrs['drift-last-checked-at']).toLocaleString() : t('common.never')}
                  </dd>
                </div>
                {perms['can-queue-run'] && attrs['drift-detection-enabled'] && (
                  <div className="flex items-end min-w-0">
                    <button
                      onClick={handleCheckDriftNow}
                      disabled={checkingDrift || attrs.locked}
                      className="px-3 py-1.5 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
                      title={attrs.locked ? t('common.workspaceLocked') : phaseWord('driftCheckNowTitle')}
                    >
                      {checkingDrift ? t('actions.queuing') : t('drift.checkNow')}
                    </button>
                  </div>
                )}
              </dl>
            </div>

            {/* Plan Expiry (#646) */}
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
              <div className="mb-4">
                <h3 className="text-sm font-medium text-slate-300">{phaseWord('planExpiryTitle')}</h3>
                <p className="text-xs text-slate-500 mt-1">
                  {phaseWord('planExpiryDescription')}
                </p>
              </div>
              <dl className="grid grid-cols-1 min-w-0">
                <Field
                  attr="plan-expiry-seconds"
                  ctx={planExpiryCtx}
                  kind="select"
                  display={
                    PLAN_EXPIRY_OPTIONS.find((o) => o.value === (attrs['plan-expiry-seconds'] || 0))
                      ?.label ?? String(attrs['plan-expiry-seconds'])
                  }
                  control={
                    <select
                      id="ws-plan-expiry"
                      data-testid="ws-plan-expiry"
                      value={attrs['plan-expiry-seconds'] || 0}
                      onChange={(e) => handlePlanExpiryChange(Number(e.target.value))}
                      disabled={savingPlanExpiry}
                      className={EDIT_INPUT}
                    >
                      {PLAN_EXPIRY_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </select>
                  }
                />
              </dl>
            </div>

            {/* AI Plan Summary (#401) */}
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
              <div className="flex items-center justify-between mb-4">
                <div>
                  <h3 className="text-sm font-medium text-slate-300">{phaseWord('aiSummaryTitle')}</h3>
                  <p className="text-xs text-slate-500 mt-1">
                    {phaseWord('aiSummaryDescription')}
                  </p>
                </div>
              </div>
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4 min-w-0">
                <Field
                  attr="ai-summary-mode"
                  ctx={aiCtx}
                  kind="select"
                  display={aiModeLabel(attrs['ai-summary-mode'])}
                  options={[
                    { value: 'default', label: t('aiSummary.modeDefault') },
                    { value: 'enabled', label: t('aiSummary.modeEnabled') },
                    { value: 'disabled', label: t('aiSummary.modeDisabled') },
                  ]}
                />
                {/* The policy gate layered on the same summary (#1766). Its own
                    per-workspace override, because a workspace may want the
                    narrative without the gate or the other way round. */}
                <Field
                  attr="ai-policy-mode"
                  ctx={aiCtx}
                  kind="select"
                  display={aiModeLabel(attrs['ai-policy-mode'])}
                  control={
                    <select
                      id="ws-ai-policy-mode"
                      data-testid="ai-policy-mode"
                      value={attrs['ai-policy-mode'] || 'default'}
                      onChange={(e) =>
                        handleAiSummaryAttrUpdate({ 'ai-policy-mode': e.target.value })
                      }
                      disabled={savingAiSummary}
                      className={EDIT_INPUT}
                    >
                      <option value="default">{t('aiSummary.modeDefault')}</option>
                      <option value="enabled">{t('aiSummary.modeEnabled')}</option>
                      <option value="disabled">{t('aiSummary.modeDisabled')}</option>
                    </select>
                  }
                />
                {/* Full width, and its label carries an "(optional, additive)"
                    suffix `WsField` has no room for, so this one stays by
                    hand. */}
                <div className="sm:col-span-2 min-w-0">
                  <dt className="text-xs text-slate-500">
                    {t('aiSummary.contextLabel')}
                    <span className="ms-2 text-slate-600">{t('aiSummary.contextLabelSuffix')}</span>
                  </dt>
                  <dd className="mt-1">
                    {perms['can-update'] ? (
                      <textarea
                        value={aiSummaryContextDraft ?? attrs['ai-summary-context'] ?? ''}
                        onChange={(e) => setAiSummaryContextDraft(e.target.value)}
                        onBlur={() => {
                          if (
                            aiSummaryContextDraft !== null &&
                            aiSummaryContextDraft !== (attrs['ai-summary-context'] ?? '')
                          ) {
                            handleAiSummaryAttrUpdate({ 'ai-summary-context': aiSummaryContextDraft })
                          } else {
                            setAiSummaryContextDraft(null)
                          }
                        }}
                        placeholder={t('aiSummary.contextPlaceholder')}
                        rows={3}
                        maxLength={4000}
                        disabled={savingAiSummary}
                        className="w-full px-3 py-2 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-1 focus:ring-brand-500 font-mono"
                      />
                    ) : attrs['ai-summary-context'] ? (
                      <p className="text-sm text-slate-200 whitespace-pre-wrap">{attrs['ai-summary-context']}</p>
                    ) : (
                      <p className="text-sm text-slate-500 italic">{t('aiSummary.noContext')}</p>
                    )}
                    {savingHint('aiSummary.contextHint', savingAiSummary)}
                  </dd>
                </div>
              </dl>
            </div>

            {/* Security scanning (#1036) — exposed here by #1763.
                Absent on a Pulumi workspace (#1911): Checkov and Trivy read a
                Terraform-shaped plan document, which a Pulumi run does not
                produce, so the API refuses anything but `off` (#1567). Rendering the
                block anyway offered three settings whose only accepted value
                was the one already shown — dead controls that 422 on use. Same
                treatment as the Cost and Architecture tabs. */}
            {!isPulumi && (
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
              <div className="flex items-center justify-between mb-4">
                <div>
                  <h3 className="text-sm font-medium text-slate-300">{t('securityScan.title')}</h3>
                  <p className="text-xs text-slate-500 mt-1">{t('securityScan.description')}</p>
                </div>
              </div>
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4 min-w-0">
                <Field
                  attr="security-scan-enforcement"
                  ctx={scanCtx}
                  kind="select"
                  display={
                    attrs['security-scan-enforcement'] === 'off'
                      ? t('securityScan.enforcementOff')
                      : attrs['security-scan-enforcement'] === 'enforced'
                        ? t('securityScan.enforcementEnforced')
                        : t('securityScan.enforcementAdvisory')
                  }
                  options={[
                    { value: 'off', label: t('securityScan.enforcementOff') },
                    { value: 'advisory', label: t('securityScan.enforcementAdvisory') },
                    { value: 'enforced', label: t('securityScan.enforcementEnforced') },
                  ]}
                />
                <Field
                  attr="security-scan-engine"
                  ctx={scanCtx}
                  kind="select"
                  display={attrs['security-scan-engine'] || 'checkov'}
                  options={[
                    { value: 'checkov', label: t('securityScan.engineCheckov') },
                    { value: 'trivy', label: t('securityScan.engineTrivy') },
                    { value: 'both', label: t('securityScan.engineBoth') },
                  ]}
                />
                <Field
                  attr="security-scan-severity-threshold"
                  ctx={scanCtx}
                  kind="select"
                  display={attrs['security-scan-severity-threshold'] || 'high'}
                  options={[
                    { value: 'critical', label: t('securityScan.thresholdCritical') },
                    { value: 'high', label: t('securityScan.thresholdHigh') },
                    { value: 'medium', label: t('securityScan.thresholdMedium') },
                    { value: 'low', label: t('securityScan.thresholdLow') },
                  ]}
                />
                {/* Full width, and its label carries an "(optional)" suffix, so
                    this one stays by hand. */}
                <div className="sm:col-span-2 min-w-0">
                  <dt className="text-xs text-slate-500">
                    {t('securityScan.skipRules')}
                    <span className="ms-2 text-slate-600">{t('securityScan.skipRulesSuffix')}</span>
                  </dt>
                  <dd className="mt-1">
                    {perms['can-update'] ? (
                      <input
                        type="text"
                        aria-label={t('securityScan.skipRules')}
                        value={
                          scanSkipRulesDraft ??
                          (attrs['security-scan-skip-rules'] || []).join(', ')
                        }
                        onChange={(e) => setScanSkipRulesDraft(e.target.value)}
                        onBlur={() => {
                          if (scanSkipRulesDraft === null) return
                          const next = scanSkipRulesDraft
                            .split(',')
                            .map((r) => r.trim())
                            .filter(Boolean)
                          const current = attrs['security-scan-skip-rules'] || []
                          if (next.join('\u0000') !== current.join('\u0000')) {
                            handleSecurityScanAttrUpdate({ 'security-scan-skip-rules': next })
                          } else {
                            setScanSkipRulesDraft(null)
                          }
                        }}
                        placeholder={t('securityScan.skipRulesPlaceholder')}
                        disabled={savingSecurityScan}
                        className="w-full px-3 py-2 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-1 focus:ring-brand-500 font-mono"
                      />
                    ) : (attrs['security-scan-skip-rules'] || []).length > 0 ? (
                      <p className="text-sm text-slate-200 font-mono">
                        {(attrs['security-scan-skip-rules'] || []).join(', ')}
                      </p>
                    ) : (
                      <p className="text-sm text-slate-500 italic">{t('securityScan.noSkipRules')}</p>
                    )}
                    {savingHint('securityScan.skipRulesHint', savingSecurityScan)}
                  </dd>
                </div>
              </dl>
            </div>
            )}

            {/* Runner debug mode (#1764) */}
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
              <div className="flex items-start justify-between gap-4 mb-4">
                <div>
                  <h3 className="text-sm font-medium text-slate-300">{t('debugMode.title')}</h3>
                  <p className="text-xs text-slate-500 mt-1">{t('debugMode.description')}</p>
                </div>
                {attrs['debug-mode'] && (
                  <span
                    data-testid="debug-mode-indicator"
                    className="shrink-0 px-2 py-1 rounded text-xs font-medium bg-amber-900/40 text-amber-300 border border-amber-700/50"
                  >
                    {t('debugMode.activeBadge')}
                  </span>
                )}
              </div>
              <dl className="grid grid-cols-1 min-w-0">
                <Field
                  attr="debug-mode"
                  ctx={debugCtx}
                  kind="checkbox"
                  display={attrs['debug-mode'] ? t('common.enabled') : t('common.disabled')}
                  control={instantToggle('debug-mode', debugCtx, !!attrs['debug-mode'])}
                />
              </dl>
            </div>

            {/* Speculative plans on fork pull requests (GHSA-gp5w-76rw-c452) */}
            {/* The toggle is this feature's OWN switch, not a dependent, so it
                is shown whether or not a VCS connection exists — hiding it
                would remove the only place the setting can be discovered, and
                `workspaces.spec.ts` asserts it visible on a workspace that has
                no connection. `FIELDS` marks the attribute a dependent of
                `vcs-connection-id`, which is right for the create form and
                wrong here, so this card renders the control directly. */}
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
              <div className="flex items-start justify-between gap-4 mb-4">
                <div>
                  <h3 className="text-sm font-medium text-slate-300">
                    {t('allowForkPrPlans.title')}
                  </h3>
                  <p className="text-xs text-slate-500 mt-1">
                    {t('allowForkPrPlans.description')}
                  </p>
                </div>
                {attrs['allow-fork-pr-plans'] && (
                  <span
                    data-testid="allow-fork-pr-plans-indicator"
                    className="shrink-0 px-2 py-1 rounded text-xs font-medium bg-amber-900/40 text-amber-300 border border-amber-700/50"
                  >
                    {t('allowForkPrPlans.activeBadge')}
                  </span>
                )}
              </div>
              <dl className="grid grid-cols-1 min-w-0">
                {/* A DEPENDENT of `vcs-connection-id`: with no connection there
                    are no pull requests to plan, so the control is absent
                    rather than offered against a gate nobody set (#2253). */}
                <Field
                  attr="allow-fork-pr-plans"
                  ctx={forkCtx}
                  kind="checkbox"
                  display={attrs['allow-fork-pr-plans'] ? t('common.enabled') : t('common.disabled')}
                  control={instantToggle('allow-fork-pr-plans', forkCtx, !!attrs['allow-fork-pr-plans'])}
                />
              </dl>
            </div>

            {/* Slack notifications (#556) */}
            <div className="bg-slate-800/50 rounded-lg border border-slate-700 p-6">
              <h3 className="text-sm font-medium text-slate-200">{t('slack.title')}</h3>
              <p className="text-sm text-slate-400 mt-1">
                {phaseWord('slackDescription')}
              </p>
              <dl className="mt-4 grid grid-cols-1 min-w-0">
                <Field
                  attr="slack-channel"
                  ctx={slackCtx}
                  kind="text"
                  display={
                    attrs['slack-channel'] ? (
                      <span className="font-mono">{attrs['slack-channel']}</span>
                    ) : (
                      <span className="text-slate-500 italic">{t('slack.notSet')}</span>
                    )
                  }
                  control={
                    <input
                      id="ws-slack-channel"
                      data-testid="ws-slack-channel"
                      type="text"
                      value={slackChannelDraft ?? attrs['slack-channel'] ?? ''}
                      onChange={(e) => setSlackChannelDraft(e.target.value)}
                      onBlur={() => {
                        if (
                          slackChannelDraft !== null &&
                          slackChannelDraft !== (attrs['slack-channel'] ?? '')
                        ) {
                          handleSlackChannelUpdate(slackChannelDraft.trim())
                        } else {
                          setSlackChannelDraft(null)
                        }
                      }}
                      placeholder={t('slack.channelPlaceholder')}
                      maxLength={128}
                      disabled={savingSlackChannel}
                      className="mt-1 w-full sm:w-96 px-3 py-2 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-1 focus:ring-brand-500 font-mono"
                    />
                  }
                />
              </dl>
            </div>

            {/* Delete */}
            {perms['can-destroy'] && (
              <div className="bg-slate-800/50 rounded-lg border border-red-900/30 p-6">
                <div className="flex items-center justify-between">
                  <div>
                    <h3 className="text-sm font-medium text-red-400">{t('delete.title')}</h3>
                    <p className="text-sm text-slate-400 mt-1">{t('delete.description')}</p>
                  </div>
                  {!showDeleteConfirm ? (
                    <button
                      onClick={() => setShowDeleteConfirm(true)}
                      className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600/20 hover:bg-red-600/40 text-red-400 transition-colors"
                    >
                      {t('actions.delete')}
                    </button>
                  ) : (
                    <div className="flex gap-2">
                      <button onClick={() => setShowDeleteConfirm(false)} className="px-3 py-1.5 rounded-lg text-sm font-medium text-slate-400 hover:text-slate-200">
                        {t('actions.cancel')}
                      </button>
                      <button
                        onClick={handleDelete}
                        disabled={deleting}
                        className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-500 text-white transition-colors"
                      >
                        {deleting ? t('actions.deleting') : t('delete.confirm')}
                      </button>
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Variables Tab */}
        {activeTab === 'variables' && (
          <div>
            {/* Which variable sets reach this workspace, and why (#1440).
                Read-only: the binding is managed on the set, and a rule-derived
                one is not bound per-workspace at all. */}
            <ApplicableVarsets workspaceId={workspaceId} />

            {perms['can-update-variable'] && (
              <div className="flex justify-end mb-4">
                <button
                  onClick={() => { setShowAddVar(!showAddVar); ensureVaultAvailability() }}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors"
                >
                  {showAddVar ? t('actions.cancel') : t('variables.addVariable')}
                </button>
              </div>
            )}

            {showAddVar && (
              <form onSubmit={handleAddVariable} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
                {/* Category first — the form below adapts to it (git categories swap
                    the value field for a credential builder, #1028). */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="var-cat" className="block text-sm font-medium text-slate-300 mb-1">{t('variables.category')}</label>
                    <select id="var-cat" value={varCategory} onChange={(e) => changeVarCategory(e.target.value)} className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      {/* One category for the engine's own parameters,
                          whatever the workspace runs (#1898): Terraform input
                          variables, Pulumi stack config, Ansible extra vars are
                          one role with three deliveries, and the runner
                          dispatches on the engine. An engine-specific option
                          here would be the mechanism showing through. */}
                      <option value="native">{t(`variables.${nativeCategoryKey(attrs.engine)}`)}</option>
                      <option value="env">{t('variables.categoryEnv')}</option>
                      <option value="git_http_auth">Git HTTPS credential</option>
                      <option value="git_ssh_auth">Git SSH credential</option>
                      {/* Translated, unlike the git pair above: those predate
                          the i18n gate and are allowlisted, and a new
                          user-facing option is held to the rule the gate
                          states (#1933). */}
                      <option value={ANSIBLE_MACHINE_CATEGORY}>{ta('categoryMachine')}</option>
                      <option value={ANSIBLE_VAULT_CATEGORY}>{ta('categoryVault')}</option>
                    </select>
                  </div>
                  <div>
                    {/* `key` is the credential's SCOPE on all four credential
                        categories, and each scopes a different thing — a git
                        URL pattern, a host pattern, a vault ID label. One
                        "Key" label over all of them is how a group name ends
                        up in the field that wants a vault label. */}
                    <label htmlFor="var-key" className="block text-sm font-medium text-slate-300 mb-1">{varKeyLabel}</label>
                    <input id="var-key" type="text" value={varKey} onChange={(e) => setVarKey(e.target.value)} required placeholder={varKeyPlaceholder} className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                    {varKeyHint && <p className="mt-1 text-xs text-slate-500">{varKeyHint}</p>}
                  </div>
                </div>

                {isAnsibleCat && (
                  <>
                    <AnsibleCredentialFields
                      idPrefix="add"
                      category={varCategory}
                      value={varValue}
                      onChange={setVarValue}
                    />
                    {/* The phase slots still apply, and on a credential they
                        matter most (#2189, #2190): a machine credential the
                        configure needs is one the plan has no use for, and
                        the fleet's own key is one the apply has no use for
                        either. There is no Sensitive checkbox — the API
                        forces an Ansible credential sensitive, so a tickbox
                        here would be a choice the operator does not have. */}
                    <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                      <PhaseSlotFlags
                        namespace="workspaceDetail.variables"
                        state={phaseSlots}
                        onChange={patchPhaseSlots}
                        labelClassName="text-sm text-slate-300"
                      />
                    </div>
                    <PhaseSlotNote namespace="workspaceDetail.variables" state={phaseSlots} />
                  </>
                )}

                {!isGitCat && !isAnsibleCat && vaultOfferable && (
                  <div>
                    <label htmlFor="var-source" className="block text-sm font-medium text-slate-300 mb-1">{t('variables.valueSource')}</label>
                    <select id="var-source" value={varSource} onChange={(e) => setVarSource(e.target.value as 'static' | 'vault')} className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      <option value="static">{t('variables.sourceStatic')}</option>
                      <option value="vault">{t('variables.sourceVault')}</option>
                    </select>
                    <p className="mt-1 text-xs text-slate-500">
                      {varSource === 'vault' ? t('variables.sourceVaultHelp') : t('variables.sourceStaticHelp')}
                    </p>
                  </div>
                )}

                {isVaultSource && (
                  <VaultReferenceFields
                    idPrefix="add"
                    instances={vaultInstances}
                    defaultInstance={vaultDefaultInstance}
                    value={vaultRef}
                    onChange={setVaultRef}
                    checkUrl={vaultCheckUrl}
                    variableKey={varKey}
                  />
                )}

                {!isGitCat && !isAnsibleCat && !isVaultSource && (
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                      <label htmlFor="var-val" className="block text-sm font-medium text-slate-300 mb-1">{t('variables.value')}</label>
                      <SensitiveValueInput id="var-val" value={varValue} onChange={setVarValue} sensitive={varSensitive} placeholder="us-east-1" rows={2} className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent resize-y" />
                    </div>
                    {/* `flex-wrap` is load-bearing with four checkboxes: at
                        phone width a single row of them overflows the grid
                        cell, and a horizontal page scroll is the one thing the
                        mobile guard refuses outright. */}
                    <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
                      <label className="flex items-center gap-2 cursor-pointer">
                        <input type="checkbox" checked={varSensitive} onChange={(e) => setVarSensitive(e.target.checked)} className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                        <span className="text-sm text-slate-300">{t('variables.sensitive')}</span>
                      </label>
                      {/* Universal: withheld from the plan whatever the
                          category (#2189), and from the apply as well when
                          only a configure has any use for it (#2190). */}
                      <PhaseSlotFlags
                        namespace="workspaceDetail.variables"
                        state={phaseSlots}
                        onChange={patchPhaseSlots}
                        labelClassName="text-sm text-slate-300"
                      />
                      {/* Only a native variable can be typed: an environment
                          variable is a string by definition, so offering the
                          flag there was always meaningless (#1435).

                          Labelled "Structured", not "HCL": one flag, and what
                          it means is the engine's (#1898). Terraform reads a
                          raw HCL expression; Pulumi sets a nested config value
                          rather than a literal dotted key. "HCL" named
                          Terraform's mechanism on a control that is not
                          Terraform's. */}
                      {varCategory === 'native' && (
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input type="checkbox" checked={varHcl} onChange={(e) => setVarHcl(e.target.checked)} className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                          <span className="text-sm text-slate-300">{t('variables.structured')}</span>
                        </label>
                      )}
                      {/* `w-full` puts the note on its own line of the wrapped
                          row rather than squeezing it between two checkboxes. */}
                      <PhaseSlotNote
                        namespace="workspaceDetail.variables"
                        state={phaseSlots}
                        className="w-full text-xs text-slate-500"
                      />
                    </div>
                  </div>
                )}

                {varCategory === 'git_http_auth' && (
                  <div className="space-y-3 rounded-lg border border-slate-700/50 bg-slate-900/30 p-3">
                    <p className="text-xs text-slate-400">{t('variables.gitHttpHint')}</p>
                    <div>
                      <label className="block text-sm font-medium text-slate-300 mb-1">{t('variables.gitCredSource')}</label>
                      <select id="git-source" value={gitSource} onChange={(e) => setGitSource(e.target.value as 'vcs_connection' | 'static')} className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                        <option value="vcs_connection">{t('variables.gitSourceVcs')}</option>
                        <option value="static">{t('variables.gitSourceStatic')}</option>
                      </select>
                    </div>
                    {gitSource === 'vcs_connection' ? (
                      <div>
                        <label className="block text-sm font-medium text-slate-300 mb-1">{t('variables.gitVcsConnection')}</label>
                        <select value={gitVcsConnId} onChange={(e) => setGitVcsConnId(e.target.value)} required className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                          <option value="">{t('variables.gitVcsConnectionSelect')}</option>
                          {vcsConnections.map((c) => (
                            <option key={c.id} value={c.id}>{c.attributes.name} ({c.attributes.provider})</option>
                          ))}
                        </select>
                        <p className="text-xs text-slate-500 mt-1">{t('variables.gitVcsConnectionHint')}</p>
                      </div>
                    ) : (
                      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        <div>
                          <label className="block text-sm font-medium text-slate-300 mb-1">{t('variables.gitUsername')}</label>
                          <input id="git-username" type="text" value={gitUsername} onChange={(e) => setGitUsername(e.target.value)} placeholder="x-access-token" className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                        </div>
                        <div>
                          <label className="block text-sm font-medium text-slate-300 mb-1">{t('variables.gitToken')}</label>
                          <SensitiveValueInput id="git-token" value={gitToken} onChange={setGitToken} sensitive placeholder="ghp_xxx" rows={2} className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent resize-y" />
                        </div>
                      </div>
                    )}
                    <div>
                      <label className="block text-sm font-medium text-slate-300 mb-1">{t('variables.gitRewrite')}</label>
                      <select value={gitRewrite} onChange={(e) => setGitRewrite(e.target.value as 'none' | 'to_https' | 'to_ssh')} className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                        <option value="none">{t('variables.gitRewriteNone')}</option>
                        <option value="to_https">{t('variables.gitRewriteToHttps')}</option>
                      </select>
                    </div>
                  </div>
                )}

                {varCategory === 'git_ssh_auth' && (
                  <div className="space-y-3 rounded-lg border border-slate-700/50 bg-slate-900/30 p-3">
                    <p className="text-xs text-slate-400">{t('variables.gitSshHint')}</p>
                    <div>
                      <label className="block text-sm font-medium text-slate-300 mb-1">{t('variables.gitPrivateKey')}</label>
                      <SensitiveValueInput id="git-key" value={gitPrivateKey} onChange={setGitPrivateKey} sensitive placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" rows={4} className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent resize-y" />
                    </div>
                    <div>
                      <label className="block text-sm font-medium text-slate-300 mb-1">{t('variables.gitKnownHosts')}</label>
                      <textarea value={gitKnownHosts} onChange={(e) => setGitKnownHosts(e.target.value)} placeholder="github.com ssh-ed25519 AAAA..." rows={2} className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent resize-y" />
                    </div>
                    <div>
                      <label className="block text-sm font-medium text-slate-300 mb-1">{t('variables.gitRewrite')}</label>
                      <select value={gitRewrite} onChange={(e) => setGitRewrite(e.target.value as 'none' | 'to_https' | 'to_ssh')} className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                        <option value="none">{t('variables.gitRewriteNone')}</option>
                        <option value="to_ssh">{t('variables.gitRewriteToSsh')}</option>
                      </select>
                    </div>
                  </div>
                )}

                <button type="submit" disabled={addingVar} className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {addingVar ? t('actions.adding') : t('variables.addVariable')}
                </button>
              </form>
            )}

            {varsLoading ? (
              <LoadingSpinner />
            ) : variables.length === 0 ? (
              <EmptyState message={t('variables.empty')} />
            ) : (
              <>
              {/* Desktop (md+): the variables table. Below md it's replaced by
                  the stacked cards (a phone can't show the table + inline edit
                  legibly). #719 Stage 2. */}
              <div className="hidden md:block bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <SortableHeader label={t('variables.key')} sortKey="key" sortState={varSortState} onSort={toggleVarSort} />
                      <SortableHeader label={t('variables.value')} sortKey="value" sortState={varSortState} onSort={toggleVarSort} />
                      <SortableHeader label={t('variables.category')} sortKey="category" sortState={varSortState} onSort={toggleVarSort} className="hidden sm:table-cell" />
                      <th className="px-4 py-3 text-end text-xs font-medium text-slate-400 uppercase tracking-wider">{t('common.actions')}</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {sortedVars.map((v) =>
                      editingVarId === v.id ? (
                        <tr key={v.id} className="bg-slate-700/20">
                          {/* One full-width cell. The Vault reference builder
                              needs five fields and the VALUE column truncated
                              them to unreadable stubs (#1439). */}
                          <td colSpan={4} className="px-4 py-4">
                            <VariableEditPanel
                              idPrefix={`edit-${v.id}`}
                              vaultCheckUrl={vaultCheckUrl}
                              engine={attrs.engine}
                              state={editPanelState}
                              onChange={patchEditPanel}
                              vaultAvailable={vaultOfferable}
                              vaultInstances={vaultInstances}
                              vaultDefaultInstance={vaultDefaultInstance}
                              saving={savingVar}
                              onSave={handleSaveVar}
                              saveError={error}
                              onCancel={() => setEditingVarId(null)}
                            />
                          </td>
                        </tr>
                      ) : (
                        <tr key={v.id} className="hover:bg-slate-700/20 transition-colors">
                          <td className="px-4 py-3 text-sm text-slate-200 font-mono">
                            <span className="inline-flex items-center gap-2">
                              {v.attributes.key}
                              {v.attributes['apply-only'] && (
                                <span title={t('variables.applyOnlyHint')} className="shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900/50 text-amber-300">{t('variables.applyOnly')}</span>
                              )}
                              {/* A third slot means a key can now be held
                                  THREE times, so the badge carries more
                                  weight than it did: without it the rows are
                                  identical text, and the category pill that
                                  might otherwise tell them apart is
                                  `hidden sm:table-cell`. Teal rather than
                                  amber so the two slots are distinguishable
                                  at a glance on adjacent rows. */}
                              {v.attributes['configure-only'] && (
                                <span title={t('variables.configureOnlyHint')} className="shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-teal-900/50 text-teal-300">{t('variables.configureOnly')}</span>
                              )}
                            </span>
                          </td>
                          <td className="px-4 py-3 text-sm text-slate-400 font-mono">
                            {v.attributes['value-source'] === 'vault'
                            ? <VaultValueDisplay value={v.attributes.value} varKey={v.attributes.key} />
                            : v.attributes.sensitive ? '***' : (v.attributes.value || <span className="text-slate-600 italic">{t('variables.emptyValue')}</span>)}
                          </td>
                          <td className="px-4 py-3 text-xs text-slate-400 hidden sm:table-cell">
                            <div className="flex flex-wrap items-center gap-1.5">
                              <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                                v.attributes.category === 'native' ? 'bg-purple-900/50 text-purple-300' : 'bg-cyan-900/50 text-cyan-300'
                              }`}>
                                {categoryLabel(v.attributes.category, attrs.engine)}
                              </span>
                            </div>
                          </td>
                          {perms['can-update-variable'] && (
                            <td className="px-4 py-3 text-end">
                              <div className="flex justify-end gap-2">
                                <button onClick={() => startEditingVar(v)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200">{t('actions.edit')}</button>
                                <button onClick={() => handleDeleteVariable(v.id)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300">{t('actions.delete')}</button>
                              </div>
                            </td>
                          )}
                        </tr>
                      )
                    )}
                  </tbody>
                </table>
              </div>

              {/* Mobile (< md): variables as stacked cards — same data + inline
                  edit / delete states as the table. Value is masked when
                  sensitive; the category pill is kept; actions are proper
                  buttons (not tiny text). */}
              <ul className="md:hidden space-y-2">
                {sortedVars.map((v) => (
                  <li key={v.id} className="rounded-lg border border-slate-700/50 bg-slate-800/50 p-3">
                    {editingVarId === v.id ? (
                      <VariableEditPanel
                          idPrefix={`medit-${v.id}`}
                          engine={attrs.engine}
                          vaultCheckUrl={vaultCheckUrl}
                          state={editPanelState}
                          onChange={patchEditPanel}
                          vaultAvailable={vaultOfferable}
                          vaultInstances={vaultInstances}
                          vaultDefaultInstance={vaultDefaultInstance}
                          saving={savingVar}
                          onSave={handleSaveVar}
                          saveError={error}
                          onCancel={() => setEditingVarId(null)}
                        />
                    ) : (
                      <>
                        <div className="flex items-start justify-between gap-2 mb-1.5">
                          <span className="text-sm font-mono font-medium text-slate-200 break-all">
                            {v.attributes.key}
                            {v.attributes['apply-only'] && (
                              <span title={t('variables.applyOnlyHint')} className="ms-2 shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900/50 text-amber-300">{t('variables.applyOnly')}</span>
                            )}
                            {v.attributes['configure-only'] && (
                              <span title={t('variables.configureOnlyHint')} className="ms-2 shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-teal-900/50 text-teal-300">{t('variables.configureOnly')}</span>
                            )}
                          </span>
                          <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                            v.attributes.category === 'native' ? 'bg-purple-900/50 text-purple-300' : 'bg-cyan-900/50 text-cyan-300'
                          }`}>
                            {categoryLabel(v.attributes.category, attrs.engine)}
                          </span>
                        </div>
                        <div className="mb-2 text-sm text-slate-400 font-mono break-all">
                          {v.attributes['value-source'] === 'vault'
                            ? <VaultValueDisplay value={v.attributes.value} varKey={v.attributes.key} />
                            : v.attributes.sensitive ? '***' : (v.attributes.value || <span className="text-slate-600 italic">{t('variables.emptyValue')}</span>)}
                        </div>
                        {perms['can-update-variable'] && (
                          <div className="flex gap-2">
                            <button onClick={() => startEditingVar(v)} className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200">{t('actions.edit')}</button>
                            <button
                              onClick={() => handleDeleteVariable(v.id)}
                              className="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300"
                            >
                              {t('actions.delete')}
                            </button>
                          </div>
                        )}
                      </>
                    )}
                  </li>
                ))}
              </ul>
              </>
            )}
          </div>
        )}

        {/* Runs Tab */}
        {activeTab === 'runs' && (
          <div>
            {perms['can-queue-run'] && (
              <div className="mb-4">
                <div className="flex justify-end items-center gap-2">
                  <button
                    onClick={() => setShowPlanOptions(!showPlanOptions)}
                    className="px-3 py-2 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 text-slate-300 transition-colors"
                  >
                    {showPlanOptions ? t('runs.hideOptions') : t('runs.options')}
                  </button>
                  {/* A destroy is its own grant (run:apply-destroy): offering it
                      on can-queue-run alone showed plan-level users a button
                      the API refuses (#1634). */}
                  {perms['can-queue-destroy'] && (!showDestroyConfirm ? (
                    <button
                      onClick={() => setShowDestroyConfirm(true)}
                      disabled={queueingDestroy || attrs.locked}
                      className="px-4 py-2 rounded-lg text-sm font-medium bg-red-600/20 hover:bg-red-600/40 text-red-400 transition-colors"
                      title={attrs.locked ? t('common.workspaceLocked') : phaseWord('queueDestroyTitle')}
                    >
                      {t('runs.queueDestroy')}
                    </button>
                  ) : (
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-red-400">{t('runs.destroyAllConfirm')}</span>
                      <button
                        onClick={() => setShowDestroyConfirm(false)}
                        className="px-3 py-1.5 rounded-lg text-sm font-medium text-slate-400 hover:text-slate-200 transition-colors"
                      >
                        {t('actions.cancel')}
                      </button>
                      <button
                        onClick={handleQueueDestroy}
                        disabled={queueingDestroy}
                        className="px-4 py-2 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-500 disabled:bg-red-800 text-white transition-colors"
                      >
                        {queueingDestroy ? t('actions.queuing') : t('runs.confirmDestroy')}
                      </button>
                    </div>
                  ))}
                  {/* Two buttons, not a button plus a hidden checkbox (#1340).
                      Plan-vs-apply is the decision that matters every time, so
                      it is made by which button you press; the options panel
                      keeps only the genuinely occasional flags. Pressing Plan
                      does exactly what it did before. */}
                  <button
                    onClick={() => handleQueuePlan(true)}
                    disabled={queueingPlan || attrs.locked}
                    className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
                    title={attrs.locked ? t('common.workspaceLocked') : undefined}
                  >
                    {queueingPlan ? t('actions.queuing') : phaseWord('queuePlan')}
                  </button>
                  {perms['can-queue-apply'] && (
                    <button
                      onClick={() => handleQueuePlan(false)}
                      // Disabled on a non-default VCS ref because the server
                      // forces those runs plan-only. Leaving it enabled would
                      // promise an apply and quietly deliver a plan.
                      disabled={queueingPlan || attrs.locked || !!vcsRef}
                      className="px-4 py-2 rounded-lg text-sm font-medium bg-amber-600 hover:bg-amber-500 disabled:bg-amber-900/40 disabled:text-amber-600/70 text-white transition-colors"
                      title={
                        attrs.locked
                          ? t('common.workspaceLocked')
                          : vcsRef
                            ? phaseWord('queueApplyRefBlocked')
                            : phaseWord('queueApplyTitle')
                      }
                    >
                      {queueingPlan ? t('actions.queuing') : phaseWord('queueRun')}
                    </button>
                  )}
                  {/* The configure trigger (#1974), data-gated: a workspace that
                      declares no configure operation shows no button, so a
                      terraform/tofu-only deployment pays nothing and there is no
                      flag to set. Gated on `can-queue-apply` because a configure
                      is apply-equivalent -- it needs `run:apply`, so offering it
                      to a plan-level role would show a button the API refuses
                      (#1634's lesson, applied to the third run kind). */}
                  {attrs['configure-enabled'] && perms['can-queue-apply'] && (
                    <button
                      onClick={handleQueueConfigure}
                      disabled={queueingConfigure || attrs.locked}
                      className="px-4 py-2 rounded-lg text-sm font-medium bg-violet-600 hover:bg-violet-500 disabled:bg-violet-900/40 disabled:text-violet-500/70 text-white transition-colors"
                      title={attrs.locked ? t('common.workspaceLocked') : t('runs.queueConfigureTitle')}
                    >
                      {queueingConfigure ? t('actions.queuing') : t('runs.queueConfigure')}
                    </button>
                  )}
                </div>
                {showPlanOptions && (
                  <div className="mt-3 p-4 bg-slate-800/50 rounded-lg border border-slate-700/50">
                    <h4 className="text-sm font-medium text-slate-300 mb-3">{phaseWord('planOptions')}</h4>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <div>
                        <label className="block text-xs text-slate-400 mb-1">{t('runs.targetResources')} <span className="text-slate-500">{t('runs.commaSeparated')}</span></label>
                        <input
                          type="text"
                          value={planTargets}
                          onChange={e => setPlanTargets(e.target.value)}
                          placeholder={phaseWord('targetPlaceholder')}
                          className="w-full px-3 py-2 bg-slate-900 border border-slate-600 rounded-lg text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-brand-500 font-mono"
                        />
                      </div>
                      <div>
                        <label className="block text-xs text-slate-400 mb-1">{t('runs.replaceResources')} <span className="text-slate-500">{t('runs.commaSeparated')}</span></label>
                        <input
                          type="text"
                          value={planReplaces}
                          onChange={e => setPlanReplaces(e.target.value)}
                          placeholder={phaseWord('replacePlaceholder')}
                          className="w-full px-3 py-2 bg-slate-900 border border-slate-600 rounded-lg text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-brand-500 font-mono"
                        />
                      </div>
                    </div>
                    {/* The configure run's own two options (#1974), shown only
                        on a workspace that has the operation. They are
                        OVERRIDES: empty means the workspace's declared default,
                        which is what a git-driven configure uses because a push
                        carries no run options. The limit is the safety surface,
                        so the hint says what empty means rather than leaving an
                        operator to find out. */}
                    {attrs['configure-enabled'] && (
                      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mt-4 pt-4 border-t border-slate-700/50">
                        <div>
                          <label htmlFor="cfg-playbook" className="block text-xs text-slate-400 mb-1">
                            {t('runs.configurePlaybook')}
                          </label>
                          <input
                            id="cfg-playbook"
                            type="text"
                            value={configurePlaybook}
                            onChange={e => setConfigurePlaybook(e.target.value)}
                            placeholder={attrs['default-playbook'] || t('runs.configurePlaybookPlaceholder')}
                            className="w-full px-3 py-2 bg-slate-900 border border-slate-600 rounded-lg text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-brand-500 font-mono"
                          />
                        </div>
                        <div>
                          <label htmlFor="cfg-limit" className="block text-xs text-slate-400 mb-1">
                            {t('runs.configureLimit')}
                          </label>
                          <input
                            id="cfg-limit"
                            type="text"
                            value={configureLimit}
                            onChange={e => setConfigureLimit(e.target.value)}
                            placeholder={attrs['default-limit'] || t('runs.configureLimitPlaceholder')}
                            className="w-full px-3 py-2 bg-slate-900 border border-slate-600 rounded-lg text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-brand-500 font-mono"
                          />
                          <p className="mt-1 text-xs text-slate-500">{t('runs.configureLimitHint')}</p>
                        </div>
                      </div>
                    )}
                    <div className="flex flex-wrap gap-4 mt-3">
                      {/* The plan-only checkbox that used to live here is now
                          the Plan / Plan + apply button pair above (#1340). */}
                      <label className="flex items-center gap-2 text-sm text-slate-300 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={planRefreshOnly}
                          onChange={e => {
                            setPlanRefreshOnly(e.target.checked)
                            if (e.target.checked) setPlanRefresh(true)
                          }}
                          className="rounded border-slate-600 bg-slate-900 text-brand-500 focus:ring-brand-500"
                        />
                        {t('runs.refreshOnly')}
                      </label>
                      <label className="flex items-center gap-2 text-sm text-slate-300 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={!planRefresh}
                          onChange={e => {
                            setPlanRefresh(!e.target.checked)
                            if (e.target.checked) setPlanRefreshOnly(false)
                          }}
                          className="rounded border-slate-600 bg-slate-900 text-brand-500 focus:ring-brand-500"
                        />
                        {t('runs.skipRefresh')}
                      </label>
                      {/* Terraform-only (#1911). `-allow-empty-apply` has no
                          Pulumi equivalent: `PulumiRunOptions` carries no such
                          field and the engine emits no env var for it, so a
                          ticked box on a Pulumi workspace was stored on the run
                          and then silently dropped. Target, replace and the two
                          refresh flags DO reach Pulumi (as `--target`/
                          `--replace` URNs and `--refresh`), so they stay. */}
                      {!vcsRef && !isPulumi && (
                        <label className="flex items-center gap-2 text-sm text-slate-300 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={planAllowEmpty}
                            onChange={e => setPlanAllowEmpty(e.target.checked)}
                            className="rounded border-slate-600 bg-slate-900 text-brand-500 focus:ring-brand-500"
                          />
                          {t('runs.allowEmptyApply')}
                        </label>
                      )}
                    </div>
                    {attrs['vcs-repo-url'] && (
                      <div className="mt-4 pt-3 border-t border-slate-700/50">
                        <label className="block text-xs text-slate-400 mb-2">{t('runs.vcsRef')}</label>
                        <div className="flex gap-2">
                          <select
                            value={vcsRefType}
                            onChange={e => {
                              setVcsRefType(e.target.value as 'branch' | 'tag')
                              setVcsRef('')
                            }}
                            className="px-2 py-2 bg-slate-900 border border-slate-600 rounded-lg text-sm text-slate-200 focus:outline-none focus:ring-2 focus:ring-brand-500"
                          >
                            <option value="branch">{t('runs.branch')}</option>
                            <option value="tag">{t('runs.tag')}</option>
                          </select>
                          <select
                            value={vcsRef}
                            onChange={e => setVcsRef(e.target.value)}
                            disabled={vcsRefsLoading}
                            className="flex-1 px-2 py-2 bg-slate-900 border border-slate-600 rounded-lg text-sm text-slate-200 focus:outline-none focus:ring-2 focus:ring-brand-500 disabled:opacity-50"
                          >
                            <option value="">
                              {vcsRefsLoading
                                ? t('actions.loading')
                                : vcsDefaultBranch ? t('runs.defaultBranch', { branch: vcsDefaultBranch }) : t('common.default')}
                            </option>
                            {(vcsRefType === 'branch' ? vcsBranches : vcsTags).map(ref => (
                              <option key={ref.name} value={ref.name}>
                                {ref.name}
                              </option>
                            ))}
                          </select>
                        </div>
                        {vcsRef && (
                          <p className="mt-2 text-xs text-amber-400">
                            {phaseWord('nonDefaultRefNote')}
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
            {runsLoading ? (
              <LoadingSpinner />
            ) : runs.length === 0 ? (
              <EmptyState message={t('runs.empty')} />
            ) : (
              <>
              {/* Desktop (md+): the sortable table, unchanged. Below md it is
                  hidden in favour of the stacked cards (#719 Stage 2) — a phone
                  can't show a 7-column table legibly. */}
              <div className="hidden md:block bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <SortableHeader label={t('runs.runId')} sortKey="id" sortState={runSortState} onSort={toggleRunSort} />
                      <SortableHeader label={t('runs.status')} sortKey="status" sortState={runSortState} onSort={toggleRunSort} />
                      <SortableHeader label={t('runs.type')} sortKey="type" sortState={runSortState} onSort={toggleRunSort} className="hidden sm:table-cell" />
                      <th className="text-start px-4 py-2 text-xs font-medium text-slate-400 uppercase tracking-wider hidden md:table-cell">{t('runs.changes')}</th>
                      <SortableHeader label={t('runs.source')} sortKey="source" sortState={runSortState} onSort={toggleRunSort} className="hidden sm:table-cell" />
                      <SortableHeader label={t('runs.triggeredBy')} sortKey="created-by" sortState={runSortState} onSort={toggleRunSort} className="hidden lg:table-cell" />
                      <SortableHeader label={t('runs.created')} sortKey="created-at" sortState={runSortState} onSort={toggleRunSort} className="hidden md:table-cell" />
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {sortedRuns.map((run) => (
                      <tr
                        key={run.id}
                        onClick={() => router.push(`/workspaces/${workspaceId}/runs/${run.id}`)}
                        className="hover:bg-slate-700/20 transition-colors cursor-pointer"
                      >
                        <td className="px-4 py-3 text-sm text-brand-400 font-mono">{run.id.replace(/^run-/, '').split('-').pop()}</td>
                        <td className="px-4 py-3">
                          {run.attributes.actions?.['is-confirmable'] ? (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900/50 text-amber-300">
                              {t('runs.needsConfirm')}
                            </span>
                          ) : (
                            run.attributes['blocked-by'] ? (
                              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900/50 text-amber-300">
                                {t('runs.awaitingDecision')}
                              </span>
                            ) : (
                              <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${statusColor(run.attributes.status)}`}>
                                {runStatusLabel(run.attributes.status)}
                              </span>
                            )
                          )}
                        </td>
                        <td className="px-4 py-3 hidden sm:table-cell">
                          <span className="flex flex-wrap items-center gap-1">
                            {run.attributes['is-destroy'] ? (
                              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-900/50 text-red-300">
                                {t('runs.typeDestroy')}
                              </span>
                            ) : run.attributes['plan-only'] ? (
                              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-cyan-900/50 text-cyan-300">
                                {phaseWord('badgePlanOnly')}
                              </span>
                            ) : (
                              <span className="text-xs text-slate-500">{phaseWord('typePlanApply')}</span>
                            )}
                            {/* Additive like the saved-plan badge beside it, and
                                for the same reason: a COMPOSITE is a plan+apply
                                AND a configure, so one badge replacing the other
                                would hide half of what the run does (#1974). */}
                            {run.attributes['is-configure'] && (
                              <span
                                className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-violet-900/50 text-violet-300"
                                title={run.attributes['configure-after-apply']
                                  ? t('runs.typeConfigureAfterApplyHint')
                                  : t('runs.typeConfigureHint')}
                              >
                                {t('runs.typeConfigure')}
                              </span>
                            )}
                            {/* Additive, not another branch: a saved plan can also
                                be a destroy, and "deferred" is the fact that
                                explains the run list, so it must not be the one
                                that loses. */}
                            {run.attributes['save-plan'] && (
                              <span
                                className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-violet-900/50 text-violet-300"
                                title={t('runs.typeSavedPlanHint')}
                              >
                                {t('runs.typeSavedPlan')}
                              </span>
                            )}
                          </span>
                        </td>
                        <td className="px-4 py-3 hidden md:table-cell">
                          {run.attributes['plan-summary'] ? (
                            <PlanSummaryBadges
                              summary={run.attributes['plan-summary']}
                              size="sm"
                              engine={workspace?.attributes.engine}
                            />
                          ) : (
                            <span className="text-slate-600">&mdash;</span>
                          )}
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-400 hidden sm:table-cell">
                          {run.attributes.source === 'module-test' ? (
                            <span className="text-purple-400">{t('runs.sourceModuleTest')}</span>
                          ) : run.attributes.source === 'module-publish' ? (
                            <span className="text-purple-400">{t('runs.sourceModulePublish')}</span>
                          ) : run.attributes.source}
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-400 hidden lg:table-cell">
                          {run.attributes['created-by'] || <span className="text-slate-600">&mdash;</span>}
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-500 hidden md:table-cell">
                          {run.attributes['created-at'] ? new Date(run.attributes['created-at']).toLocaleString() : ''}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {/* Mobile (< md): each run is a tappable card. The whole card is
                  the link (big touch target); Status stays prominent (never
                  hidden — the primary signal), destroy/plan-only keep their
                  coloured pills, and the rest reflow as label/value rows. */}
              <MobileCardList>
                {sortedRuns.map((run) => {
                  const shortId = run.id.replace(/^run-/, '').split('-').pop()
                  return (
                    <MobileCard
                      key={run.id}
                      href={`/workspaces/${workspaceId}/runs/${run.id}`}
                      title={<span className="text-sm font-mono text-brand-400">{shortId}</span>}
                      badge={
                        run.attributes.actions?.['is-confirmable'] ? (
                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900/50 text-amber-300">
                            {t('runs.needsConfirm')}
                          </span>
                        ) : (
                          run.attributes['blocked-by'] ? (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900/50 text-amber-300">
                              {t('runs.awaitingDecision')}
                            </span>
                          ) : (
                            <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${statusColor(run.attributes.status)}`}>
                              {runStatusLabel(run.attributes.status)}
                            </span>
                          )
                        )
                      }
                      fields={[
                        {
                          label: t('runs.type'),
                          value: (
                            <span className="flex flex-wrap items-center gap-1">
                              {run.attributes['is-destroy'] ? (
                                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-900/50 text-red-300">
                                  {t('runs.typeDestroy')}
                                </span>
                              ) : run.attributes['plan-only'] ? (
                                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-cyan-900/50 text-cyan-300">
                                  {phaseWord('badgePlanOnly')}
                                </span>
                              ) : (
                                <span className="text-slate-400">{phaseWord('typePlanApply')}</span>
                              )}
                              {run.attributes['is-configure'] && (
                                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-violet-900/50 text-violet-300">
                                  {t('runs.typeConfigure')}
                                </span>
                              )}
                              {run.attributes['save-plan'] && (
                                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-violet-900/50 text-violet-300">
                                  {t('runs.typeSavedPlan')}
                                </span>
                              )}
                            </span>
                          ),
                        },
                        ...(run.attributes['plan-summary']
                          ? [{
                              label: t('runs.changes'),
                              value: (
                                <PlanSummaryBadges
                                  summary={run.attributes['plan-summary']}
                                  size="sm"
                                  engine={workspace?.attributes.engine}
                                />
                              ),
                            }]
                          : []),
                        {
                          label: t('runs.source'),
                          value:
                            run.attributes.source === 'module-test' ? (
                              <span className="text-purple-400">{t('runs.sourceModuleTest')}</span>
                            ) : run.attributes.source === 'module-publish' ? (
                              <span className="text-purple-400">{t('runs.sourceModulePublish')}</span>
                            ) : (
                              run.attributes.source
                            ),
                        },
                        ...(run.attributes['created-by']
                          ? [{ label: t('runs.triggeredBy'), value: run.attributes['created-by'] }]
                          : []),
                        ...(run.attributes['created-at']
                          ? [{
                              label: t('runs.created'),
                              value: new Date(run.attributes['created-at']).toLocaleString(),
                              valueClassName: 'text-slate-400',
                            }]
                          : []),
                      ]}
                    />
                  )
                })}
              </MobileCardList>
              </>
            )}
          </div>
        )}

        {/* State Tab */}
        {activeTab === 'state' && (
          <div>
            {/* Confirmation dialog */}
            {confirmStateAction && (
              <div className="bg-red-900/30 border border-red-700/50 rounded-lg p-4 mb-4">
                <p className="text-sm text-red-200 mb-3">
                  {confirmStateAction.action === 'delete'
                    ? t('state.deleteConfirm', { serial: confirmStateAction.sv.attributes.serial })
                    : t('state.rollbackConfirm', { serial: confirmStateAction.sv.attributes.serial })}
                </p>
                <div className="flex gap-2">
                  <button
                    disabled={!!stateActionLoading}
                    onClick={async () => {
                      const { action, sv } = confirmStateAction
                      setStateActionLoading(sv.id)
                      try {
                        if (action === 'delete') {
                          const resp = await apiFetch(`/api/terrapod/v1/state-versions/${sv.id}/manage`, { method: 'DELETE' })
                          if (!resp.ok) {
                            const err = await resp.json().catch(() => ({ detail: t('errors.failed') }))
                            throw new Error(err.detail || t('errors.deleteStateVersion'))
                          }
                        } else {
                          const resp = await apiFetch(`/api/terrapod/v1/state-versions/${sv.id}/actions/rollback`, { method: 'POST' })
                          if (!resp.ok) {
                            const err = await resp.json().catch(() => ({ detail: t('errors.failed') }))
                            throw new Error(err.detail || t('errors.rollbackStateVersion'))
                          }
                        }
                        setConfirmStateAction(null)
                        loadStateVersions()
                      } catch (err) {
                        setError(err instanceof Error ? err.message : t('errors.stateActionFailed'))
                      } finally {
                        setStateActionLoading(null)
                      }
                    }}
                    className={`px-3 py-1.5 rounded text-xs font-medium text-white transition-colors ${
                      confirmStateAction.action === 'delete'
                        ? 'bg-red-600 hover:bg-red-500'
                        : 'bg-amber-600 hover:bg-amber-500'
                    }`}
                  >
                    {stateActionLoading ? t('actions.processing') : confirmStateAction.action === 'delete' ? t('state.confirmDelete') : t('state.confirmRollback')}
                  </button>
                  <button
                    onClick={() => setConfirmStateAction(null)}
                    className="px-3 py-1.5 rounded text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-300 transition-colors"
                  >
                    {t('actions.cancel')}
                  </button>
                </div>
              </div>
            )}

            {/* Upload state button */}
            {perms['can-create-state-versions'] && (
              <div className="flex justify-end mb-4">
                <label className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors cursor-pointer">
                  {t('state.uploadState')}
                  <input
                    type="file"
                    accept=".json,.tfstate"
                    className="hidden"
                    onChange={async (e) => {
                      const file = e.target.files?.[0]
                      if (!file) return
                      setStateActionLoading('upload')
                      try {
                        const body = await file.text()
                        JSON.parse(body) // validate JSON
                        const resp = await apiFetch(`/api/terrapod/v1/workspaces/${workspaceId}/state-versions/actions/upload`, {
                          method: 'POST',
                          headers: { 'Content-Type': 'application/json' },
                          body,
                        })
                        if (!resp.ok) {
                          const err = await resp.json().catch(() => ({ detail: t('errors.failed') }))
                          throw new Error(err.detail || t('errors.uploadState'))
                        }
                        loadStateVersions()
                      } catch (err) {
                        setError(err instanceof Error ? err.message : t('errors.uploadStateFile'))
                      } finally {
                        setStateActionLoading(null)
                        e.target.value = ''
                      }
                    }}
                  />
                </label>
              </div>
            )}

            {stateLoading ? (
              <LoadingSpinner />
            ) : stateVersions.length === 0 ? (
              <EmptyState message={t('state.empty')} />
            ) : (
              <>
                {/* Desktop (md+): the sortable table, unchanged columns. Actions
                    are proper buttons (Download/Rollback/Delete), not tiny text. */}
                <div className="hidden md:block bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                  <table className="w-full">
                    <thead>
                      <tr className="border-b border-slate-700/50">
                        <SortableHeader label={t('state.serial')} sortKey="serial" sortState={stateSortState} onSort={toggleStateSort} />
                        <SortableHeader label={t('state.createdBy')} sortKey="created-by" sortState={stateSortState} onSort={toggleStateSort} />
                        <th className="px-4 py-3 text-start text-xs font-medium text-slate-400 uppercase tracking-wider">{t('state.run')}</th>
                        <SortableHeader label={t('state.size')} sortKey="size" sortState={stateSortState} onSort={toggleStateSort} />
                        <SortableHeader label={t('state.created')} sortKey="created-at" sortState={stateSortState} onSort={toggleStateSort} className="hidden lg:table-cell" />
                        <th className="px-4 py-3 text-end text-xs font-medium text-slate-400 uppercase tracking-wider">{t('common.actions')}</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-700/30">
                      {sortedState.map((sv) => {
                        const maxSerial = Math.max(...stateVersions.map(s => s.attributes.serial))
                        const isLatest = sv.attributes.serial === maxSerial
                        const runData = sv.relationships?.run?.data
                        return (
                          <tr key={sv.id} className="hover:bg-slate-700/20 transition-colors">
                            <td className="px-4 py-3 text-sm text-slate-200 font-mono">#{sv.attributes.serial}</td>
                            <td className="px-4 py-3 text-xs text-slate-400">
                              {sv.attributes['created-by'] || <span className="text-slate-500">{t('state.runner')}</span>}
                            </td>
                            <td className="px-4 py-3 text-xs">
                              {runData ? (
                                <a href={`/workspaces/${workspaceId}/runs/${runData.id}`} className="text-brand-400 hover:text-brand-300">
                                  {runData.id.replace('run-', '').slice(0, 8)}
                                </a>
                              ) : (
                                <span className="text-slate-500">-</span>
                              )}
                            </td>
                            <td className="px-4 py-3 text-xs text-slate-400">
                              {sv.attributes.size > 0 ? `${(sv.attributes.size / 1024).toFixed(1)} KB` : '-'}
                            </td>
                            <td className="px-4 py-3 text-xs text-slate-500 hidden lg:table-cell">
                              {sv.attributes['created-at'] ? new Date(sv.attributes['created-at']).toLocaleString() : ''}
                            </td>
                            <td className="px-4 py-3 text-end">
                              <div className="flex items-center justify-end gap-2">
                                <button
                                  onClick={() => downloadStateVersion(sv)}
                                  className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200"
                                >
                                  {t('actions.download')}
                                </button>
                                {!isLatest && perms['can-create-state-versions'] && (
                                  <button
                                    onClick={() => setConfirmStateAction({ action: 'rollback', sv })}
                                    className="px-2.5 py-1 rounded-md text-xs font-medium bg-amber-900/40 hover:bg-amber-900/60 text-amber-300"
                                  >
                                    {t('actions.rollback')}
                                  </button>
                                )}
                                {!isLatest && perms['can-update'] && (
                                  <button
                                    onClick={() => setConfirmStateAction({ action: 'delete', sv })}
                                    className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300"
                                  >
                                    {t('actions.delete')}
                                  </button>
                                )}
                              </div>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>

                {/* Mobile (< md): state versions as cards so nothing is dropped —
                    the table hid Created-by / Run / Size / Created behind
                    sm/md/lg breakpoints, leaving phones with only the serial. */}
                <MobileCardList>
                  {sortedState.map((sv) => {
                    const maxSerial = Math.max(...stateVersions.map(s => s.attributes.serial))
                    const isLatest = sv.attributes.serial === maxSerial
                    const runData = sv.relationships?.run?.data
                    return (
                      <MobileCard
                        key={sv.id}
                        title={<span className="text-sm font-mono text-slate-200">#{sv.attributes.serial}</span>}
                        badge={
                          isLatest && (
                            <span className="shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-green-900/40 text-green-300">
                              {t('state.latest')}
                            </span>
                          )
                        }
                        fields={[
                          {
                            label: t('state.createdBy'),
                            value: sv.attributes['created-by'] || t('state.runner'),
                            valueClassName: sv.attributes['created-by'] ? 'text-slate-300' : 'text-slate-500',
                          },
                          {
                            label: t('state.run'),
                            value: runData ? (
                              <a href={`/workspaces/${workspaceId}/runs/${runData.id}`} className="text-brand-400 hover:text-brand-300">
                                {runData.id.replace('run-', '').slice(0, 8)}
                              </a>
                            ) : (
                              '—'
                            ),
                            valueClassName: 'text-slate-400',
                          },
                          {
                            label: t('state.size'),
                            value: sv.attributes.size > 0 ? `${(sv.attributes.size / 1024).toFixed(1)} KB` : '—',
                            valueClassName: 'text-slate-400',
                          },
                          {
                            label: t('state.created'),
                            value: sv.attributes['created-at'] ? new Date(sv.attributes['created-at']).toLocaleString() : '—',
                            valueClassName: 'text-slate-500',
                          },
                        ]}
                        actions={
                          <>
                            <button
                              onClick={() => downloadStateVersion(sv)}
                              className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200"
                            >
                              {t('actions.download')}
                            </button>
                            {!isLatest && perms['can-create-state-versions'] && (
                              <button
                                onClick={() => setConfirmStateAction({ action: 'rollback', sv })}
                                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-amber-900/40 hover:bg-amber-900/60 text-amber-300"
                              >
                                {t('actions.rollback')}
                              </button>
                            )}
                            {!isLatest && perms['can-update'] && (
                              <button
                                onClick={() => setConfirmStateAction({ action: 'delete', sv })}
                                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300"
                              >
                                {t('actions.delete')}
                              </button>
                            )}
                          </>
                        }
                      />
                    )
                  })}
                </MobileCardList>
              </>
            )}

            {/* Stack outputs (#1568) — the latest state's output values, for
                both engines. Keyed on the state-version count so a new version
                arriving over SSE re-reads them without a manual reload. */}
            <StackOutputsPanel workspaceId={workspaceId} refreshKey={stateVersions.length} />
          </div>
        )}

        {/* State Graph Tab (#765) */}
        {activeTab === 'state-graph' && workspace && (
          <StateGraphTab workspaceId={workspace.id} />
        )}

        {/* Cost Tab (#871) — current managed-infra cost from latest state */}
        {activeTab === 'cost' && workspace && (
          <CostPanel workspaceId={workspace.id} />
        )}

        {/* Architecture Tab (#1036) — AI critique of the deployed system from state */}
        {activeTab === 'architecture' && workspace && (
          <ArchitectureCritiquePanel workspaceId={workspace.id} refreshKey={archRefresh} />
        )}

        {/* Versions Tab (configuration versions) */}
        {activeTab === 'versions' && (
          <div>
            <div className="flex items-center justify-between mb-4">
              <p className="text-sm text-slate-400">
                {t('configurations.description')}
              </p>
              <div className="flex items-center gap-3">
                <span className="text-xs text-slate-500">{t('configurations.selectedCount', { count: cvSelected.size })}</span>
                <button
                  type="button"
                  onClick={compareSelectedCvs}
                  disabled={cvSelected.size !== 2 || cvDiffLoading}
                  className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {cvDiffLoading ? t('configurations.comparing') : t('configurations.compare')}
                </button>
              </div>
            </div>

            {cvLoading ? (
              <LoadingSpinner />
            ) : cvs.length === 0 ? (
              <EmptyState message={phaseWord('configurationsEmpty')} />
            ) : (
              <>
                {/* Desktop (md+): the table with per-row compare checkboxes. */}
                <div className="hidden md:block overflow-hidden rounded-xl border border-slate-800">
                  <table className="w-full text-sm">
                    <thead className="bg-slate-900/50 text-slate-400">
                      <tr>
                        <th className="w-8 px-2 py-3" aria-hidden />
                        <th className="px-4 py-3 text-start font-medium">{t('configurations.id')}</th>
                        <th className="px-4 py-3 text-start font-medium">{t('configurations.source')}</th>
                        <th className="px-4 py-3 text-start font-medium">{t('configurations.status')}</th>
                        <th className="px-4 py-3 text-start font-medium">{t('configurations.created')}</th>
                        <th className="px-4 py-3 text-end font-medium">{t('actions.download')}</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800">
                      {cvs.map(cv => {
                        const isCurrent = cv.id === cvCurrentId
                        const isSelected = cvSelected.has(cv.id)
                        const canDownload = cv.attributes.status === 'uploaded'
                        return (
                          <tr key={cv.id} className={isSelected ? 'bg-blue-900/20' : 'hover:bg-slate-900/30 transition-colors'}>
                            <td className="px-2 py-3 align-middle">
                              <input
                                type="checkbox"
                                aria-label={t('configurations.selectForCompare', { id: cv.id })}
                                checked={isSelected}
                                onChange={() => toggleCvSelected(cv.id)}
                                className="h-4 w-4"
                                disabled={!canDownload}
                              />
                            </td>
                            <td className="px-4 py-3 font-mono text-xs">
                              <span className="text-slate-200">{cv.id}</span>
                              {isCurrent && (
                                <span className="ms-2 inline-flex items-center rounded bg-green-900/40 px-1.5 py-0.5 text-xs font-medium text-green-300">
                                  {t('configurations.current')}
                                </span>
                              )}
                            </td>
                            <td className="px-4 py-3 text-slate-300">{cv.attributes.source}</td>
                            <td className="px-4 py-3 text-slate-400">{cv.attributes.status}</td>
                            <td className="px-4 py-3 text-slate-400">
                              {new Date(cv.attributes['created-at']).toLocaleString()}
                            </td>
                            <td className="px-4 py-3 text-end">
                              {canDownload ? (
                                <button
                                  type="button"
                                  onClick={() => downloadCv(cv.id)}
                                  className="text-blue-400 hover:text-blue-300 text-sm font-medium"
                                >
                                  {t('actions.download')}
                                </button>
                              ) : (
                                <span className="text-slate-600 text-sm">—</span>
                              )}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>

                {/* Mobile (< md): cards — the 6-column table is 529px wide and was
                    clipped by its overflow-hidden wrapper on a phone, hiding
                    Created + Download entirely. Cards keep every field plus the
                    compare checkbox and Download as tap targets. */}
                <MobileCardList>
                  {cvs.map(cv => {
                    const isCurrent = cv.id === cvCurrentId
                    const isSelected = cvSelected.has(cv.id)
                    const canDownload = cv.attributes.status === 'uploaded'
                    return (
                      <MobileCard
                        key={cv.id}
                        title={<span className="min-w-0 text-xs font-mono text-slate-200 break-all">{cv.id}</span>}
                        badge={
                          isCurrent && (
                            <span className="shrink-0 inline-flex items-center rounded bg-green-900/40 px-1.5 py-0.5 text-xs font-medium text-green-300">
                              {t('configurations.current')}
                            </span>
                          )
                        }
                        fields={[
                          { label: t('configurations.source'), value: cv.attributes.source },
                          { label: t('configurations.status'), value: cv.attributes.status, valueClassName: 'text-slate-400' },
                          {
                            label: t('configurations.created'),
                            value: new Date(cv.attributes['created-at']).toLocaleString(),
                            valueClassName: 'text-slate-400',
                          },
                        ]}
                        actions={
                          <>
                            <label className={`flex items-center gap-1.5 text-xs ${canDownload ? 'text-slate-400' : 'text-slate-600'}`}>
                              <input
                                type="checkbox"
                                aria-label={t('configurations.selectForCompare', { id: cv.id })}
                                checked={isSelected}
                                onChange={() => toggleCvSelected(cv.id)}
                                className="h-4 w-4"
                                disabled={!canDownload}
                              />
                              {t('configurations.compareLabel')}
                            </label>
                            {canDownload && (
                              <button
                                type="button"
                                onClick={() => downloadCv(cv.id)}
                                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200"
                              >
                                {t('actions.download')}
                              </button>
                            )}
                          </>
                        }
                      />
                    )
                  })}
                </MobileCardList>
              </>
            )}

            {cvDiffError && (
              <div className="mt-4">
                <ErrorBanner message={cvDiffError} />
              </div>
            )}

            {cvDiff && (
              <div className="mt-6 space-y-4">
                <div className="flex items-baseline justify-between">
                  <h3 className="text-lg font-medium text-slate-100">
                    {t('configurations.diff')} <span className="text-slate-500 text-sm font-normal">{t('configurations.filesChanged', { count: cvDiff['total-files-changed'] })}</span>
                  </h3>
                  <button
                    type="button"
                    onClick={() => { setCvDiff(null); setCvSelected(new Set()) }}
                    className="text-sm text-slate-400 hover:text-slate-200"
                  >
                    {t('actions.close')}
                  </button>
                </div>

                {cvDiff.oversized.length > 0 && (
                  <div className="rounded-md border border-amber-900/50 bg-amber-900/20 px-4 py-3 text-sm text-amber-200">
                    {t('configurations.oversized', { count: cvDiff.oversized.length, files: cvDiff.oversized.join(', ') })}
                  </div>
                )}

                {cvDiff.files.length === 0 ? (
                  <p className="text-slate-500 text-sm italic">
                    {t('configurations.noDiff')}
                  </p>
                ) : (
                  cvDiff.files.map(f => (
                    <div key={f.path} className="overflow-hidden rounded-lg border border-slate-800">
                      <div className="flex items-center gap-3 bg-slate-900/50 px-4 py-2 text-sm">
                        <span
                          className={
                            'inline-block w-20 text-center rounded px-1.5 py-0.5 text-xs font-medium ' +
                            (f.type === 'added' ? 'bg-green-900/50 text-green-300' :
                             f.type === 'removed' ? 'bg-red-900/50 text-red-300' :
                             f.type === 'binary-changed' ? 'bg-slate-700 text-slate-300' :
                             'bg-blue-900/50 text-blue-300')
                          }
                        >
                          {t(`configurations.diffType.${f.type}`)}
                        </span>
                        <span className="font-mono text-slate-200">{f.path}</span>
                      </div>
                      {f.diff ? (
                        <pre className="overflow-x-auto bg-slate-950 px-4 py-3 text-xs leading-relaxed text-slate-300">
                          {f.diff.split('\n').map((line, i) => (
                            <div
                              key={i}
                              className={
                                line.startsWith('+') && !line.startsWith('+++') ? 'text-green-300' :
                                line.startsWith('-') && !line.startsWith('---') ? 'text-red-300' :
                                line.startsWith('@@') ? 'text-cyan-400' :
                                'text-slate-400'
                              }
                            >
                              {line || '\u00a0'}
                            </div>
                          ))}
                        </pre>
                      ) : (
                        <p className="px-4 py-3 text-sm text-slate-500 italic">
                          {t('configurations.binaryChanged')}
                        </p>
                      )}
                    </div>
                  ))
                )}
              </div>
            )}
          </div>
        )}

        {/* Notifications Tab */}
        {activeTab === 'notifications' && (
          <div>
            {perms['can-update'] && (
              <div className="flex justify-end mb-4">
                <button
                  onClick={() => setShowAddNotif(!showAddNotif)}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors"
                >
                  {showAddNotif ? t('actions.cancel') : t('notifications.addNotification')}
                </button>
              </div>
            )}

            {showAddNotif && (
              <form onSubmit={handleAddNotification} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="notif-name" className="block text-sm font-medium text-slate-300 mb-1">{t('notifications.name')}</label>
                    <input id="notif-name" type="text" value={notifName} onChange={(e) => setNotifName(e.target.value)} required placeholder={t('notifications.namePlaceholder')}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="notif-type" className="block text-sm font-medium text-slate-300 mb-1">{t('notifications.destinationType')}</label>
                    <select id="notif-type" value={notifType} onChange={(e) => setNotifType(e.target.value as 'generic' | 'slack' | 'email')}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      <option value="generic">{t('notifications.typeGeneric')}</option>
                      <option value="slack">Slack</option>
                      <option value="email">{t('notifications.typeEmail')}</option>
                    </select>
                  </div>
                  {notifType !== 'email' && (
                    <div>
                      <label htmlFor="notif-url" className="block text-sm font-medium text-slate-300 mb-1">{t('notifications.url')}</label>
                      <input id="notif-url" type="url" value={notifUrl} onChange={(e) => setNotifUrl(e.target.value)} required
                        placeholder={notifType === 'slack' ? 'https://hooks.slack.com/services/...' : 'https://example.com/webhook'}
                        className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                    </div>
                  )}
                  {notifType === 'generic' && (
                    <div>
                      <label htmlFor="notif-token" className="block text-sm font-medium text-slate-300 mb-1">{t('notifications.hmacToken')}</label>
                      <input id="notif-token" type="password" value={notifToken} onChange={(e) => setNotifToken(e.target.value)}
                        placeholder={t('notifications.signingSecret')}
                        className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                    </div>
                  )}
                  {notifType === 'email' && (
                    <div className="sm:col-span-2">
                      <label htmlFor="notif-emails" className="block text-sm font-medium text-slate-300 mb-1">{t('notifications.emailAddresses')}</label>
                      <input id="notif-emails" type="text" value={notifEmails} onChange={(e) => setNotifEmails(e.target.value)} required
                        placeholder="team@example.com, ops@example.com"
                        className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                    </div>
                  )}
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-2">{t('notifications.triggerEvents')}</label>
                  <div className="flex flex-wrap gap-2">
                    {ALL_TRIGGERS.map(trg => (
                      <label key={trg} className="flex items-center gap-1.5 cursor-pointer">
                        <input type="checkbox" checked={notifTriggers.has(trg)} onChange={() => toggleTrigger(trg)}
                          className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                        <span className="text-xs text-slate-300">{trg}</span>
                      </label>
                    ))}
                  </div>
                </div>
                <button type="submit" disabled={addingNotif} className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {addingNotif ? t('actions.creating') : t('notifications.createNotification')}
                </button>
              </form>
            )}

            {notifLoading ? (
              <LoadingSpinner />
            ) : notifications.length === 0 ? (
              <EmptyState message={t('notifications.empty')} />
            ) : (
              <div className="space-y-3">
                {notifications.map((nc) => {
                  const a = nc.attributes
                  const responses = a['delivery-responses'] || []
                  const lastResponse = responses.length > 0 ? responses[responses.length - 1] : null
                  const isExpanded = expandedNotifId === nc.id

                  return (
                    <div key={nc.id} className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                      <div className="px-4 py-3 flex flex-col gap-3 sm:flex-row sm:items-center">
                        <div className="min-w-0 sm:flex-1">
                          <div className="flex items-center gap-2 mb-1">
                            <span className="text-sm font-medium text-slate-200 truncate">{a.name}</span>
                            <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${destTypeBadge(a['destination-type'])}`}>
                              {a['destination-type']}
                            </span>
                            <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                              a.enabled ? 'bg-green-900/50 text-green-300' : 'bg-slate-700 text-slate-400'
                            }`}>
                              {a.enabled ? t('common.enabled') : t('common.disabled')}
                            </span>
                          </div>
                          <div className="flex flex-wrap gap-1">
                            {a.triggers.map(trg => (
                              <span key={trg} className="inline-flex items-center px-1.5 py-0.5 rounded text-xs bg-slate-700 text-slate-300">{trg}</span>
                            ))}
                          </div>
                        </div>
                        <div className="flex flex-wrap items-center gap-2 sm:shrink-0">
                          {lastResponse && (
                            <span className={`text-xs ${lastResponse.success ? 'text-green-400' : 'text-red-400'}`}>
                              {lastResponse.success ? t('notifications.ok') : t('notifications.err', { status: lastResponse.status })}
                            </span>
                          )}
                          {perms['can-update'] && (
                            <>
                              <button onClick={() => handleToggleNotif(nc)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200">
                                {a.enabled ? t('actions.disable') : t('actions.enable')}
                              </button>
                              <button onClick={() => handleVerifyNotif(nc.id)} disabled={verifyingId === nc.id}
                                className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 disabled:opacity-50">
                                {verifyingId === nc.id ? t('actions.sending') : t('notifications.verify')}
                              </button>
                            </>
                          )}
                          {responses.length > 0 && (
                            <button onClick={() => setExpandedNotifId(isExpanded ? null : nc.id)}
                              className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-300">
                              {isExpanded ? t('actions.hide') : t('notifications.history')}
                            </button>
                          )}
                          {perms['can-update'] && (
                            <button onClick={() => handleDeleteNotif(nc.id)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300">{t('actions.delete')}</button>
                          )}
                        </div>
                      </div>
                      {isExpanded && responses.length > 0 && (
                        <div className="border-t border-slate-700/50 px-4 py-2">
                          <h4 className="text-xs font-medium text-slate-400 mb-2">{t('notifications.deliveryHistory')}</h4>
                          <div className="space-y-1">
                            {[...responses].reverse().map((r, i) => (
                              <div key={i} className="flex items-center gap-3 text-xs">
                                <span className={r.success ? 'text-green-400' : 'text-red-400'}>
                                  {r.success ? t('notifications.ok') : t('notifications.fail')}
                                </span>
                                <span className="text-slate-400">{t('notifications.http', { status: r.status })}</span>
                                <span className="text-slate-500 truncate flex-1">{r.body}</span>
                                <span className="text-slate-600 shrink-0">{r.delivered_at ? new Date(r.delivered_at).toLocaleString() : ''}</span>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )}
        {/* Run Tasks Tab */}
        {activeTab === 'run-tasks' && (
          <div>
            {perms['can-update'] && (
              <div className="flex justify-end mb-4">
                <button
                  onClick={() => setShowAddRunTask(!showAddRunTask)}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors"
                >
                  {showAddRunTask ? t('actions.cancel') : t('runTasks.addRunTask')}
                </button>
              </div>
            )}

            {showAddRunTask && (
              <form onSubmit={handleAddRunTask} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="rt-name" className="block text-sm font-medium text-slate-300 mb-1">{t('runTasks.name')}</label>
                    <input id="rt-name" type="text" value={rtName} onChange={(e) => setRtName(e.target.value)} required placeholder={t('runTasks.namePlaceholder')}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="rt-url" className="block text-sm font-medium text-slate-300 mb-1">{t('runTasks.webhookUrl')}</label>
                    <input id="rt-url" type="url" value={rtUrl} onChange={(e) => setRtUrl(e.target.value)} required
                      placeholder="https://opa.example.com/check"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="rt-stage" className="block text-sm font-medium text-slate-300 mb-1">{t('runTasks.stage')}</label>
                    <select id="rt-stage" value={rtStage} onChange={(e) => setRtStage(e.target.value)}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      {ALL_STAGES.map(s => (
                        <option key={s} value={s}>{s.replace('_', ' ')}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label htmlFor="rt-enforcement" className="block text-sm font-medium text-slate-300 mb-1">{t('runTasks.enforcementLevel')}</label>
                    <select id="rt-enforcement" value={rtEnforcement} onChange={(e) => setRtEnforcement(e.target.value)}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      {ALL_ENFORCEMENT_LEVELS.map(l => (
                        <option key={l} value={l}>{l}</option>
                      ))}
                    </select>
                  </div>
                  <div className="sm:col-span-2">
                    <label htmlFor="rt-hmac" className="block text-sm font-medium text-slate-300 mb-1">{t('runTasks.hmacKey')}</label>
                    <input id="rt-hmac" type="password" value={rtHmacKey} onChange={(e) => setRtHmacKey(e.target.value)}
                      placeholder={t('runTasks.hmacKeyPlaceholder')}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                </div>
                <button type="submit" disabled={addingRunTask} className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {addingRunTask ? t('actions.creating') : t('runTasks.createRunTask')}
                </button>
              </form>
            )}

            {runTasksLoading ? (
              <LoadingSpinner />
            ) : runTasks.length === 0 ? (
              <EmptyState message={t('runTasks.empty')} />
            ) : (
              <div className="space-y-3">
                {runTasks.map((rt) => {
                  const a = rt.attributes
                  return (
                    <div key={rt.id} className="bg-slate-800/50 rounded-lg border border-slate-700/50 px-4 py-3 flex flex-col gap-3 sm:flex-row sm:items-center">
                      <div className="min-w-0 sm:flex-1">
                        {/* Below sm the name takes its own line (w-full) so the
                            badges wrap beneath it instead of squeezing it to an
                            ellipsis; sm+ they share one line and the name
                            truncates as before. */}
                        <div className="flex flex-wrap items-center gap-2 mb-1">
                          <span className="w-full sm:w-auto text-sm font-medium text-slate-200 break-words sm:truncate">{a.name}</span>
                          <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${stageBadge(a.stage)}`}>
                            {a.stage.replace('_', ' ')}
                          </span>
                          <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${enforcementBadge(a['enforcement-level'])}`}>
                            {a['enforcement-level']}
                          </span>
                          <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                            a.enabled ? 'bg-green-900/50 text-green-300' : 'bg-slate-700 text-slate-400'
                          }`}>
                            {a.enabled ? t('common.enabled') : t('common.disabled')}
                          </span>
                        </div>
                        <div className="text-xs text-slate-500 break-all sm:truncate">{a.url}</div>
                      </div>
                      {perms['can-update'] && (
                        <div className="flex items-center gap-2 sm:shrink-0">
                          <button onClick={() => handleToggleRunTask(rt)} className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200">
                            {a.enabled ? t('actions.disable') : t('actions.enable')}
                          </button>
                          <button
                            onClick={() => handleDeleteRunTask(rt.id)}
                            className="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300"
                          >
                            {t('actions.delete')}
                          </button>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )}

        {/* Run Triggers Tab — cross-workspace apply-fires-plan edges */}
        {activeTab === 'run-triggers' && (
          <div>
            <div className="flex items-baseline justify-between mb-1">
              <h3 className="text-lg font-semibold text-slate-200">{t('runTriggers.title')}</h3>
              {trgLoading && <span className="text-xs text-slate-500">{t('actions.loadingLower')}</span>}
            </div>
            <p className="text-xs text-slate-500 mb-6">
              {phaseWord('runTriggersDescription')}
            </p>

            {/* Inbound — source workspaces that trigger runs HERE */}
            <div className="mb-8">
              <h4 className="text-sm font-medium text-slate-300 mb-2">{t('runTriggers.inboundHeading')}</h4>
              {trgInbound.length === 0 ? (
                <p className="text-sm text-slate-500 italic">{t('runTriggers.inboundEmpty')}</p>
              ) : (
                <ul className="space-y-1">
                  {trgInbound.map((edge) => (
                    <li key={edge.id} className="flex items-center justify-between gap-3 rounded bg-slate-800/40 px-3 py-2 text-sm">
                      <div>
                        <a href={`/workspaces/${edge.sourceableId}`} className="text-brand-400 hover:text-brand-300 font-medium">
                          {edge.sourceableName || edge.sourceableId}
                        </a>
                      </div>
                      {perms['can-update'] && (
                        <button
                          type="button"
                          onClick={() => removeRunTrigger(edge.id)}
                          className="rounded px-2 py-1 text-xs font-medium bg-red-900/40 text-red-200 hover:bg-red-900/60"
                        >
                          {t('actions.remove')}
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              )}

              {perms['can-update'] && (
                <div className="mt-3" data-testid="run-trigger-picker">
                  <WorkspacePicker
                    placeholder={t('runTriggers.pickerPlaceholder')}
                    excludeIds={[workspaceId, ...trgInbound.map((e) => e.sourceableId)]}
                    busyId={trgAddingId}
                    disabled={trgAdding}
                    onSelect={(ws) => addRunTrigger(ws.id)}
                  />
                </div>
              )}
            </div>

            {/* Outbound — destination workspaces this one triggers */}
            <div>
              <h4 className="text-sm font-medium text-slate-300 mb-2">{t('runTriggers.outboundHeading')}</h4>
              {trgOutbound.length === 0 ? (
                <p className="text-sm text-slate-500 italic">{t('runTriggers.outboundEmpty')}</p>
              ) : (
                <ul className="space-y-1">
                  {trgOutbound.map((edge) => (
                    <li key={edge.id} className="rounded bg-slate-800/40 px-3 py-2 text-sm">
                      <a href={`/workspaces/${edge.workspaceId}`} className="text-brand-400 hover:text-brand-300 font-medium">
                        {edge.workspaceName || edge.workspaceId}
                      </a>
                      <span className="ms-2 text-xs text-slate-500">{t('runTriggers.destinationNote')}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}

        {/* Inventory Tab — the seven structures an ansible inventory has, each
            editable per row (#1967, #1968, #1969). The eighth was a settings
            row with its own VCS binding; it is gone, and what it held is now
            workspace columns edited on the Configuration tab (#1929).
            Rendered only when the workspace actually has an inventory; the
            panel owns its own data and its own `?inv=` sub-view, so the page
            passes nothing but the id. */}
        {activeTab === 'inventory' && <InventoryPanel workspaceId={workspaceId} />}

        {/* Sharing Tab — cross-workspace remote-state allowlist (#344, #349) */}
        {activeTab === 'access' && (
          <div>
            <ResourceAccessPanel kind="workspaces" id={workspaceId} />
          </div>
        )}

        {activeTab === 'sharing' && (
          <div>
            <div className="flex items-baseline justify-between mb-1">
              <h3 className="text-lg font-semibold text-slate-200">{t('sharing.title')}</h3>
              {rscLoading && <span className="text-xs text-slate-500">{t('actions.loadingLower')}</span>}
            </div>
            <p className="text-xs text-slate-500 mb-6">
              {t.rich('sharing.description', {
                code: () => <code className="text-slate-400">terraform_remote_state</code>,
                docs: (chunks) => (
                  <a href="https://github.com/mattrobinsonsre/terrapod/blob/main/docs/remote-state.md" className="text-brand-400 hover:text-brand-300 underline" target="_blank" rel="noopener noreferrer">{chunks}</a>
                ),
              })}
            </p>

            {/* Outbound — workspaces I share my state to */}
            <div className="mb-8">
              <h4 className="text-sm font-medium text-slate-300 mb-2">{t('sharing.outboundHeading')}</h4>
              {rscOutbound.length === 0 ? (
                <p className="text-sm text-slate-500 italic">{t('sharing.outboundEmpty')}</p>
              ) : (
                <ul className="space-y-1">
                  {rscOutbound.map((e) => (
                    <li key={e.id} className="flex items-center justify-between gap-3 rounded bg-slate-800/40 px-3 py-2 text-sm">
                      <div>
                        <a href={`/workspaces/${e.consumerId}`} className="text-brand-400 hover:text-brand-300 font-medium">
                          {e.consumerName || e.consumerId}
                        </a>
                        {e.createdBy && (
                          <span className="ms-2 text-xs text-slate-500">{t('sharing.grantedBy', { by: e.createdBy })}</span>
                        )}
                      </div>
                      {perms['can-update'] && (
                        <button
                          type="button"
                          onClick={() => revokeRemoteStateConsumer(e.id)}
                          className="rounded px-2 py-1 text-xs font-medium bg-red-900/40 text-red-200 hover:bg-red-900/60"
                        >
                          {t('sharing.revoke')}
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              )}

              {perms['can-update'] && (
                <div className="mt-3" data-testid="remote-state-consumer-picker">
                  <WorkspacePicker
                    placeholder={t('sharing.pickerPlaceholder')}
                    excludeIds={[workspaceId, ...rscOutbound.map((e) => e.consumerId)]}
                    busyId={rscAddingId}
                    disabled={rscAdding}
                    onSelect={(ws) => addRemoteStateConsumer(ws.id)}
                  />
                </div>
              )}
            </div>

            {/* Inbound — workspaces I read state from */}
            <div>
              <h4 className="text-sm font-medium text-slate-300 mb-2">{t('sharing.inboundHeading')}</h4>
              {rscInbound.length === 0 ? (
                <p className="text-sm text-slate-500 italic">{t('sharing.inboundEmpty')}</p>
              ) : (
                <ul className="space-y-1">
                  {rscInbound.map((e) => (
                    <li key={e.id} className="rounded bg-slate-800/40 px-3 py-2 text-sm">
                      <a href={`/workspaces/${e.producerId}`} className="text-brand-400 hover:text-brand-300 font-medium">
                        {e.producerName || e.producerId}
                      </a>
                      <span className="ms-2 text-xs text-slate-500">{t('sharing.producerNote')}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </main>
    </>
  )
}
