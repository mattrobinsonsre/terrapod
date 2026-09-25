'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { LabelsEditor } from '@/components/labels-editor'
import {
  StringListEditor,
  RunTaskTemplatesEditor,
  NotificationTemplatesEditor,
  type RunTaskSpec,
  type NotificationSpec,
} from '@/components/template-editors'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch, fetchAllPages } from '@/lib/api'

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
  const [uExecBackend, setUExecBackend] = useState('')
  const [uExecMode, setUExecMode] = useState('')
  // '' means "leave unchanged"; anything else is an auto-apply mode (#1276).
  const [uAutoApplyMode, setUAutoApplyMode] = useState('')
  const [uAgentPoolId, setUAgentPoolId] = useState('')
  const [uResourceCpu, setUResourceCpu] = useState('')
  const [uResourceMemory, setUResourceMemory] = useState('')
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
  const [uSetLabels, setUSetLabels] = useState(false)
  const [uLabels, setULabels] = useState<Record<string, string>>({})
  const [uSetVarFiles, setUSetVarFiles] = useState(false)
  const [uVarFiles, setUVarFiles] = useState<string[]>([])
  const [uSetRunTasks, setUSetRunTasks] = useState(false)
  const [uRunTasks, setURunTasks] = useState<RunTaskSpec[]>([])
  const [uSetNotifications, setUSetNotifications] = useState(false)
  const [uNotifications, setUNotifications] = useState<NotificationSpec[]>([])

  const [dryRun, setDryRun] = useState(true)
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
    if (uExecBackend) u['execution-backend'] = uExecBackend
    if (uExecMode) u['execution-mode'] = uExecMode
    // Send the mode, never the boolean — the endpoint 422s if both are set.
    if (uAutoApplyMode) u['auto-apply-mode'] = uAutoApplyMode
    if (uAgentPoolId) u['agent-pool-id'] = uAgentPoolId
    if (uResourceCpu.trim()) u['resource-cpu'] = uResourceCpu.trim()
    if (uResourceMemory.trim()) u['resource-memory'] = uResourceMemory.trim()
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
    if (uSetLabels) u.labels = uLabels
    if (uSetVarFiles) u['var-files'] = uVarFiles.map((s) => s.trim()).filter(Boolean)
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
    if (dryRun) {
      runBulkUpdate(true)
      return
    }
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
            className={fAll ? 'opacity-40 pointer-events-none space-y-3' : 'space-y-3'}
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

          <div className="pt-3 border-t border-slate-800 flex flex-wrap items-center gap-4">
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={dryRun}
                onChange={(e) => {
                  setDryRun(e.target.checked)
                  setConfirmApply(false)
                }}
              />
              {t('update.dryRun')}
            </label>
            {!dryRun && confirmApply ? (
              <div className="flex items-center gap-2">
                <span className="text-sm text-amber-300">
                  {t('update.confirmWarning')}
                </span>
                <button
                  type="submit"
                  disabled={submitting}
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
                disabled={submitting}
                className={`px-4 py-2 rounded-lg text-sm font-medium text-white transition-colors disabled:opacity-50 ${
                  dryRun
                    ? 'bg-brand-600 hover:bg-brand-500'
                    : 'bg-amber-700 hover:bg-amber-600'
                }`}
              >
                {submitting
                  ? t('update.working')
                  : dryRun
                    ? t('update.previewDryRun')
                    : t('update.applyChanges')}
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
