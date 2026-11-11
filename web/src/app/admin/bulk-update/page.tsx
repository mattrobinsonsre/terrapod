'use client'

import { useEffect, useState, type ReactNode } from 'react'
import { tieredHint } from '@/components/hint-with-detail'
import { engineWord } from '@/lib/phase-vocabulary'
import {
  EE_UNKNOWN,
  fetchAnsibleExecutionEnvironments,
  type AnsibleExecutionEnvironments,
} from '@/lib/ansible-ee'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { LabelsEditor } from '@/components/labels-editor'
import {
  FIELD_BY_ATTR,
  GROUPS,
  type GroupId,
} from '@/components/workspace-fields'
import { Group, type GroupCtx } from '@/components/workspace-field-groups'
import {
  StringListEditor,
  OidcAudiencesEditor,
  sanitizeOidcAudiences,
  type OidcAudiences,
  RunTaskTemplatesEditor,
  NotificationTemplatesEditor,
  type RunTaskSpec,
  type NotificationSpec,
} from '@/components/template-editors'
import { useOidcAudienceDefaults } from '@/lib/use-oidc-audience-defaults'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch, fetchAllPages, parseApiError } from '@/lib/api'
import { parseCollectionLines } from '@/lib/ansible-collections'

interface WorkspaceSummary {
  id: string
  name: string
  'execution-mode': string
  'execution-backend': string
  'terraform-version': string
  'agent-pool-id': string | null
  labels: Record<string, string>
}

interface SearchResult {
  matched: number
  workspaces: WorkspaceSummary[]
}

interface DiffEntry {
  id: string
  name: string
  diff: Record<string, { from: unknown; to: unknown } | unknown>
}

interface DryRunResult {
  dry_run: true
  matched: number
  would_change: DiffEntry[]
  unchanged: { id: string; name: string }[]
}

interface ApplyResult {
  dry_run: false
  matched: number
  applied: number
  changes: DiffEntry[]
  unchanged: { id: string; name: string }[]
  errors: { id?: string; name?: string; error: string }[]
}

interface AgentPool {
  id: string
  attributes: { name: string }
}

interface VCSConnection {
  id: string
  attributes: { name: string; provider: string }
}

const inputCls =
  'w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent disabled:opacity-50 disabled:cursor-not-allowed'
const labelCls = 'block text-sm font-medium text-slate-300 mb-1'

// The run-kind select's option value standing in for the API's AUTOMATIC,
// which the API itself spells as an empty string -- a spelling this page has
// already spent on "unchanged" (#2479). Not a word the API accepts, so a
// sentinel that escapes the mapping is refused with a 422 naming the field
// rather than stored as a run kind nothing can honour.
const AUTO_RUN_KIND = '__auto__'

