'use client'

import { useEffect, useState } from 'react'
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
import { apiFetch, fetchAllPages } from '@/lib/api'
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
  'w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent'
const labelCls = 'block text-sm font-medium text-slate-300 mb-1'

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
  // How a configure starts (#1974). Both opt-in, both meaningless without
  // `configure-enabled` above, and both bulk-settable because adopting Ansible
  // across a fleet means turning the trigger on for the workspaces that should
  // have it.
  const [uAutoConfigureEnabled, setUAutoConfigureEnabled] = useState('')
  const [uConfigureAfterApply, setUConfigureAfterApply] = useState('')
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
  // A tri-state '' | 'true' | 'false', as terragrunt-enabled below.
  const [uInventoryIncludePlatform, setUInventoryIncludePlatform] = useState('')
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
  const [uSetInventoryIgnorePaths, setUSetInventoryIgnorePaths] = useState(false)
  const [uInventoryIgnorePaths, setUInventoryIgnorePaths] = useState<string[]>([])
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
    if (uAutoConfigureEnabled) u['auto-configure-enabled'] = uAutoConfigureEnabled === 'true'
    if (uConfigureAfterApply) u['configure-after-apply'] = uConfigureAfterApply === 'true'
    // Not `if (uConfigDirectory.trim())` — see the state comment: '' is a
    // value here (the configuration version's root), so the checkbox is what
    // says whether to write this at all. Never `null`: an explicit null on
    // this attribute is a 422 naming the field (#1966).
    if (uSetConfigDirectory) u['config-directory'] = uConfigDirectory.trim()
    if (uSetDefaultPlaybook) u['default-playbook'] = uDefaultPlaybook.trim()
    if (uSetDefaultLimit) u['default-limit'] = uDefaultLimit.trim()
    if (uInventoryIncludePlatform)
      u['inventory-include-platform'] = uInventoryIncludePlatform === 'true'
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
    if (uSetInventoryIgnorePaths)
      u['inventory-ignore-paths'] = uInventoryIgnorePaths.map((s) => s.trim()).filter(Boolean)
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
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.searchFailed', { status: res.status }))
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
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.bulkUpdateFailed', { status: res.status }))
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
            className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
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

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <div>
              <label className={labelCls}>{t('update.terraformVersion')}</label>
              <input
                type="text"
                value={uTfVersion}
                onChange={(e) => setUTfVersion(e.target.value)}
                placeholder={t('unchanged')}
                className={inputCls}
              />
            </div>
            <div>
              <label className={labelCls} title={tWs('fields.ansibleVersionTitle')}>
                {tWs('fields.ansibleVersion')}
              </label>
              {envs.listing === 'complete' && envs.lines.length > 0 ? (
                <select
                  value={uAnsibleVersion}
                  onChange={(e) => setUAnsibleVersion(e.target.value)}
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
                  className={inputCls}
                />
              )}
            </div>
            <div>
              <label className={labelCls} title={tWs('fields.ansibleExecutionEnvironmentTitle')}>
                {tWs('fields.ansibleExecutionEnvironment')}
              </label>
              {/* Always a picker: operator labels come from configuration, not
                  from a registry, so they are authoritative even when the
                  built-in listing is unknown. */}
              <select
                value={uAnsibleEE}
                onChange={(e) => setUAnsibleEE(e.target.value)}
                disabled={envs.labels.length === 0}
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
            </div>
            <div>
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
            <div>
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
            <div>
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
            <div>
              <label className={labelCls}>{t('update.agentPool')}</label>
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
            <div>
              <label className={labelCls}>{t('update.cpuRequest')}</label>
              <input
                type="text"
                value={uResourceCpu}
                onChange={(e) => setUResourceCpu(e.target.value)}
                placeholder={t('unchanged')}
                className={inputCls}
              />
            </div>
            <div>
              <label className={labelCls}>{t('update.memoryRequest')}</label>
              <input
                type="text"
                value={uResourceMemory}
                onChange={(e) => setUResourceMemory(e.target.value)}
                placeholder={t('unchanged')}
                className={inputCls}
              />
            </div>
            <div>
              <label className={labelCls}>{t('update.eeCpuRequest')}</label>
              <input
                type="text"
                value={uEeResourceCpu}
                onChange={(e) => setUEeResourceCpu(e.target.value)}
                placeholder={t('unchanged')}
                className={inputCls}
              />
            </div>
            <div>
              <label className={labelCls}>{t('update.eeMemoryRequest')}</label>
              <input
                type="text"
                value={uEeResourceMemory}
                onChange={(e) => setUEeResourceMemory(e.target.value)}
                placeholder={t('unchanged')}
                className={inputCls}
              />
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 pt-3 border-t border-slate-800">
            <div>
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
            <div>
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
            <div>
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
            <div>
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
            <div>
              <label className={labelCls}>{tAd('terragruntVersion')}</label>
              <input
                type="text"
                value={uTerragruntVersion}
                onChange={(e) => setUTerragruntVersion(e.target.value)}
                placeholder={t('unchanged')}
                className={inputCls}
              />
            </div>
            <div>
              <label className={labelCls}>{tWs('fields.vcsWorkflow')}</label>
              <select
                value={uVcsWorkflow}
                onChange={(e) => setUVcsWorkflow(e.target.value)}
                className={inputCls}
              >
                <option value="">{t('unchanged')}</option>
                <option value="merge_then_apply">{tWs('fields.vcsWorkflowMergeThenApply')}</option>
                <option value="apply_then_merge">{tWs('fields.vcsWorkflowApplyThenMerge')}</option>
              </select>
            </div>
            <div>
              <label className={labelCls}>{tWs('fields.autoMerge')}</label>
              <select
                value={uAutoMerge}
                onChange={(e) => setUAutoMerge(e.target.value)}
                className={inputCls}
              >
                <option value="">{t('unchanged')}</option>
                <option value="true">{tCommon('yes')}</option>
                <option value="false">{tCommon('no')}</option>
              </select>
            </div>
            <div>
              <label className={labelCls}>{tWs('fields.autoMergeStrategy')}</label>
              <select
                value={uAutoMergeStrategy}
                onChange={(e) => setUAutoMergeStrategy(e.target.value)}
                className={inputCls}
              >
                <option value="">{t('unchanged')}</option>
                <option value="merge">merge</option>{/* i18n-ignore */}
                <option value="squash">squash</option>{/* i18n-ignore */}
                <option value="rebase">rebase</option>{/* i18n-ignore */}
              </select>
            </div>
            <div>
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
            <div>
              <label className={labelCls}>{tWs('drift.checkInterval')}</label>
              <input
                type="number"
                min={0}
                value={uDriftInterval}
                onChange={(e) => setUDriftInterval(e.target.value)}
                placeholder={t('unchanged')}
                className={inputCls}
              />
            </div>
            <div>
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
            <div>
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
            <div>
              <label className={labelCls}>{tWs('slack.channel')}</label>
              <input
                type="text"
                value={uSlackChannel}
                onChange={(e) => setUSlackChannel(e.target.value)}
                placeholder={t('unchanged')}
                className={inputCls}
              />
            </div>
            <div>
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
            <div>
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
            <div>
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
            <div>
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
            <div>
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
            <div className="sm:col-span-2">
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
          </div>

          {/* Which operations the matched workspaces have (#1966). Its own
              block above the Ansible one, because `configure-enabled` is the
              declaration and the roots below are only where that operation
              runs — the ordering #1929 did not have, and #1966 introduced. */}
          <div className="pt-3 border-t border-slate-800">
            <h3 className="text-sm font-medium text-slate-200 mb-1">{tWs('fields.operations')}</h3>
            <p className="text-xs text-slate-500 mb-3">{tWs('fields.operationsHint')}</p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
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
                <p className="mt-1 text-xs text-slate-400">
                  {tWs('fields.planApplyEnabledHint')}
                </p>
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
                <p className="mt-1 text-xs text-slate-400">
                  {tWs('fields.configureEnabledHint')}
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
                  className={inputCls}
                >
                  <option value="">{t('unchangedOption')}</option>
                  <option value="true">{tCommon('yes')}</option>
                  <option value="false">{tCommon('no')}</option>
                </select>
                <p className="mt-1 text-xs text-slate-400">
                  {tWs('fields.autoConfigureEnabledHint')}
                </p>
              </div>
              <div className="min-w-0">
                <label className={labelCls} htmlFor="bu-configure-after-apply">
                  {tWs('fields.configureAfterApply')}
                </label>
                <select
                  id="bu-configure-after-apply"
                  value={uConfigureAfterApply}
                  onChange={(e) => setUConfigureAfterApply(e.target.value)}
                  className={inputCls}
                >
                  <option value="">{t('unchangedOption')}</option>
                  <option value="true">{tCommon('yes')}</option>
                  <option value="false">{tCommon('no')}</option>
                </select>
                <p className="mt-1 text-xs text-slate-400">
                  {tWs('fields.configureAfterApplyHint')}
                </p>
              </div>
            </div>
            {uWouldLeaveNoOperations && (
              // Guaranteed 422 for every matched workspace, so both submits are
              // blocked below and this is the sentence that explains it.
              <p
                className="mt-3 rounded-lg border border-red-500/50 bg-red-500/10 p-2 text-xs text-red-300"
                data-testid="bu-operations-none"
              >
                {t('update.operationsNone')}
              </p>
            )}
          </div>

          {/* Where the configure operation runs, and what it does by
              default (#1929) */}
          <div className="pt-3 border-t border-slate-800">
            <h3 className="text-sm font-medium text-slate-200 mb-1">{tAd('ansibleConfigure')}</h3>
            <p className="text-xs text-slate-500 mb-3">{tAd('ansibleConfigureHint')}</p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {/* min-w-0: a grid item has min-width:auto, so a select whose
                  longest option is a long translated string can push the row
                  past the viewport however w-full the control is. */}
              <div className="min-w-0">
                <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
                  <input
                    type="checkbox"
                    checked={uSetConfigDirectory}
                    onChange={(e) => setUSetConfigDirectory(e.target.checked)}
                  />
                  {t('update.setConfigDirectory')}
                </label>
                {/* A checkbox plus a box, not a select: '' is a real value
                    (the configuration version's root), so the gate has to be
                    out of band — the same shape as the two run defaults
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
                      title={tWs('fields.configDirectoryTitle')}
                      className={inputCls}
                    />
                    <p className="mt-1 text-xs text-slate-400">
                      {tWs('fields.configDirectoryRootHint')}
                    </p>
                  </>
                )}
              </div>
              <div className="min-w-0">
                <label className={labelCls} htmlFor="bu-inventory-include-platform">
                  {tWs('fields.inventoryIncludePlatform')}
                </label>
                <select
                  id="bu-inventory-include-platform"
                  value={uInventoryIncludePlatform}
                  onChange={(e) => setUInventoryIncludePlatform(e.target.value)}
                  title={tWs('fields.inventoryIncludePlatformTitle')}
                  className={inputCls}
                >
                  <option value="">{t('unchangedOption')}</option>
                  <option value="true">{tCommon('yes')}</option>
                  <option value="false">{tCommon('no')}</option>
                </select>
                <p className="mt-1 text-xs text-slate-400">
                  {tWs('fields.inventoryIncludePlatformHint')}
                </p>
              </div>
            </div>
            <div className="mt-3">
              <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
                <input
                  type="checkbox"
                  checked={uSetDefaultPlaybook}
                  onChange={(e) => setUSetDefaultPlaybook(e.target.checked)}
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
                  className={inputCls}
                />
              )}
            </div>
            <div className="mt-3">
              <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
                <input
                  type="checkbox"
                  checked={uSetDefaultLimit}
                  onChange={(e) => setUSetDefaultLimit(e.target.checked)}
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
                  className={inputCls}
                />
              )}
            </div>
          </div>

          <div className="pt-3 border-t border-slate-800">
            <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
              <input
                type="checkbox"
                checked={uSetInventoryPaths}
                onChange={(e) => setUSetInventoryPaths(e.target.checked)}
              />
              {t('update.setInventoryPaths')}
            </label>
            {uSetInventoryPaths && (
              <div className="space-y-2">
                <p className="text-xs text-slate-400">{tWs('fields.inventoryPathsHint')}</p>
                <StringListEditor
                  values={uInventoryPaths}
                  onChange={setUInventoryPaths}
                  placeholder={tWs('fields.inventoryPathsPlaceholder')}
                  addLabel={tWs('fields.inventoryPathsAdd')}
                />
              </div>
            )}
            <label className="flex items-center gap-2 text-sm text-slate-300 mb-2 mt-3">
              <input
                type="checkbox"
                className="rounded border-slate-600 bg-slate-800"
                checked={uSetAnsibleCollections}
                onChange={(e) => setUSetAnsibleCollections(e.target.checked)}
              />
              {t('update.setAnsibleCollections')}
            </label>
            {uSetAnsibleCollections && (
              <div className="space-y-2">
                <p className="text-xs text-slate-400">
                  {tWs('fields.ansibleCollectionsHint')}
                </p>
                <StringListEditor
                  values={uAnsibleCollections}
                  onChange={setUAnsibleCollections}
                  placeholder={tWs('fields.ansibleCollectionsPlaceholder')}
                  addLabel={tWs('fields.ansibleCollectionsAdd')}
                />
              </div>
            )}
          </div>

          <div className="pt-3 border-t border-slate-800">
            <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
              <input
                type="checkbox"
                checked={uSetInventoryIgnorePaths}
                onChange={(e) => setUSetInventoryIgnorePaths(e.target.checked)}
              />
              {t('update.setInventoryIgnorePaths')}
            </label>
            {uSetInventoryIgnorePaths && (
              <div className="space-y-2">
                <p className="text-xs text-slate-400">{tWs('fields.inventoryIgnorePathsHint')}</p>
                <StringListEditor
                  values={uInventoryIgnorePaths}
                  onChange={setUInventoryIgnorePaths}
                  placeholder={tWs('fields.inventoryIgnorePathsPlaceholder')}
                  addLabel={tWs('fields.inventoryIgnorePathsAdd')}
                />
              </div>
            )}
          </div>

          <div className="pt-3 border-t border-slate-800">
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

          <div className="pt-3 border-t border-slate-800">
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

          <div className="pt-3 border-t border-slate-800">
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
                <p className="text-xs text-slate-400">{tWs('fields.oidcAudiencesHint')}</p>
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

          <div className="pt-3 border-t border-slate-800">
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
          </div>

          <div className="pt-3 border-t border-slate-800">
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
          </div>

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
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-red-700 hover:bg-red-600 disabled:opacity-50 text-white transition-colors"
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
                className="px-4 py-2 rounded-lg text-sm font-medium bg-amber-700 hover:bg-amber-600 disabled:opacity-50 text-white transition-colors"
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