function fmtVal(v: unknown, noneLabel: string): string {
  if (v === null || v === undefined) return noneLabel
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

export default function BulkUpdatePage() {
  const router = useRouter()
  const t = useTranslations('adminBulkUpdate')
  const tMode = useTranslations('common.autoApplyMode')
  // Reuses the workspace page's own strings (#1763) so a fleet-wide change and
  // the per-workspace one are described in the same words, in every locale.
  const tScan = useTranslations('workspaceDetail.securityScan')
  const tAi = useTranslations('workspaceDetail.aiSummary')
  const tWs = useTranslations('workspaceDetail')
  // Root-scoped, because a phase word's key is `phases.<engine>.words.<name>`.
  const tPhase = useTranslations()
  const tAd = useTranslations('adminAutodiscovery.form')
  // A generic yes/no pair that already exists in every locale.
  const tCommon = useTranslations('runDetail.common')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [pools, setPools] = useState<AgentPool[]>([])
  const [connections, setConnections] = useState<VCSConnection[]>([])

  // ---- Filter form state ----
  const [fLabels, setFLabels] = useState<Record<string, string>>({})
  const [fNamePrefix, setFNamePrefix] = useState('')
  const [fExecBackend, setFExecBackend] = useState('')
  const [fAgentPoolId, setFAgentPoolId] = useState('')
  const [fVcsConnId, setFVcsConnId] = useState('')
  const [fOwnerEmail, setFOwnerEmail] = useState('')
  const [fAll, setFAll] = useState(false)

  const [searching, setSearching] = useState(false)
  const [searchResult, setSearchResult] = useState<SearchResult | null>(null)

  // ---- Update form state ----
  const [uTfVersion, setUTfVersion] = useState('')
  const [uAnsibleVersion, setUAnsibleVersion] = useState('')
  const [uAnsibleEE, setUAnsibleEE] = useState('')
  const [envs, setEnvs] = useState<AnsibleExecutionEnvironments>(EE_UNKNOWN)
  // Which operations the matched workspaces have (#1966). Tri-state
  // '' | 'true' | 'false', as terragrunt-enabled below: a plain checkbox
  // cannot say "leave this alone", and the endpoint type-checks booleans
  // rather than coercing them.
  //
  // Fleet-settable deliberately: turning configure on across a matched set is
  // the operation an operator adopting Ansible actually performs, and doing it
  // one workspace at a time is what this page exists to avoid.
  const [uPlanApplyEnabled, setUPlanApplyEnabled] = useState('')
  const [uConfigureEnabled, setUConfigureEnabled] = useState('')
  // Which of those operations gets the prominent button (#2479). '' is
  // "unchanged" here as everywhere on this page, so the AUTOMATIC value --
  // which the API spells as an empty string -- needs an option value of its
  // own. `AUTO_RUN_KIND` is that sentinel, and it is deliberately not a word
  // the API would accept: if one ever leaks past the mapping below, the
  // endpoint answers 422 naming the field rather than storing it.
  const [uDefaultRunKind, setUDefaultRunKind] = useState('')
  // How a configure starts (#1974). Both opt-in, both meaningless without
  // `configure-enabled` above, and both bulk-settable because adopting Ansible
  // across a fleet means turning the trigger on for the workspaces that should
  // have it.
  const [uAutoConfigureEnabled, setUAutoConfigureEnabled] = useState('')
  const [uConfigureAfterApply, setUConfigureAfterApply] = useState('')
  const [uConfigureAllHosts, setUConfigureAllHosts] = useState('')
  // Which inventory SOURCES ansible may resolve on the runner (#2033). Both
  // opt-in, both inert without `configure-enabled`, and both fleet-settable
  // for the same reason the triggers above are: an operator adopting a
  // dynamic inventory does it across the workspaces that should have it.
  const [uDynamicInventoryEnabled, setUDynamicInventoryEnabled] = useState('')
  const [uInventoryScriptsEnabled, setUInventoryScriptsEnabled] = useState('')
  const [uAnsibleHostKeyChecking, setUAnsibleHostKeyChecking] = useState('')
  // BOTH explicitly false is the only combination that can never succeed for
  // any matched workspace, so it is the only one blocked here. Turning just
  // plan/apply off is legitimate for a set that has configure on, and the
  // endpoint answers that against each row — guessing at it client-side would
  // refuse a real fleet operation on a rule the client cannot evaluate.
  const uWouldLeaveNoOperations = uPlanApplyEnabled === 'false' && uConfigureEnabled === 'false'
  // The Ansible roots and run defaults (#1929).
  //
  // `config-directory` CANNOT use this form's `if (value.trim())` idiom. That
  // idiom reads an empty box as "the operator did not touch this", which is
  // only sound where the empty string is not itself a value — and '' is a
  // value here: it means the configuration version's root (#1966). So the
  // checkbox is what says "touch this at all", exactly as it is for the two
  // run defaults below. #1929 needed a THIRD state here, because null then
  // meant "no configure operation"; that declaration is `configure-enabled`
  // above now, so two states are all there are.
  const [uSetConfigDirectory, setUSetConfigDirectory] = useState(false)
  const [uConfigDirectory, setUConfigDirectory] = useState('')
  // Checkbox-gated for the same reason: '' is a real value on both of these
  // (no default playbook; the whole inventory), so there is no in-band way to
  // say "leave this alone" — same shape as var-files.
  const [uSetDefaultPlaybook, setUSetDefaultPlaybook] = useState(false)
  const [uDefaultPlaybook, setUDefaultPlaybook] = useState('')
  const [uSetDefaultLimit, setUSetDefaultLimit] = useState(false)
  const [uDefaultLimit, setUDefaultLimit] = useState('')
  const [uSetHardLimit, setUSetHardLimit] = useState(false)
  const [uHardLimit, setUHardLimit] = useState('')
  const [uExecBackend, setUExecBackend] = useState('')
  const [uExecMode, setUExecMode] = useState('')
  // '' means "leave unchanged"; anything else is an auto-apply mode (#1276).
  const [uAutoApplyMode, setUAutoApplyMode] = useState('')
  const [uAgentPoolId, setUAgentPoolId] = useState('')
  const [uResourceCpu, setUResourceCpu] = useState('')
  const [uResourceMemory, setUResourceMemory] = useState('')
  // The Ansible execution environment's own pair (#2154). Blank means "leave
  // this alone", as every field on this form does.
  const [uEeResourceCpu, setUEeResourceCpu] = useState('')
  const [uEeResourceMemory, setUEeResourceMemory] = useState('')
  const [uScanEnforcement, setUScanEnforcement] = useState('')
  const [uScanEngine, setUScanEngine] = useState('')
  const [uScanThreshold, setUScanThreshold] = useState('')
  const [uAiSummaryMode, setUAiSummaryMode] = useState('')
  const [uAiPolicyMode, setUAiPolicyMode] = useState('')
  const [uAiSummaryContext, setUAiSummaryContext] = useState('')
  const [uTerragruntEnabled, setUTerragruntEnabled] = useState('')
  const [uTerragruntVersion, setUTerragruntVersion] = useState('')
  const [uVcsWorkflow, setUVcsWorkflow] = useState('')
  const [uAutoMerge, setUAutoMerge] = useState('')
  const [uAutoMergeStrategy, setUAutoMergeStrategy] = useState('')
  const [uDriftEnabled, setUDriftEnabled] = useState('')
  const [uDriftInterval, setUDriftInterval] = useState('')
  const [uPlanExpiry, setUPlanExpiry] = useState('')
  const [uParallelism, setUParallelism] = useState('')
  const [uConfigureParallelism, setUConfigureParallelism] = useState('')
  const [uSlackChannel, setUSlackChannel] = useState('')
  const [uBindPlan, setUBindPlan] = useState('')
  const [uDebugMode, setUDebugMode] = useState('')
  const [uAllowForkPrPlans, setUAllowForkPrPlans] = useState('')
  const [uSetLabels, setUSetLabels] = useState(false)
  const [uLabels, setULabels] = useState<Record<string, string>>({})
  const [uSetVarFiles, setUSetVarFiles] = useState(false)
  const [uVarFiles, setUVarFiles] = useState<string[]>([])
  // An EMPTY list is a meaningful value on both of these — it clears every
  // inventory source / ignore entry — so, as with var-files, the checkbox is
  // what says "touch this at all" (#1929).
  const [uSetInventoryPaths, setUSetInventoryPaths] = useState(false)
  const [uInventoryPaths, setUInventoryPaths] = useState<string[]>([])
  const [uSetAnsibleCollections, setUSetAnsibleCollections] = useState(false)
  const [uAnsibleCollections, setUAnsibleCollections] = useState<string[]>([])
  // Only to tell the operator when the deployment publishes no issuer, so an
  // entry added here could never be minted for. No partition on this surface:
  // the value IS the override — unlike a workspace read, nothing is merged
  // into it — so there is nothing to subtract and nothing to inherit.
  const oidcDefaults = useOidcAudienceDefaults()

  // A checkbox gate, not a bare editor: an EMPTY map is a meaningful value
  // here — it clears every override, so each provider falls back to the
  // deployment's own configured audiences — and there is therefore no in-band
  // way to say "leave this alone" (#1901). Same shape as var-files, for the
  // same reason.
  const [uSetOidcAudiences, setUSetOidcAudiences] = useState(false)
  const [uOidcAudiences, setUOidcAudiences] = useState<OidcAudiences>({})
  const [uSetRunTasks, setUSetRunTasks] = useState(false)
  const [uRunTasks, setURunTasks] = useState<RunTaskSpec[]>([])
  const [uSetNotifications, setUSetNotifications] = useState(false)
  const [uNotifications, setUNotifications] = useState<NotificationSpec[]>([])

  const [submitting, setSubmitting] = useState(false)
  const [confirmApply, setConfirmApply] = useState(false)
  const [dryResult, setDryResult] = useState<DryRunResult | null>(null)
  const [applyResult, setApplyResult] = useState<ApplyResult | null>(null)

  useEffect(() => {
    if (!getAuthState()) {
      router.push('/login')
      return
    }
    if (!isAdmin()) {
      router.push('/')
      return
    }
    loadRefs()
  }, [router])

  async function loadRefs() {
    setLoading(true)
    try {
      const [poolsList, connsList] = await Promise.all([
        fetchAllPages<AgentPool>('/api/terrapod/v1/agent-pools').catch(() => [] as AgentPool[]),
        fetchAllPages<VCSConnection>('/api/terrapod/v1/vcs-connections').catch(() => [] as VCSConnection[]),
      ])
      setPools(poolsList)
      setConnections(connsList)
      // Its own call rather than `fetchAllPages`, which discards the `meta`
      // this needs; non-fatal like the two above.
      void fetchAnsibleExecutionEnvironments().then(setEnvs)
    } catch {
      // refs are convenience-only; the page still works with manual ids
    } finally {
      setLoading(false)
    }
  }

  function buildFilter(): Record<string, unknown> {
    if (fAll) return { all: true }
    const f: Record<string, unknown> = {}
    if (Object.keys(fLabels).length > 0) f.labels = fLabels
    if (fNamePrefix.trim()) f['name-prefix'] = fNamePrefix.trim()
    if (fExecBackend) f['execution-backend'] = fExecBackend
    if (fAgentPoolId) f['agent-pool-id'] = fAgentPoolId
    if (fVcsConnId) f['vcs-connection-id'] = fVcsConnId
    if (fOwnerEmail.trim()) f['owner-email'] = fOwnerEmail.trim()
    return f
  }

  function buildUpdate(): Record<string, unknown> {
    const u: Record<string, unknown> = {}
    // `engine-version` is the canonical name; `terraform-version` is the
    // #1559 alias the endpoint still normalises. Sending the canonical one
    // keeps the UI off a deprecated spelling.
    if (uTfVersion.trim()) u['engine-version'] = uTfVersion.trim()
    if (uAnsibleVersion.trim()) u['ansible-version'] = uAnsibleVersion.trim()
    if (uAnsibleEE.trim()) u['ansible-execution-environment'] = uAnsibleEE.trim()
    // Which operations the matched workspaces have (#1966). Either may be sent
    // on its own: the endpoint checks the MERGE against each matched
    // workspace's own row, so turning plan/apply off is legitimate across a set
    // that already has configure on, and only the pair that survives the write
    // being empty is refused.
    if (uPlanApplyEnabled) u['plan-apply-enabled'] = uPlanApplyEnabled === 'true'
    if (uConfigureEnabled) u['configure-enabled'] = uConfigureEnabled === 'true'
    // Mapped, not sent raw: the sentinel becomes the API's empty string, which
    // is how the preference is CLEARED across a matched set. The endpoint
    // checks the kind against each matched workspace's own merged row, so
    // naming one a workspace does not offer is refused per workspace rather
    // than silently stored -- which is also why turning an operation off in
    // the same submission wants this cleared alongside it.
    if (uDefaultRunKind)
      u['default-run-kind'] = uDefaultRunKind === AUTO_RUN_KIND ? '' : uDefaultRunKind
    if (uAutoConfigureEnabled) u['auto-configure-enabled'] = uAutoConfigureEnabled === 'true'
    if (uConfigureAfterApply) u['configure-after-apply'] = uConfigureAfterApply === 'true'
    if (uConfigureAllHosts) u['configure-all-hosts'] = uConfigureAllHosts === 'true'
    if (uDynamicInventoryEnabled)
      u['dynamic-inventory-enabled'] = uDynamicInventoryEnabled === 'true'
    if (uInventoryScriptsEnabled)
      u['inventory-scripts-enabled'] = uInventoryScriptsEnabled === 'true'
    if (uAnsibleHostKeyChecking)
      u['ansible-host-key-checking'] = uAnsibleHostKeyChecking === 'true'
    // Not `if (uConfigDirectory.trim())` — see the state comment: '' is a
    // value here (the configuration version's root), so the checkbox is what
    // says whether to write this at all. Never `null`: an explicit null on
    // this attribute is a 422 naming the field (#1966).
    if (uSetConfigDirectory) u['config-directory'] = uConfigDirectory.trim()
    if (uSetDefaultPlaybook) u['default-playbook'] = uDefaultPlaybook.trim()
    if (uSetDefaultLimit) u['default-limit'] = uDefaultLimit.trim()
    if (uSetHardLimit) u['hard-limit'] = uHardLimit.trim()
    if (uExecBackend) u['execution-backend'] = uExecBackend
    if (uExecMode) u['execution-mode'] = uExecMode
    // Send the mode, never the boolean — the endpoint 422s if both are set.
    if (uAutoApplyMode) u['auto-apply-mode'] = uAutoApplyMode
    if (uAgentPoolId) u['agent-pool-id'] = uAgentPoolId
    if (uResourceCpu.trim()) u['resource-cpu'] = uResourceCpu.trim()
    if (uResourceMemory.trim()) u['resource-memory'] = uResourceMemory.trim()
    if (uEeResourceCpu.trim()) u['ee-resource-cpu'] = uEeResourceCpu.trim()
    if (uEeResourceMemory.trim()) u['ee-resource-memory'] = uEeResourceMemory.trim()
    if (uScanEnforcement) u['security-scan-enforcement'] = uScanEnforcement
    if (uScanEngine) u['security-scan-engine'] = uScanEngine
    if (uScanThreshold) u['security-scan-severity-threshold'] = uScanThreshold
    if (uAiSummaryMode) u['ai-summary-mode'] = uAiSummaryMode
    if (uAiPolicyMode) u['ai-policy-mode'] = uAiPolicyMode
    if (uAiSummaryContext) u['ai-summary-context'] = uAiSummaryContext
    // A tri-state '' | 'true' | 'false': a plain checkbox cannot say "leave
    // this alone", and the endpoint type-checks booleans rather than coercing.
    if (uTerragruntEnabled) u['terragrunt-enabled'] = uTerragruntEnabled === 'true'
    if (uTerragruntVersion.trim()) u['terragrunt-version'] = uTerragruntVersion.trim()
    if (uVcsWorkflow) u['vcs-workflow'] = uVcsWorkflow
    if (uAutoMerge) u['auto-merge'] = uAutoMerge === 'true'
    if (uAutoMergeStrategy) u['auto-merge-strategy'] = uAutoMergeStrategy
    if (uDriftEnabled) u['drift-detection-enabled'] = uDriftEnabled === 'true'
    if (uDriftInterval.trim()) u['drift-detection-interval-seconds'] = Number(uDriftInterval)
    if (uPlanExpiry.trim()) u['plan-expiry-seconds'] = Number(uPlanExpiry)
    if (uParallelism.trim()) u['parallelism'] = Number(uParallelism)
    // Empty means UNCHANGED here, as every box on this form does -- not "set to
    // inherit" (#2499). Resetting a workspace to inheriting is the single-workspace
    // form's job, where an empty box is the field's own value rather than a
    // not-filled-in marker shared with forty other fields.
    if (uConfigureParallelism.trim())
      u['configure-parallelism'] = Number(uConfigureParallelism)
    if (uSlackChannel.trim()) u['slack-channel'] = uSlackChannel.trim()
    if (uBindPlan) u['pulumi-bind-plan'] = uBindPlan === 'true'
    if (uDebugMode) u['debug-mode'] = uDebugMode === 'true'
    if (uAllowForkPrPlans) u['allow-fork-pr-plans'] = uAllowForkPrPlans === 'true'
    if (uSetLabels) u.labels = uLabels
    if (uSetVarFiles) u['var-files'] = uVarFiles.map((s) => s.trim()).filter(Boolean)
    if (uSetInventoryPaths)
      u['inventory-paths'] = uInventoryPaths.map((s) => s.trim()).filter(Boolean)
    if (uSetAnsibleCollections)
      u['ansible-collections'] = parseCollectionLines(uAnsibleCollections)
    // Blank audience rows are refused by the server rather than dropped, and
    // so is a provider whose list came out empty — removing the key is the way
    // to stop overriding one. An empty row left in the editor would otherwise
    // 422 the whole fleet update.
    if (uSetOidcAudiences) u['oidc-audiences'] = sanitizeOidcAudiences(uOidcAudiences)
    if (uSetRunTasks) u['run-tasks'] = uRunTasks
    if (uSetNotifications) u['notification-configurations'] = uNotifications
    return u
  }

  async function handleSearch(e: React.FormEvent) {
    e.preventDefault()
    setSearching(true)
    setError('')
    setSearchResult(null)
    try {
      const res = await apiFetch('/api/terrapod/v1/workspaces/actions/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filter: buildFilter() }),
      })
      if (!res.ok) {
        // `parseApiError`, not `data.detail` (#2413 F2): a FastAPI validation
        // error carries `detail` as an ARRAY, which renders as [object Object]
        // through a bare `||`. The helper knows all four shapes the API emits.
        throw new Error(await parseApiError(res, t('errors.search')))
      }
      setSearchResult(await res.json())
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.search'))
    } finally {
      setSearching(false)
    }
  }

  async function runBulkUpdate(isDryRun: boolean) {
    setSubmitting(true)
    setError('')
    setDryResult(null)
    setApplyResult(null)
    try {
      const res = await apiFetch('/api/terrapod/v1/workspaces/actions/bulk-update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filter: buildFilter(),
          update: buildUpdate(),
          dry_run: isDryRun,
        }),
      })
      if (!res.ok) {
        throw new Error(await parseApiError(res, t('errors.bulkUpdate')))
      }
      const json = await res.json()
      if (json.dry_run) setDryResult(json as DryRunResult)
      else setApplyResult(json as ApplyResult)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.bulkUpdate'))
    } finally {
      setSubmitting(false)
      setConfirmApply(false)
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    // Submit is Apply only — Preview is a separate button that calls
    // runBulkUpdate(true) directly, so there is no mode to read here.
    // Destructive apply requires an explicit confirm click.
    if (!confirmApply) {
      setConfirmApply(true)
      return
    }
    runBulkUpdate(false)
  }

  function renderDiffTable(entries: DiffEntry[], title: string) {
    if (entries.length === 0) return null
    return (
      <div className="mb-6">
        <h3 className="text-sm font-semibold text-slate-200 mb-2">
          {title} ({entries.length})
        </h3>
        <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-700/50 text-start text-xs text-slate-400 uppercase">
                <th className="px-4 py-2">{t('diff.colWorkspace')}</th>
                <th className="px-4 py-2">{t('diff.colChanges')}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-700/30">
              {entries.map((e) => (
                <tr key={e.id}>
                  <td className="px-4 py-3 text-slate-200 align-top">{e.name}</td>
                  <td className="px-4 py-3">
                    <ul className="space-y-1">
                      {Object.entries(e.diff).map(([k, v]) => {
                        const fv = v as { from?: unknown; to?: unknown }
                        const isFromTo =
                          v !== null &&
                          typeof v === 'object' &&
                          'from' in (v as object) &&
                          'to' in (v as object)
                        return (
                          <li key={k} className="font-mono text-xs">
                            <span className="text-slate-400">{k}</span>:{' '}
                            {isFromTo ? (
                              <>
                                <span className="text-red-300">{fmtVal(fv.from, t('none'))}</span>
                                <span className="text-slate-500"> &rarr; </span>
                                <span className="text-green-300">{fmtVal(fv.to, t('none'))}</span>
                              </>
                            ) : (
                              <span className="text-green-300">{fmtVal(v, t('none'))}</span>
                            )}
                          </li>
                        )
                      })}
                    </ul>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    )
  }

  /**
   * The gate booleans this form can explicitly turn OFF, keyed as the API
   * names the attribute.
   *
   * Keyed that way so `gateOff` below can read each field's `dependsOn` out of
   * the shared registry rather than from a hand-kept list of gate/dependent
   * pairs: a new `dependsOn` declared in `workspace-fields.ts` is honoured
   * here the moment it is declared, which is the point of the registry holding
   * the gate at all. Every tri-state boolean on this surface is listed, not
   * only the ones that happen to gate something today.
   */
  const booleanValues: Record<string, string> = {
    'plan-apply-enabled': uPlanApplyEnabled,
    'configure-enabled': uConfigureEnabled,
    'auto-configure-enabled': uAutoConfigureEnabled,
    'configure-after-apply': uConfigureAfterApply,
    'configure-all-hosts': uConfigureAllHosts,
    'dynamic-inventory-enabled': uDynamicInventoryEnabled,
    'inventory-scripts-enabled': uInventoryScriptsEnabled,
    'ansible-host-key-checking': uAnsibleHostKeyChecking,
    'terragrunt-enabled': uTerragruntEnabled,
    'auto-merge': uAutoMerge,
    'drift-detection-enabled': uDriftEnabled,
    'pulumi-bind-plan': uBindPlan,
    'debug-mode': uDebugMode,
    'allow-fork-pr-plans': uAllowForkPrPlans,
  }

  /**
   * Whether a field's gate is explicitly OFF here, so the control renders
   * disabled-but-visible (#2413 F20).
   *
   * Deliberately NOT the shared `dependencyMet`/`fieldGateMet`, and the
   * difference is the whole reason this exists. Those read an ABSENT gate as
   * off, which is right on create and edit -- a workspace's
   * `configure-enabled` is always a concrete boolean there -- and wrong here,
   * because this form's selects are TRI-STATE and '' means "leave this alone".
   * Handed '', `dependencyMet` returns false, so all thirteen Configure
   * dependents would be disabled whenever Configure itself is untouched: that
   * forbids the commonest fleet operation there is -- "I am not changing
   * configure-enabled, but I do want to set the playbook across the fleet".
   *
   * So only an explicit 'false' disables a dependent. A gate this form does
   * not offer at all (`vcs-connection-id`) can never be explicitly false, so
   * its dependents stay editable -- correct, because each matched workspace
   * has its own connection and the server merges the write against that row.
   */
  const gateOff = (attr: string): boolean => {
    const dep = FIELD_BY_ATTR[attr]?.dependsOn
    return dep !== undefined && booleanValues[dep] === 'false'
  }

  /**
   * What `Group` reads off a `GroupCtx` -- which is `engine` and nothing else.
   *
   * `Group`'s only use of the context is dropping a section whose every field
   * belongs to another engine. The matched set here is heterogeneous BY
   * CONSTRUCTION: one filter can match Terraform and Pulumi workspaces at
   * once, so there is no honest engine value to pass, and '' is the honest
   * absence of one. Measured against the table: every group holds at least one
   * engine-agnostic field, so no section is dropped by it; and `Group` passes
   * `children` straight through, so the per-field engine filtering `Field`
   * applies never reaches the controls below.
   *
   * The remaining members are what the type requires and this surface does not
   * use, because the controls stay the page's own: `Field` was measured
   * incompatible here on three counts it has no room for -- a tri-state
   * "unchanged" value with no concrete value to show, a `uSet*` opt-in
   * checkbox whose sentence is the only label six fields have, and a payload
   * that must omit a key entirely rather than send a cleared one.
   */
  const groupCtx: GroupCtx = {
    mode: 'create',
    values: {},
    onChange: () => {},
    engine: '',
    label: (attr) => attr,
  }

  /** A section's own hint, rendered by `Group` above its grid. */
  const groupHint = (text: string) => <p className="text-xs text-slate-500 mb-3">{text}</p>

  const groupDescription: Partial<Record<GroupId, ReactNode>> = {
    operations: groupHint(tWs('fields.operationsHint')),
    ansible: groupHint(tAd('ansibleConfigureHint')),
  }

  /**
   * The fleet form's controls, keyed by the group the shared registry puts
   * each attribute in (#2413 F4).
   *
   * Keyed rather than written out in order so the render below walks `GROUPS`
   * itself and cannot drift from the table's order -- the same shape the
   * create form uses. `identity` is absent and that is not an omission: `name`
   * and `engine` are `NEVER_BULK_SETTABLE` (identity, not settings), so this
   * surface contributes nothing to that group, and the render filters it out
   * rather than printing a heading over an empty grid.
   *
   * Within a group the order is the registry's own, which is what makes the
   * four forms read alike -- and incidentally what puts the orchestrator's CPU
   * and memory requests back beside each other (#2413 F3).
   *
   * `min-w-0` on every wrapper: a grid item has `min-width: auto`, so a select
   * whose longest option is a long translated string pushes the row past the
   * viewport however `w-full` the control is.
   */
  const groupBody: Partial<Record<GroupId, ReactNode>> = {
    execution: (
      <>
        <div className="min-w-0">
          <label className={labelCls}>{t('update.executionMode')}</label>
          <select
            value={uExecMode}
            onChange={(e) => setUExecMode(e.target.value)}
            className={inputCls}
          >
            <option value="">{t('unchangedOption')}</option>
            <option value="local">local</option>
            <option value="agent">agent</option>
          </select>
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{t('update.executionBackend')}</label>
          <select
            value={uExecBackend}
            onChange={(e) => setUExecBackend(e.target.value)}
            className={inputCls}
          >
            <option value="">{t('unchangedOption')}</option>
            <option value="tofu">tofu</option>
            <option value="terraform">terraform</option>
          </select>
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{t('update.terraformVersion')}</label>
          <input
            type="text"
            value={uTfVersion}
            onChange={(e) => setUTfVersion(e.target.value)}
            placeholder={t('unchanged')}
            className={inputCls}
          />
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{t('update.autoApply')}</label>
          <select
            aria-label={t('update.autoApply')}
            value={uAutoApplyMode}
            onChange={(e) => setUAutoApplyMode(e.target.value)}
            className={inputCls}
          >
            <option value="">{t('unchangedOption')}</option>
            <option value="never">{tMode('never')}</option>
            <option value="always">{tMode('always')}</option>
            <option value="create">{tMode('create')}</option>
            <option value="create_update">{tMode('createUpdate')}</option>
          </select>
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{tAd('terragruntEnabled')}</label>
          <select
            value={uTerragruntEnabled}
            onChange={(e) => setUTerragruntEnabled(e.target.value)}
            className={inputCls}
          >
            <option value="">{t('unchanged')}</option>
            <option value="true">{tCommon('yes')}</option>
            <option value="false">{tCommon('no')}</option>
          </select>
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{tAd('terragruntVersion')}</label>
          <input
            type="text"
            value={uTerragruntVersion}
            onChange={(e) => setUTerragruntVersion(e.target.value)}
            placeholder={t('unchanged')}
            disabled={gateOff('terragrunt-version')}
            className={inputCls}
          />
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{tWs('fields.bindPlan')}</label>
          <select
            value={uBindPlan}
            onChange={(e) => setUBindPlan(e.target.value)}
            className={inputCls}
          >
            <option value="">{t('unchanged')}</option>
            <option value="true">{tCommon('yes')}</option>
            <option value="false">{tCommon('no')}</option>
          </select>
        </div>
        <div className="min-w-0">
          <label className={labelCls} title={tWs('fields.parallelismTitle')}>
            {tWs('fields.parallelism')}
          </label>
          <input
            type="number"
            min={1}
            max={256}
            value={uParallelism}
            onChange={(e) => setUParallelism(e.target.value)}
            placeholder={t('unchanged')}
            className={inputCls}
          />
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{tWs('fields.configureParallelism')}</label>
          <input
            type="number"
            min={1}
            max={256}
            value={uConfigureParallelism}
            onChange={(e) => setUConfigureParallelism(e.target.value)}
            placeholder={t('unchanged')}
            className={inputCls}
          />
          {tieredHint(tWs, 'fields.configureParallelismTitle')}
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{t('update.cpuRequest')}</label>
          <input
            type="text"
            value={uResourceCpu}
            onChange={(e) => setUResourceCpu(e.target.value)}
            placeholder={t('unchanged')}
            className={inputCls}
          />
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{t('update.memoryRequest')}</label>
          <input
            type="text"
            value={uResourceMemory}
            onChange={(e) => setUResourceMemory(e.target.value)}
            placeholder={t('unchanged')}
            className={inputCls}
          />
        </div>
      </>
    ),

    operations: (
      <>
        <div className="min-w-0">
          <label className={labelCls} htmlFor="bu-plan-apply-enabled">
            {tWs('fields.planApplyEnabled')}
          </label>
          <select
            id="bu-plan-apply-enabled"
            value={uPlanApplyEnabled}
            onChange={(e) => setUPlanApplyEnabled(e.target.value)}
            className={inputCls}
          >
            <option value="">{t('unchangedOption')}</option>
            <option value="true">{tCommon('yes')}</option>
            <option value="false">{tCommon('no')}</option>
          </select>
          <p className="mt-1 text-xs text-slate-400">{tWs('fields.planApplyEnabledHint')}</p>
        </div>
        <div className="min-w-0">
          <label className={labelCls} htmlFor="bu-configure-enabled">
            {tWs('fields.configureEnabled')}
          </label>
          <select
            id="bu-configure-enabled"
            value={uConfigureEnabled}
            onChange={(e) => setUConfigureEnabled(e.target.value)}
            className={inputCls}
          >
            <option value="">{t('unchangedOption')}</option>
            <option value="true">{tCommon('yes')}</option>
            <option value="false">{tCommon('no')}</option>
          </select>
          <p className="mt-1 text-xs text-slate-400">{tWs('fields.configureEnabledHint')}</p>
        </div>
        <div className="min-w-0">
          <label className={labelCls} htmlFor="bu-default-run-kind">
            {tWs('fields.defaultRunKind')}
          </label>
          <select
            id="bu-default-run-kind"
            value={uDefaultRunKind}
            onChange={(e) => setUDefaultRunKind(e.target.value)}
            className={inputCls}
          >
            <option value="">{t('unchangedOption')}</option>
            <option value={AUTO_RUN_KIND}>{tWs('fields.defaultRunKindAuto')}</option>
            {/* The same labels the run button itself uses, as on the workspace
                forms. The two engine-specific ones take the DEFAULT engine's
                word deliberately: a matched set can span engines, so there is
                no one engine to ask, and the platform's default vocabulary is
                what every other engine-agnostic surface shows. */}
            <option value="full">{tWs('runs.queueFullRun')}</option>
            <option value="plan_apply">{engineWord(tPhase, null, 'queueRun')}</option>
            <option value="configure">{tWs('runs.queueConfigure')}</option>
            <option value="plan">{engineWord(tPhase, null, 'queuePlan')}</option>
          </select>
          <p className="mt-1 text-xs text-slate-400">
            {tieredHint(tWs, 'fields.defaultRunKindTitle')}
          </p>
        </div>
        <div className="min-w-0">
          <label className={labelCls} htmlFor="bu-auto-configure-enabled">
            {tWs('fields.autoConfigureEnabled')}
          </label>
          <select
            id="bu-auto-configure-enabled"
            value={uAutoConfigureEnabled}
            onChange={(e) => setUAutoConfigureEnabled(e.target.value)}
            disabled={gateOff('auto-configure-enabled')}
            className={inputCls}
          >
            <option value="">{t('unchangedOption')}</option>
            <option value="true">{tCommon('yes')}</option>
            <option value="false">{tCommon('no')}</option>
          </select>
          <p className="mt-1 text-xs text-slate-400">{tWs('fields.autoConfigureEnabledHint')}</p>
        </div>
        <div className="min-w-0">
          <label className={labelCls} htmlFor="bu-configure-after-apply">
            {tWs('fields.configureAfterApply')}
          </label>
          <select
            id="bu-configure-after-apply"
            value={uConfigureAfterApply}
            onChange={(e) => setUConfigureAfterApply(e.target.value)}
            disabled={gateOff('configure-after-apply')}
            className={inputCls}
          >
            <option value="">{t('unchangedOption')}</option>
            <option value="true">{tCommon('yes')}</option>
            <option value="false">{tCommon('no')}</option>
          </select>
          <p className="mt-1 text-xs text-slate-400">{tWs('fields.configureAfterApplyHint')}</p>
        </div>
        <div className="min-w-0">
          <label className={labelCls} htmlFor="bu-configure-all-hosts">
            {tWs('fields.configureAllHosts')}
          </label>
          <select
            id="bu-configure-all-hosts"
            value={uConfigureAllHosts}
            onChange={(e) => setUConfigureAllHosts(e.target.value)}
            disabled={gateOff('configure-all-hosts')}
            className={inputCls}
          >
            <option value="">{t('unchangedOption')}</option>
            <option value="true">{tCommon('yes')}</option>
            <option value="false">{tCommon('no')}</option>
          </select>
          <p className="mt-1 text-xs text-slate-400">{tWs('fields.configureAllHostsHint')}</p>
        </div>
        {uWouldLeaveNoOperations && (
          /* Guaranteed 422 for every matched workspace, so both submits are
             blocked below and this is the sentence that explains it. Inside
             the Operations grid, spanning it, because it is about the pair of
             controls directly above rather than about the form.
             Wrapped in a `<div>` because `Group`'s grid is a `<dl>`, which
             permits `div` and not `p` -- the same wrapper every field already
             renders. */
          <div className="sm:col-span-2 min-w-0">
            <p
              className="rounded-lg border border-red-500/50 bg-red-500/10 p-2 text-xs text-red-300"
              data-testid="bu-operations-none"
            >
              {t('update.operationsNone')}
            </p>
          </div>
        )}
      </>
    ),

    ansible: (
      <>
        <div className="min-w-0">
          <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
            <input
              type="checkbox"
              checked={uSetConfigDirectory}
              onChange={(e) => setUSetConfigDirectory(e.target.checked)}
              disabled={gateOff('config-directory')}
            />
            {t('update.setConfigDirectory')}
          </label>
          {/* A checkbox plus a box, not a select: '' is a real value
              (the configuration version's root), so the gate has to be
              out of band -- the same shape as the two run defaults
              below. The third option #1929 needed here went with the
              nullable column (#1966). */}
          {uSetConfigDirectory && (
            <>
              <input
                type="text"
                value={uConfigDirectory}
                onChange={(e) => setUConfigDirectory(e.target.value)}
                placeholder={tWs('fields.configDirectoryPlaceholder')}
                aria-label={tWs('fields.configDirectory')}
                disabled={gateOff('config-directory')}
                className={inputCls}
              />
              {tieredHint(tWs, 'fields.configDirectoryTitle')}
            </>
          )}
        </div>
        <div className="min-w-0">
          <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
            <input
              type="checkbox"
              checked={uSetDefaultPlaybook}
              onChange={(e) => setUSetDefaultPlaybook(e.target.checked)}
              disabled={gateOff('default-playbook')}
            />
            {t('update.setDefaultPlaybook')}
          </label>
          {uSetDefaultPlaybook && (
            <input
              type="text"
              value={uDefaultPlaybook}
              onChange={(e) => setUDefaultPlaybook(e.target.value)}
              placeholder={tWs('fields.defaultPlaybookPlaceholder')}
              aria-label={tWs('fields.defaultPlaybook')}
              title={tWs('fields.defaultPlaybookTitle')}
              disabled={gateOff('default-playbook')}
              className={inputCls}
            />
          )}
        </div>
        <div className="min-w-0">
          <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
            <input
              type="checkbox"
              checked={uSetDefaultLimit}
              onChange={(e) => setUSetDefaultLimit(e.target.checked)}
              disabled={gateOff('default-limit')}
            />
            {t('update.setDefaultLimit')}
          </label>
          {uSetDefaultLimit && (
            <input
              type="text"
              value={uDefaultLimit}
              onChange={(e) => setUDefaultLimit(e.target.value)}
              placeholder={tWs('fields.defaultLimitPlaceholder')}
              aria-label={tWs('fields.defaultLimit')}
              title={tWs('fields.defaultLimitTitle')}
              disabled={gateOff('default-limit')}
              className={inputCls}
            />
          )}
        </div>
          <div className="min-w-0">
          <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
            <input
              type="checkbox"
              checked={uSetHardLimit}
              onChange={(e) => setUSetHardLimit(e.target.checked)}
              disabled={gateOff('hard-limit')}
            />
            {t('update.setHardLimit')}
          </label>
          {uSetHardLimit && (
            <>
              <input
                type="text"
                value={uHardLimit}
                onChange={(e) => setUHardLimit(e.target.value)}
                placeholder={tWs('fields.hardLimitPlaceholder')}
                aria-label={tWs('fields.hardLimit')}
                disabled={gateOff('hard-limit')}
                className={inputCls}
              />
              {tieredHint(tWs, 'fields.hardLimitTitle')}
            </>
          )}
        </div>
        <div className="min-w-0 sm:col-span-2">
          <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
            <input
              type="checkbox"
              checked={uSetInventoryPaths}
              onChange={(e) => setUSetInventoryPaths(e.target.checked)}
              disabled={gateOff('inventory-paths')}
            />
            {t('update.setInventoryPaths')}
          </label>
          {uSetInventoryPaths && (
            <fieldset disabled={gateOff('inventory-paths')} className="min-w-0 border-0 p-0 m-0 space-y-2">
              <p className="text-xs text-slate-400">{tWs('fields.inventoryPathsHint')}</p>
              <StringListEditor
                values={uInventoryPaths}
                onChange={setUInventoryPaths}
                placeholder={tWs('fields.inventoryPathsPlaceholder')}
                addLabel={tWs('fields.inventoryPathsAdd')}
              />
            </fieldset>
          )}
        </div>
        {/* Which inventory sources ansible may resolve on the runner
            (#2033). Terrapod itself never runs either kind, in either
            state -- these say what the RUNNER is permitted to resolve. */}
        <div className="min-w-0">
          <label className={labelCls} htmlFor="bu-dynamic-inventory-enabled">
            {tWs('fields.dynamicInventoryEnabled')}
          </label>
          <select
            id="bu-dynamic-inventory-enabled"
            value={uDynamicInventoryEnabled}
            onChange={(e) => setUDynamicInventoryEnabled(e.target.value)}
            disabled={gateOff('dynamic-inventory-enabled')}
            className={inputCls}
          >
            <option value="">{t('unchangedOption')}</option>
            <option value="true">{tCommon('yes')}</option>
            <option value="false">{tCommon('no')}</option>
          </select>
          {tieredHint(tWs, 'fields.dynamicInventoryEnabledHint')}
        </div>
        <div className="min-w-0">
          <label className={labelCls} htmlFor="bu-inventory-scripts-enabled">
            {tWs('fields.inventoryScriptsEnabled')}
          </label>
          <select
            id="bu-inventory-scripts-enabled"
            value={uInventoryScriptsEnabled}
            onChange={(e) => setUInventoryScriptsEnabled(e.target.value)}
            disabled={gateOff('inventory-scripts-enabled')}
            className={inputCls}
          >
            <option value="">{t('unchangedOption')}</option>
            <option value="true">{tCommon('yes')}</option>
            <option value="false">{tCommon('no')}</option>
          </select>
          {tieredHint(tWs, 'fields.inventoryScriptsEnabledHint')}
        </div>
        {/* Whether a configure verifies the host key of the hosts it
            targets (#2318). Tri-state like every select on this surface:
            the column defaults TRUE, but "unchanged" is a real value
            here, so a fleet write says nothing about a workspace the
            operator left alone. */}
        <div className="min-w-0">
          <label className={labelCls} htmlFor="bu-ansible-host-key-checking">
            {tWs('fields.ansibleHostKeyChecking')}
          </label>
          <select
            id="bu-ansible-host-key-checking"
            value={uAnsibleHostKeyChecking}
            onChange={(e) => setUAnsibleHostKeyChecking(e.target.value)}
            disabled={gateOff('ansible-host-key-checking')}
            className={inputCls}
          >
            <option value="">{t('unchangedOption')}</option>
            <option value="true">{tCommon('yes')}</option>
            <option value="false">{tCommon('no')}</option>
          </select>
          {tieredHint(tWs, 'fields.ansibleHostKeyCheckingHint')}
        </div>
        <div className="min-w-0">
          <label className={labelCls} title={tWs('fields.ansibleVersionTitle')}>
            {tWs('fields.ansibleVersion')}
          </label>
          {envs.listing === 'complete' && envs.lines.length > 0 ? (
            <select
              value={uAnsibleVersion}
              onChange={(e) => setUAnsibleVersion(e.target.value)}
              disabled={gateOff('ansible-version')}
              className={inputCls}
            >
              <option value="">{t('unchangedOption')}</option>
              {envs.lines.map((l) => (
                <option key={l} value={l}>{l}</option>
              ))}
            </select>
          ) : (
            /* `unknown` means the server accepts ANY line, so free text is
               correct -- an empty picker would block a fleet move the server
               would have allowed. */
            <input
              type="text"
              value={uAnsibleVersion}
              onChange={(e) => setUAnsibleVersion(e.target.value)}
              placeholder={t('unchanged')}
              title={tWs('fields.ansibleVersionUnconfirmed')}
              disabled={gateOff('ansible-version')}
              className={inputCls}
            />
          )}
        </div>
        <div className="min-w-0">
          <label className={labelCls}>
            {tWs('fields.ansibleExecutionEnvironment')}
          </label>
          {/* Always a picker: operator labels come from configuration, not
              from a registry, so they are authoritative even when the
              built-in listing is unknown. */}
          <select
            value={uAnsibleEE}
            onChange={(e) => setUAnsibleEE(e.target.value)}
            disabled={envs.labels.length === 0 || gateOff('ansible-execution-environment')}
            className={inputCls}
          >
            <option value="">
              {envs.labels.length === 0
                ? tWs('fields.ansibleExecutionEnvironmentNone')
                : t('unchangedOption')}
            </option>
            {envs.labels.map((l) => (
              <option key={l} value={l}>{l}</option>
            ))}
          </select>
          {/* Was a hover `title` on the label (#2432). The override it
              describes is the whole reason to set the field, so it belongs on
              screen rather than behind a pointer. */}
          {tieredHint(tWs, 'fields.ansibleExecutionEnvironmentTitle')}
        </div>
        <div className="min-w-0 sm:col-span-2">
          <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
            <input
              type="checkbox"
              className="rounded border-slate-600 bg-slate-800"
              checked={uSetAnsibleCollections}
              onChange={(e) => setUSetAnsibleCollections(e.target.checked)}
              disabled={gateOff('ansible-collections')}
            />
            {t('update.setAnsibleCollections')}
          </label>
          {uSetAnsibleCollections && (
            <fieldset
              disabled={gateOff('ansible-collections')}
              className="min-w-0 border-0 p-0 m-0 space-y-2"
            >
              {tieredHint(tWs, 'fields.ansibleCollectionsHint')}
              <StringListEditor
                values={uAnsibleCollections}
                onChange={setUAnsibleCollections}
                placeholder={tWs('fields.ansibleCollectionsPlaceholder')}
                addLabel={tWs('fields.ansibleCollectionsAdd')}
              />
            </fieldset>
          )}
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{t('update.eeCpuRequest')}</label>
          <input
            type="text"
            value={uEeResourceCpu}
            onChange={(e) => setUEeResourceCpu(e.target.value)}
            placeholder={t('unchanged')}
            disabled={gateOff('ee-resource-cpu')}
            className={inputCls}
          />
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{t('update.eeMemoryRequest')}</label>
          <input
            type="text"
            value={uEeResourceMemory}
            onChange={(e) => setUEeResourceMemory(e.target.value)}
            placeholder={t('unchanged')}
            disabled={gateOff('ee-resource-memory')}
            className={inputCls}
          />
        </div>
      </>
    ),

    pools: (
      <div className="min-w-0">
        <label className={labelCls}>{t('update.agentPool')}</label>
        {/* Sent as `agent-pool-id`, the single-pool spelling the endpoint
            accepts beside `agent-pool-ids` -- offering both would let an
            operator set two things that mean one thing. */}
        <select
          value={uAgentPoolId}
          onChange={(e) => setUAgentPoolId(e.target.value)}
          className={inputCls}
        >
          <option value="">{t('unchangedOption')}</option>
          {pools.map((p) => (
            <option key={p.id} value={p.id}>
              {p.attributes.name}
            </option>
          ))}
        </select>
      </div>
    ),

    vcs: (
      <>
        <div className="min-w-0">
          <label className={labelCls}>{tWs('fields.vcsWorkflow')}</label>
          <select
            value={uVcsWorkflow}
            onChange={(e) => setUVcsWorkflow(e.target.value)}
            disabled={gateOff('vcs-workflow')}
            className={inputCls}
          >
            <option value="">{t('unchanged')}</option>
            <option value="merge_then_apply">{tWs('fields.vcsWorkflowMergeThenApply')}</option>
            <option value="apply_then_merge">{tWs('fields.vcsWorkflowApplyThenMerge')}</option>
          </select>
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{tWs('fields.autoMerge')}</label>
          <select
            value={uAutoMerge}
            onChange={(e) => setUAutoMerge(e.target.value)}
            disabled={gateOff('auto-merge')}
            className={inputCls}
          >
            <option value="">{t('unchanged')}</option>
            <option value="true">{tCommon('yes')}</option>
            <option value="false">{tCommon('no')}</option>
          </select>
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{tWs('fields.autoMergeStrategy')}</label>
          <select
            value={uAutoMergeStrategy}
            onChange={(e) => setUAutoMergeStrategy(e.target.value)}
            disabled={gateOff('auto-merge-strategy')}
            className={inputCls}
          >
            <option value="">{t('unchanged')}</option>
            <option value="merge">merge</option>{/* i18n-ignore */}
            <option value="squash">squash</option>{/* i18n-ignore */}
            <option value="rebase">rebase</option>{/* i18n-ignore */}
          </select>
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{tWs('allowForkPrPlans.title')}</label>
          <select
            value={uAllowForkPrPlans}
            onChange={(e) => setUAllowForkPrPlans(e.target.value)}
            className={inputCls}
          >
            <option value="">{t('unchanged')}</option>
            <option value="true">{tCommon('yes')}</option>
            <option value="false">{tCommon('no')}</option>
          </select>
        </div>
      </>
    ),

    governance: (
      <>
        <div className="min-w-0">
          <label className={labelCls}>{tWs('drift.title')}</label>
          <select
            value={uDriftEnabled}
            onChange={(e) => setUDriftEnabled(e.target.value)}
            className={inputCls}
          >
            <option value="">{t('unchanged')}</option>
            <option value="true">{tCommon('yes')}</option>
            <option value="false">{tCommon('no')}</option>
          </select>
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{tWs('drift.checkInterval')}</label>
          <input
            type="number"
            min={0}
            value={uDriftInterval}
            onChange={(e) => setUDriftInterval(e.target.value)}
            placeholder={t('unchanged')}
            disabled={gateOff('drift-detection-interval-seconds')}
            className={inputCls}
          />
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{tWs('planExpiry.expireAfter')}</label>
          <input
            type="number"
            min={0}
            value={uPlanExpiry}
            onChange={(e) => setUPlanExpiry(e.target.value)}
            placeholder={t('unchanged')}
            className={inputCls}
          />
        </div>
      </>
    ),

    ai: (
      <>
        <div className="min-w-0">
          <label className={labelCls}>{tAi('title')}</label>
          <select
            value={uAiSummaryMode}
            onChange={(e) => setUAiSummaryMode(e.target.value)}
            className={inputCls}
          >
            <option value="">{t('unchanged')}</option>
            <option value="default">{tAi('modeDefault')}</option>
            <option value="enabled">{tAi('modeEnabled')}</option>
            <option value="disabled">{tAi('modeDisabled')}</option>
          </select>
        </div>
        <div className="min-w-0 sm:col-span-2">
          <label className={labelCls}>
            {tAi('contextLabel')} {tAi('contextLabelSuffix')}
          </label>
          <textarea
            value={uAiSummaryContext}
            onChange={(e) => setUAiSummaryContext(e.target.value)}
            rows={3}
            maxLength={4000}
            placeholder={tAi('contextPlaceholder')}
            className={inputCls}
          />
          <p className="mt-1 text-xs text-slate-400">{tAi('contextHint')}</p>
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{tAi('policyMode')}</label>
          <select
            value={uAiPolicyMode}
            onChange={(e) => setUAiPolicyMode(e.target.value)}
            className={inputCls}
          >
            <option value="">{t('unchanged')}</option>
            <option value="default">{tAi('modeDefault')}</option>
            <option value="enabled">{tAi('modeEnabled')}</option>
            <option value="disabled">{tAi('modeDisabled')}</option>
          </select>
          <p className="mt-1 text-xs text-slate-400">{tAi('policyModeNote')}</p>
        </div>
      </>
    ),

    security: (
      <>
        <div className="min-w-0">
          <label className={labelCls}>{tScan('engine')}</label>
          <select
            value={uScanEngine}
            onChange={(e) => setUScanEngine(e.target.value)}
            className={inputCls}
          >
            <option value="">{t('unchanged')}</option>
            <option value="checkov">{tScan('engineCheckov')}</option>
            <option value="trivy">{tScan('engineTrivy')}</option>
            <option value="both">{tScan('engineBoth')}</option>
          </select>
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{tScan('enforcement')}</label>
          <select
            value={uScanEnforcement}
            onChange={(e) => setUScanEnforcement(e.target.value)}
            className={inputCls}
          >
            <option value="">{t('unchanged')}</option>
            <option value="off">{tScan('enforcementOff')}</option>
            <option value="advisory">{tScan('enforcementAdvisory')}</option>
            <option value="enforced">{tScan('enforcementEnforced')}</option>
          </select>
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{tScan('threshold')}</label>
          <select
            value={uScanThreshold}
            onChange={(e) => setUScanThreshold(e.target.value)}
            className={inputCls}
          >
            <option value="">{t('unchanged')}</option>
            <option value="critical">{tScan('thresholdCritical')}</option>
            <option value="high">{tScan('thresholdHigh')}</option>
            <option value="medium">{tScan('thresholdMedium')}</option>
            <option value="low">{tScan('thresholdLow')}</option>
          </select>
        </div>
      </>
    ),

    metadata: (
      <>
        <div className="min-w-0 sm:col-span-2">
          <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
            <input
              type="checkbox"
              checked={uSetLabels}
              onChange={(e) => setUSetLabels(e.target.checked)}
            />
            {t('update.setLabels')}
          </label>
          {uSetLabels && <LabelsEditor labels={uLabels} onChange={setULabels} />}
        </div>
        <div className="min-w-0 sm:col-span-2">
          <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
            <input
              type="checkbox"
              checked={uSetVarFiles}
              onChange={(e) => setUSetVarFiles(e.target.checked)}
            />
            {t('update.setVarFiles')}
          </label>
          {uSetVarFiles && (
            <StringListEditor
              values={uVarFiles}
              onChange={setUVarFiles}
              placeholder="env/prod.tfvars"
              addLabel={t('update.addVarFile')}
            />
          )}
        </div>
      </>
    ),

    cloudIdentity: (
      <div className="min-w-0 sm:col-span-2">
        <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
          <input
            type="checkbox"
            checked={uSetOidcAudiences}
            onChange={(e) => setUSetOidcAudiences(e.target.checked)}
          />
          {t('update.setOidcAudiences')}
        </label>
        {uSetOidcAudiences && (
          <div className="space-y-2">
            {tieredHint(tWs, 'fields.oidcAudiencesHint')}
            <OidcAudiencesEditor
              value={uOidcAudiences}
              inert={!oidcDefaults.issuerEnabled}
              onChange={setUOidcAudiences}
              audiencePlaceholder={tWs('fields.oidcAudiencesPlaceholder')}
              addAudienceLabel={tWs('fields.oidcAudiencesAdd')}
            />
          </div>
        )}
      </div>
    ),

    diagnostics: (
      <>
        <div className="min-w-0">
          <label className={labelCls}>{tWs('debugMode.title')}</label>
          <select
            value={uDebugMode}
            onChange={(e) => setUDebugMode(e.target.value)}
            className={inputCls}
          >
            <option value="">{t('unchanged')}</option>
            <option value="true">{tCommon('yes')}</option>
            <option value="false">{tCommon('no')}</option>
          </select>
        </div>
        <div className="min-w-0">
          <label className={labelCls}>{tWs('slack.channel')}</label>
          <input
            type="text"
            value={uSlackChannel}
            onChange={(e) => setUSlackChannel(e.target.value)}
            placeholder={t('unchanged')}
            className={inputCls}
          />
        </div>
      </>
    ),
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={t('title')}
          description={t('description')}
        />

        {error && <ErrorBanner message={error} />}

        {/* ---- Filter ---- */}
        <form
          onSubmit={handleSearch}
          className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3"
        >
          <h2 className="text-lg font-semibold text-slate-100">{t('filter.heading')}</h2>
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={fAll}
              onChange={(e) => setFAll(e.target.checked)}
            />
            {t.rich('filter.matchAll', { strong: (c) => <strong>{c}</strong> })}
          </label>
          <fieldset
            disabled={fAll}
            // `min-w-0`: a `fieldset` defaults to `min-inline-size: min-content`,
            // so it refuses to shrink below its content however narrow the
            // viewport and `w-full` on the children cannot save it. Measured at
            // 412px it sat 501px wide and was the outermost cause of the page's
            // horizontal scroll.
            className={
              fAll
                ? 'opacity-40 pointer-events-none space-y-3 min-w-0'
                : 'space-y-3 min-w-0'
            }
          >
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div>
                <label className={labelCls}>{t('filter.namePrefix')}</label>
                <input
                  type="text"
                  value={fNamePrefix}
                  onChange={(e) => setFNamePrefix(e.target.value)}
                  placeholder="prod-"
                  className={inputCls}
                />
              </div>
              <div>
                <label className={labelCls}>{t('filter.executionBackend')}</label>
                <select
                  value={fExecBackend}
                  onChange={(e) => setFExecBackend(e.target.value)}
                  className={inputCls}
                >
                  <option value="">{t('anyOption')}</option>
                  <option value="tofu">tofu</option>
                  <option value="terraform">terraform</option>
                </select>
              </div>
              <div>
                <label className={labelCls}>{t('filter.ownerEmail')}</label>
                <input
                  type="text"
                  value={fOwnerEmail}
                  onChange={(e) => setFOwnerEmail(e.target.value)}
                  placeholder="platform@example.com"
                  className={inputCls}
                />
              </div>
              <div>
                <label className={labelCls}>{t('filter.agentPool')}</label>
                <select
                  value={fAgentPoolId}
                  onChange={(e) => setFAgentPoolId(e.target.value)}
                  className={inputCls}
                >
                  <option value="">{t('anyOption')}</option>
                  {pools.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.attributes.name}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className={labelCls}>{t('filter.vcsConnection')}</label>
                <select
                  value={fVcsConnId}
                  onChange={(e) => setFVcsConnId(e.target.value)}
                  className={inputCls}
                >
                  <option value="">{t('anyOption')}</option>
                  {connections.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.attributes.name} ({c.attributes.provider})
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <div>
              <label className={labelCls}>{t('filter.labels')}</label>
              <LabelsEditor labels={fLabels} onChange={setFLabels} />
            </div>
          </fieldset>
          <button
            type="submit"
            disabled={searching || loading}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-600/80 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
          >
            {searching ? t('filter.searching') : t('filter.search')}
          </button>
        </form>

        {searchResult && (
          <div className="mb-6">
            <p className="text-sm text-slate-400 mb-2">
              {t.rich('results.matched', {
                count: searchResult.matched,
                strong: (c) => <strong className="text-slate-200">{c}</strong>,
              })}
            </p>
            {searchResult.workspaces.length === 0 ? (
              <EmptyState message={t('results.noMatch')} />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-slate-700/50 text-start text-xs text-slate-400 uppercase">
                      <th className="px-4 py-2">{t('results.colName')}</th>
                      <th className="px-4 py-2">{t('results.colMode')}</th>
                      <th className="px-4 py-2">{t('results.colBackend')}</th>
                      <th className="px-4 py-2 hidden sm:table-cell">{t('results.colTfVersion')}</th>
                      <th className="px-4 py-2 hidden md:table-cell">{t('results.colLabels')}</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {searchResult.workspaces.map((w) => (
                      <tr key={w.id} className="hover:bg-slate-700/20">
                        <td className="px-4 py-3 text-slate-200">{w.name}</td>
                        <td className="px-4 py-3 text-slate-400">{w['execution-mode']}</td>
                        <td className="px-4 py-3 text-slate-400">{w['execution-backend']}</td>
                        <td className="px-4 py-3 text-slate-400 hidden sm:table-cell">
                          {w['terraform-version']}
                        </td>
                        <td className="px-4 py-3 hidden md:table-cell">
                          <LabelsEditor labels={w.labels || {}} readOnly />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {/* ---- Update ---- */}
        <form
          onSubmit={handleSubmit}
          className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-4"
        >
          <h2 className="text-lg font-semibold text-slate-100">{t('update.heading')}</h2>
          <p className="text-xs text-slate-500">
            {t('update.hint')}
          </p>

          {/* The shared sections, in the registry's own order (#2413 F4).
              Four forms set workspace attributes and until now each invented
              its own arrangement -- this one had ~35 controls in two unlabelled
              grids with six heading-less separators, so the orchestrator's CPU
              and memory requests sat nine fields apart (#2413 F3) and the only
              two real headings were an `<h3>` pair the rest of the form had no
              counterpart for (#2413 F5).

              `Group` is shared; the CONTROLS are not, which is the line
              `workspace-fields.ts` draws: which group an attribute belongs to
              and which boolean gates it are properties of the ATTRIBUTE and the
              same everywhere, while the control type and what an empty value
              means are per surface. Both are load-bearing here -- a tri-state
              select and a `uSet*` opt-in checkbox are how "leave this alone"
              is said on a fleet form, and neither has a counterpart on a form
              that sets one concrete value.

              A group whose every field is behind an off gate still renders,
              with those fields disabled (#2413 F20): seeing the option is how
              an operator learns it exists. */}
          {GROUPS.filter((g) => groupBody[g]).map((g) => (
            <Group
              key={g}
              id={g}
              ctx={groupCtx}
              title={tWs(`groups.${g}`)}
              description={groupDescription[g]}
            >
              {groupBody[g]}
            </Group>
          ))}

          {/* Run tasks and notification configurations have NO entry in
              `FIELDS`, so they cannot be rendered inside the registry-driven
              loop above -- and that is correct rather than a gap. The table is
              the workspace's own ATTRIBUTES; these two are collections attached
              to a workspace and upserted by name, each with its own template
              editor that owns its rows. They keep the section idiom so the form
              reads as one thing, and they sit last because replacing a
              collection wholesale across a match set is the heaviest thing on
              the page. The headings reuse the workspace page's own tab names:
              they are already translated in every catalogue and are the words
              an operator has seen for exactly these two things. */}
          <section className="min-w-0">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
              {tWs('tabs.runTasks')}
            </h4>
            <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
              <input
                type="checkbox"
                checked={uSetRunTasks}
                onChange={(e) => setUSetRunTasks(e.target.checked)}
              />
              {t('update.setRunTasks')}
            </label>
            {uSetRunTasks && (
              <RunTaskTemplatesEditor items={uRunTasks} onChange={setURunTasks} />
            )}
          </section>

          <section className="min-w-0">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
              {tWs('tabs.notifications')}
            </h4>
            <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
              <input
                type="checkbox"
                checked={uSetNotifications}
                onChange={(e) => setUSetNotifications(e.target.checked)}
              />
              {t('update.setNotifications')}
            </label>
            {uSetNotifications && (
              <NotificationTemplatesEditor
                items={uNotifications}
                onChange={setUNotifications}
              />
            )}
          </section>

          {/* BOTH actions are always present. The previous shape put a single
              button next to a `Dry run` checkbox that was ticked by default, so
              the only control that looked like a save performed a no-op and
              nothing said so — you had to UNTICK something to reach Apply.
              Preview and Apply are two different intentions, so they are two
              buttons, and neither is reachable only by changing a checkbox
              first. The confirm step stays: this writes to every matched
              workspace, which is tier 1 of the #719 confirm policy. */}
          <div className="pt-3 border-t border-slate-800 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() => {
                setConfirmApply(false)
                runBulkUpdate(true)
              }}
              disabled={submitting || uWouldLeaveNoOperations}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 disabled:opacity-50 text-slate-100 transition-colors"
            >
              {submitting ? t('update.working') : t('update.previewDryRun')}
            </button>
            {confirmApply ? (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm text-amber-300">{t('update.confirmWarning')}</span>
                <button
                  type="submit"
                  disabled={submitting || uWouldLeaveNoOperations}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-600/80 disabled:opacity-50 text-white transition-colors"
                >
                  {submitting ? t('update.applying') : t('update.confirmApply')}
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmApply(false)}
                  className="px-3 py-2 rounded-lg text-sm bg-slate-700 hover:bg-slate-600 text-slate-200"
                >
                  {t('update.cancel')}
                </button>
              </div>
            ) : (
              <button
                type="submit"
                disabled={submitting || uWouldLeaveNoOperations}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-amber-500 hover:bg-amber-500/80 disabled:opacity-50 text-black transition-colors"
              >
                {t('update.applyChanges')}
              </button>
            )}
          </div>
        </form>

        {dryResult && (
          <div className="mb-6">
            <h2 className="text-lg font-semibold text-slate-100 mb-1">{t('dryResult.heading')}</h2>
            <p className="text-sm text-slate-400 mb-3">
              {t.rich('dryResult.summary', {
                matched: dryResult.matched,
                wouldChange: dryResult.would_change.length,
                unchanged: dryResult.unchanged.length,
                strong: (c) => <strong className="text-slate-200">{c}</strong>,
              })}
            </p>
            {renderDiffTable(dryResult.would_change, t('dryResult.wouldChange'))}
            {dryResult.would_change.length === 0 && (
              <EmptyState message={t('dryResult.noChange')} />
            )}
          </div>
        )}

        {applyResult && (
          <div className="mb-6">
            <h2 className="text-lg font-semibold text-slate-100 mb-1">{t('applyResult.heading')}</h2>
            <p className="text-sm text-slate-400 mb-3">
              {t.rich('applyResult.summary', {
                matched: applyResult.matched,
                applied: applyResult.applied,
                unchanged: applyResult.unchanged.length,
                errors: applyResult.errors.length,
                strong: (c) => <strong className="text-slate-200">{c}</strong>,
                appliedstrong: (c) => <strong className="text-green-300">{c}</strong>,
              })}
            </p>
            {applyResult.errors.length > 0 && (
              <div className="mb-4 p-3 bg-red-900/30 text-red-300 rounded-lg text-sm border border-red-800/50">
                <ul className="space-y-1">
                  {applyResult.errors.map((er, i) => (
                    <li key={i}>
                      {er.name || er.id || t('applyResult.unnamedWorkspace')}: {er.error}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {renderDiffTable(applyResult.changes, t('applyResult.applied'))}
            {applyResult.applied === 0 && applyResult.errors.length === 0 && (
              <EmptyState message={t('applyResult.noChange')} />
            )}
          </div>
        )}
      </main>
    </>
  )
}
