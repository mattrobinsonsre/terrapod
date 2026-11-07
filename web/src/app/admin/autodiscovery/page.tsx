'use client'

import { useEffect, useState, useCallback } from 'react'
import type { ReactNode } from 'react'
import {
  EE_UNKNOWN,
  fetchAnsibleExecutionEnvironments,
  type AnsibleExecutionEnvironments,
} from '@/lib/ansible-ee'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
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
import {
  CREATE_INPUT,
  FIELD_BY_ATTR,
  GROUPS,
  LABEL_KEY,
  type GroupId,
} from '@/components/workspace-fields'
import { Field, Group, type GroupCtx } from '@/components/workspace-field-groups'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch, fetchAllPages, parseApiError } from '@/lib/api'
import { useOidcAudienceDefaults } from '@/lib/use-oidc-audience-defaults'
import { useSortable } from '@/lib/use-sortable'
import { useFormat } from '@/lib/format'
import { formatCollectionLines, parseCollectionLines } from '@/lib/ansible-collections'

interface AutodiscoveryRule {
  id: string
  attributes: {
    name: string
    'name-template': string
    'vcs-connection-id': string
    'repo-url': string
    branch: string
    pattern: string
    'ignore-patterns': string[]
    enabled: boolean
    'execution-mode': string
    'execution-backend': string
    engine?: string
    'pulumi-bind-plan'?: boolean
    'agent-pool-id': string | null
    'terraform-version': string
    'ansible-version'?: string
    'ansible-execution-environment'?: string
    'ansible-collections'?: { name: string; version?: string }[]
    // Which operations a materialised workspace has (#1966), and the roots
    // each one uses (#1929). `configure-enabled` defaults false, so a rule
    // written before #1966 goes on producing plan/apply workspaces.
    'plan-apply-enabled'?: boolean
    'configure-enabled'?: boolean
    // And how a configure starts on them (#1974), both default false.
    'auto-configure-enabled'?: boolean
    'configure-after-apply'?: boolean
    'configure-all-hosts'?: boolean
    // Which inventory sources ansible may resolve on the runner (#2033),
    // templated for the same reason the triggers above are: a rule that can
    // declare a configure operation and not say which sources it may read
    // would leave an operator editing every materialised workspace by hand.
    'dynamic-inventory-enabled'?: boolean
    'inventory-scripts-enabled'?: boolean
    // Whether a configure on a materialised workspace verifies the SSH host
    // key of the hosts it targets (#2318). Templated like the two above, and
    // the only one of the three whose default is TRUE.
    'ansible-host-key-checking'?: boolean
    // An ordinary templated path, '' meaning the configuration version's root.
    // No null state: the declaration is `configure-enabled` above (#1966).
    'config-directory'?: string
    'inventory-paths'?: string[]
    'default-playbook'?: string
    'default-limit'?: string
    'resource-cpu': string
    parallelism: number
    'resource-memory': string
    'ee-resource-cpu'?: string
    'ee-resource-memory'?: string
    'auto-apply': boolean
    'auto-apply-mode'?: string
    'on-directory-delete': 'flag' | 'destroy'
    labels: Record<string, string>
    'owner-email': string
    'var-files': string[]
    'oidc-audiences'?: OidcAudiences
    'execution-hook-templates'?: string[]
    'security-scan-enforcement'?: string
    'security-scan-engine'?: string
    'security-scan-severity-threshold'?: string
    'ai-summary-mode'?: string
    'ai-policy-mode'?: string
    'ai-summary-context'?: string
    'terragrunt-enabled'?: boolean
    'terragrunt-version'?: string
    'vcs-workflow'?: string
    'auto-merge'?: boolean
    'auto-merge-strategy'?: string
    'drift-detection-enabled'?: boolean
    'drift-detection-interval-seconds'?: number
    'drift-ignore-rules'?: string[]
    'plan-expiry-seconds'?: number | null
    'slack-channel'?: string
    'debug-mode'?: boolean
    'allow-fork-pr-plans'?: boolean
    'run-task-templates': RunTaskSpec[]
    'notification-templates': NotificationSpec[]
    'created-at': string
    'updated-at': string
  }
}

interface VCSConnection {
  id: string
  attributes: { name: string; provider: string }
}

interface AgentPool {
  id: string
  attributes: { name: string }
}

interface ExecutionHookRef {
  id: string
  attributes: { name: string; 'hook-point': string; enabled: boolean }
}

type SortKey = 'name' | 'repo' | 'pattern' | 'enabled' | 'created'

/**
 * Settings the rule fixes rather than offers.
 *
 * `execution-mode` is the only one: autodiscovery is VCS-driven, so a
 * materialised workspace is always agent-executed. Rendered DISABLED rather
 * than omitted, for the same reason a field behind an off gate is (#2413 F20)
 * -- seeing the setting is how an operator learns the rule has made that
 * choice for them, where an absent control reads as a missing feature.
 */
const RULE_READ_ONLY: ReadonlySet<string> = new Set(['execution-mode'])

/**
 * A templated boolean, as a yes/no `<select>`.
 *
 * THE CONTROL IDIOM IS PER SURFACE and the structure is shared (#2413 F4):
 * `lib/workspace-fields.ts` says which group a boolean belongs to and which
 * other boolean gates it, and this form keeps its own input. The select is
 * what the newest blocks on this form already chose, and the reason holds:
 * a rule writes a CONCRETE value onto every workspace it materialises, so
 * "yes" and "no" are the two things an operator is choosing between and a
 * checkbox renders the second as an absence.
 *
 * One component rather than eleven hand-written selects, because the eleven
 * are where the two would drift. The gate arrives from `Field`, which wraps a
 * bespoke control in a disabled `<fieldset>` when it is unmet -- so this only
 * has to honour `busy` and `readOnly` itself.
 */
function RuleYesNo({ attr, ctx }: { attr: string; ctx: GroupCtx }) {
  // Its own `useTranslations` rather than two props, so a call site is
  // `<RuleYesNo attr=... ctx=... />` and nothing else. That spelling is
  // load-bearing: `workspace-operations.test.ts` reads this file as TEXT for a
  // control per operation boolean, and a helper taking the attribute as an
  // argument -- `yesNo('configure-enabled')` -- made the attribute invisible to
  // it. The gate caught it, which is the gate working.
  //
  // And this comment writes the example WITHOUT the real quoted form on
  // purpose: spelling it here would satisfy that gate from prose, so deleting
  // the control below would leave it green. That is the same defect one layer
  // up, and it is the one this file keeps meeting.
  const tCommon = useTranslations('runDetail.common')
  const id = FIELD_BY_ATTR[attr]?.id ?? attr
  return (
    <Field
      attr={attr}
      ctx={ctx}
      kind="select"
      control={
        <select
          id={id}
          data-testid={id}
          value={ctx.values[attr] ? 'true' : 'false'}
          disabled={ctx.busy || ctx.readOnly?.has(attr)}
          onChange={(e) => ctx.onChange(attr, e.target.value === 'true')}
          className={CREATE_INPUT}
        >
          <option value="true">{tCommon('yes')}</option>
          <option value="false">{tCommon('no')}</option>
        </select>
      }
    />
  )
}

export default function AutodiscoveryPage() {
  const router = useRouter()
  const t = useTranslations('adminAutodiscovery')
  const tMode = useTranslations('common.autoApplyMode')
  // Reuses the workspace page's own labels (#1763) so a rule's template and the
  // setting it produces are described in the same words, in every locale.
  const tWs = useTranslations('workspaceDetail')
  // The yes/no pair the templated booleans need is read by `RuleYesNo` itself,
  // so the page no longer binds `runDetail.common`.
  const fmt = useFormat()
  const [rules, setRules] = useState<AutodiscoveryRule[]>([])
  const [connections, setConnections] = useState<VCSConnection[]>([])
  const [pools, setPools] = useState<AgentPool[]>([])
  const [hooks, setHooks] = useState<ExecutionHookRef[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  // Create / edit form
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [vcsConnectionId, setVcsConnectionId] = useState('')
  const [repoUrl, setRepoUrl] = useState('')
  const [branch, setBranch] = useState('')
  const [pattern, setPattern] = useState('accounts/*/**/*.tf')
  const [ignorePatternsText, setIgnorePatternsText] = useState('modules/**')
  const [nameTemplate, setNameTemplate] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [executionMode, setExecutionMode] = useState<'agent'>('agent')
  const [agentPoolId, setAgentPoolId] = useState('')
  const [executionBackend, setExecutionBackend] = useState<'tofu' | 'terraform'>('tofu')
  const [terraformVersion, setTerraformVersion] = useState('1.13')
  // Deliberately NOT pre-filled, unlike the engine version beside it. Empty is
  // a real value here and means "take the platform's current ansible-core
  // line" (#2010, the semantics `engine_version_attr` documents), so
  // pre-filling would pin every rule the UI creates to whatever the default
  // happened to be the day it was created -- which is the thing that goes
  // stale. An operator who wants a pin types one.
  const [ansibleVersion, setAnsibleVersion] = useState('')
  const [ansibleEE, setAnsibleEE] = useState('')
  const [envs, setEnvs] = useState<AnsibleExecutionEnvironments>(EE_UNKNOWN)
  const [ansibleCollections, setAnsibleCollections] = useState<string[]>([])
  // Which operations every workspace this rule materialises has (#1966).
  // These initial values are the model's own column defaults, so a rule
  // created through this form without touching them produces exactly what
  // every rule produced before #1966: plan/apply on, configure off.
  const [planApplyEnabled, setPlanApplyEnabled] = useState(true)
  const [configureEnabled, setConfigureEnabled] = useState(false)
  const [autoConfigureEnabled, setAutoConfigureEnabled] = useState(false)
  const [configureAfterApply, setConfigureAfterApply] = useState(false)
  const [configureAllHosts, setConfigureAllHosts] = useState(false)
  const [dynamicInventoryEnabled, setDynamicInventoryEnabled] = useState(false)
  const [inventoryScriptsEnabled, setInventoryScriptsEnabled] = useState(false)
  const [ansibleHostKeyChecking, setAnsibleHostKeyChecking] = useState(true)
  // Where the configure operation runs in each materialised workspace
  // (#1929) — a plain text box, because the path is just a path and '' is the
  // configuration version's root. #1929 had a mode select here because NULL
  // meant "no configure operation"; that declaration is `configureEnabled`
  // above now (#1966).
  const [configDirectory, setConfigDirectory] = useState('')
  // The one combination the materialised workspace could not hold (#1966).
  // Blocked here because a rule carrying it would produce inert workspaces on
  // every future scan, one per matched directory, with nothing in the rule
  // form to say why.
  const ruleHasNoOperations = !planApplyEnabled && !configureEnabled
  const [inventoryPaths, setInventoryPaths] = useState<string[]>([])
  const [defaultPlaybook, setDefaultPlaybook] = useState('')
  const [defaultLimit, setDefaultLimit] = useState('')
  const [resourceCpu, setResourceCpu] = useState('1')
  const [engine, setEngine] = useState('terraform')
  const [bindPlan, setBindPlan] = useState(false)
  const [parallelism, setParallelism] = useState('10')
  const [resourceMemory, setResourceMemory] = useState('2Gi')
  // Deliberately NOT pre-filled, unlike the pair above: empty is a real value
  // here and means "inherit the deployment default" (#2154), so pre-filling
  // would pin every rule the UI creates to whatever the default happened to be
  // the day it was created.
  const [eeResourceCpu, setEeResourceCpu] = useState('')
  const [eeResourceMemory, setEeResourceMemory] = useState('')
  const [autoApplyMode, setAutoApplyMode] = useState('never')
  const [onDirectoryDelete, setOnDirectoryDelete] = useState<'flag' | 'destroy'>('flag')
  const [labels, setLabels] = useState<Record<string, string>>({})
  const [ownerEmail, setOwnerEmail] = useState('')
  const [varFiles, setVarFiles] = useState<string[]>([])
  // Only to tell the operator when the deployment publishes no issuer, so an
  // entry added here could never be minted for. No partition on this surface:
  // the value IS the override — unlike a workspace read, nothing is merged
  // into it — so there is nothing to subtract and nothing to inherit.
  const oidcDefaults = useOidcAudienceDefaults()
  // Templated onto every workspace the rule creates (#1901), keyed on the
  // provider configuration a token is for. Empty is the default and overrides
  // nothing: those workspaces take the deployment's configured audiences.
  const [oidcAudiences, setOidcAudiences] = useState<OidcAudiences>({})
  const [executionHookTemplates, setExecutionHookTemplates] = useState<string[]>([])
  // Templated onto every workspace the rule materialises (#1763).
  const [scanEnforcement, setScanEnforcement] = useState('advisory')
  const [scanEngine, setScanEngine] = useState('checkov')
  const [scanThreshold, setScanThreshold] = useState('high')
  const [aiSummaryMode, setAiSummaryMode] = useState('default')
  const [aiPolicyMode, setAiPolicyMode] = useState('default')
  const [aiSummaryContext, setAiSummaryContext] = useState('')
  const [terragruntEnabled, setTerragruntEnabled] = useState(false)
  const [terragruntVersion, setTerragruntVersion] = useState('1.0')
  const [ruleVcsWorkflow, setRuleVcsWorkflow] = useState('merge_then_apply')
  const [ruleAutoMerge, setRuleAutoMerge] = useState(false)
  const [ruleAutoMergeStrategy, setRuleAutoMergeStrategy] = useState('merge')
  const [driftEnabled, setDriftEnabled] = useState(true)
  const [driftInterval, setDriftInterval] = useState(86400)
  const [driftIgnoreRules, setDriftIgnoreRules] = useState<string[]>([])
  const [planExpiry, setPlanExpiry] = useState('')
  const [ruleSlackChannel, setRuleSlackChannel] = useState('')
  // Runner debug mode (#1764). Off by default here as on a workspace: a rule
  // can materialise hundreds of workspaces, and a held pod keeps credentials.
  const [ruleDebugMode, setRuleDebugMode] = useState(false)
  const [ruleAllowForkPrPlans, setRuleAllowForkPrPlans] = useState(false)
  const [runTaskTemplates, setRunTaskTemplates] = useState<RunTaskSpec[]>([])
  const [notificationTemplates, setNotificationTemplates] = useState<NotificationSpec[]>([])
  const [submitting, setSubmitting] = useState(false)

  const [deleteId, setDeleteId] = useState<string | null>(null)

  // Preview / on-demand scan modal (#311). Three lifecycle states:
  //   { id: null }                          → modal closed
  //   { id, loading: true }                  → fetching /preview
  //   { id, entries: [...] }                 → preview loaded; awaiting confirm
  //   { id, entries: [...], scanning: true } → user clicked Provision; running /scan
  const [previewModal, setPreviewModal] = useState<{
    id: string
    ruleName: string
    loading?: boolean
    error?: string
    ref?: string
    filesWalked?: number
    entries?: Array<{
      workspace_name: string
      working_directory: string
      collision: boolean
      existing_autodiscovered: boolean
    }>
    scanning?: boolean
  } | null>(null)

  const accessor = useCallback((r: AutodiscoveryRule, key: SortKey) => {
    switch (key) {
      case 'name': return r.attributes.name
      case 'repo': return r.attributes['repo-url']
      case 'pattern': return r.attributes.pattern
      case 'enabled': return r.attributes.enabled ? '1' : '0'
      case 'created': return r.attributes['created-at']
    }
  }, [])

  const { sortedItems, sortState, toggleSort } = useSortable<AutodiscoveryRule, SortKey>(
    rules, 'name', 'asc', accessor,
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadAll()
  // eslint-disable-next-line react-hooks/exhaustive-deps -- initial mount load; the loader is a hoisted function declaration recreated each render, so depending on it would re-fetch on every render
  }, [router])

  async function loadAll() {
    try {
      // rules are fatal for the page; the pickers (conns/pools/hooks) are non-fatal
      const [rulesList, connsList, poolsList, hooksList] = await Promise.all([
        fetchAllPages<AutodiscoveryRule>('/api/terrapod/v1/autodiscovery-rules'),
        fetchAllPages<VCSConnection>('/api/terrapod/v1/vcs-connections').catch(() => [] as VCSConnection[]),
        fetchAllPages<AgentPool>('/api/terrapod/v1/agent-pools').catch(() => [] as AgentPool[]),
        fetchAllPages<ExecutionHookRef>('/api/terrapod/v1/execution-hooks').catch(() => [] as ExecutionHookRef[]),
      ])
      setRules(rulesList)
      // Non-fatal like the pickers above, and its own call because it needs
      // `meta.listing` -- which `fetchAllPages` discards.
      void fetchAnsibleExecutionEnvironments().then(setEnvs)
      setConnections(connsList)
      setPools(poolsList)
      setHooks(hooksList)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.load'))
    } finally {
      setLoading(false)
    }
  }

  /**
   * Back to a brand-new rule's defaults -- EVERY field, which it was not.
   *
   * Fifteen of the form's sixty-one fields were never cleared, so clicking
   * New Rule after editing one carried the edited rule's values into it:
   * its engine, its parallelism, its execution-environment sizing, all three
   * configure-start booleans, BOTH dynamic-inventory grants, host-key
   * checking, its collections, its AI policy and context, and its fork-PR
   * grant. Two of those are grants that let third-party code run on a runner
   * and one decides whether a pull request from a fork gets a speculative
   * plan, so inheriting them silently is not a cosmetic defect -- and it got
   * sharper when the template stopped being collapsed (#2413 F4), because a
   * stale value is now on the screen rather than behind a click.
   *
   * `openEditForm` seeds all sixty-one. That asymmetry is what
   * `autodiscovery-rule-form.test.ts` now pins, deriving BOTH sides from this
   * file so the next field added cannot be left out of one of them.
   *
   * Every value below is the field's own `useState` initialiser, which is what
   * "brand new" means here.
   */
  function resetForm() {
    setEditingId(null)
    setName('')
    setVcsConnectionId('')
    setRepoUrl('')
    setBranch('')
    setPattern('accounts/*/**/*.tf')
    setIgnorePatternsText('modules/**')
    setNameTemplate('')
    setEnabled(true)
    setExecutionMode('agent')
    setAgentPoolId('')
    setExecutionBackend('tofu')
    setEngine('terraform')
    setBindPlan(false)
    setParallelism('10')
    setTerraformVersion('1.13')
    setAnsibleVersion('')
    setAnsibleEE('')
    setAnsibleCollections([])
    // Back to the model's column defaults (#1966), which is what a brand-new
    // rule means by not mentioning them.
    setPlanApplyEnabled(true)
    setConfigureEnabled(false)
    setAutoConfigureEnabled(false)
    setConfigureAfterApply(false)
    setConfigureAllHosts(false)
    setConfigDirectory('')
    setInventoryPaths([])
    setDefaultPlaybook('')
    setDefaultLimit('')
    // The two grants that decide what the RUNNER may resolve (#2033), and the
    // one whose default is TRUE because that is ansible's own behaviour
    // (#2318). All three were among the fifteen.
    setDynamicInventoryEnabled(false)
    setInventoryScriptsEnabled(false)
    setAnsibleHostKeyChecking(true)
    setResourceCpu('1')
    setResourceMemory('2Gi')
    setEeResourceCpu('')
    setEeResourceMemory('')
    setAutoApplyMode('never')
    setOnDirectoryDelete('flag')
    setLabels({})
    setOwnerEmail('')
    setVarFiles([])
    setOidcAudiences({})
    setExecutionHookTemplates([])
    setScanEnforcement('advisory')
    setScanEngine('checkov')
    setScanThreshold('high')
    setAiSummaryMode('default')
    setAiPolicyMode('default')
    setAiSummaryContext('')
    setTerragruntEnabled(false)
    setTerragruntVersion('1.0')
    setRuleVcsWorkflow('merge_then_apply')
    setRuleAutoMerge(false)
    setRuleAutoMergeStrategy('merge')
    setDriftEnabled(true)
    setDriftInterval(86400)
    setDriftIgnoreRules([])
    setPlanExpiry('')
    setRuleSlackChannel('')
    setRuleDebugMode(false)
    setRuleAllowForkPrPlans(false)
    setRunTaskTemplates([])
    setNotificationTemplates([])
  }

  function openCreateForm() {
    resetForm()
    setShowForm(true)
  }

  function openEditForm(r: AutodiscoveryRule) {
    setEditingId(r.id)
    const a = r.attributes
    setName(a.name)
    setVcsConnectionId(a['vcs-connection-id'] ? `vcs-${a['vcs-connection-id']}` : '')
    setRepoUrl(a['repo-url'])
    setBranch(a.branch)
    setPattern(a.pattern)
    setIgnorePatternsText((a['ignore-patterns'] || []).join('\n'))
    setNameTemplate(a['name-template'])
    setEnabled(a.enabled)
    setExecutionMode('agent')
    setAgentPoolId(a['agent-pool-id'] ? `apool-${a['agent-pool-id']}` : '')
    setExecutionBackend((a['execution-backend'] as 'tofu' | 'terraform') || 'tofu')
    setTerraformVersion(a['terraform-version'])
    setAnsibleVersion(a['ansible-version'] || '')
    setAnsibleEE(a['ansible-execution-environment'] || '')
    setAnsibleCollections(formatCollectionLines(a['ansible-collections']))
    // `?? default` rather than `|| default` on both booleans: a stored `false`
    // is a value, and `||` would read it as absent and seed the column default
    // — so editing a rule that had plan/apply turned off would silently offer
    // to turn it back on (#1966).
    setPlanApplyEnabled(a['plan-apply-enabled'] ?? true)
    setConfigureEnabled(a['configure-enabled'] ?? false)
    setAutoConfigureEnabled(a['auto-configure-enabled'] ?? false)
    setConfigureAfterApply(a['configure-after-apply'] ?? false)
    setConfigureAllHosts(a['configure-all-hosts'] ?? false)
    setDynamicInventoryEnabled(a['dynamic-inventory-enabled'] ?? false)
    setInventoryScriptsEnabled(a['inventory-scripts-enabled'] ?? false)
    setAnsibleHostKeyChecking(a['ansible-host-key-checking'] ?? true)
    // `?? ''` and nothing to decide: a stored '' is the configuration
    // version's root and seeds an empty box, which is the same box an older
    // API's missing key seeds (#1966).
    setConfigDirectory(a['config-directory'] ?? '')
    setInventoryPaths(a['inventory-paths'] || [])
    setDefaultPlaybook(a['default-playbook'] || '')
    setDefaultLimit(a['default-limit'] || '')
    setResourceCpu(a['resource-cpu'])
    setParallelism(String(a.parallelism ?? 10))
    setEngine(a.engine || 'terraform')
    setBindPlan(Boolean(a['pulumi-bind-plan']))
    setResourceMemory(a['resource-memory'])
    setEeResourceCpu(a['ee-resource-cpu'] ?? '')
    setEeResourceMemory(a['ee-resource-memory'] ?? '')
    // Fall back to the boolean for a rule created before #1274.
    setAutoApplyMode(a['auto-apply-mode'] || (a['auto-apply'] ? 'always' : 'never'))
    setOnDirectoryDelete(a['on-directory-delete'] === 'destroy' ? 'destroy' : 'flag')
    setLabels(a.labels || {})
    setOwnerEmail(a['owner-email'] || '')
    setVarFiles(a['var-files'] || [])
    setOidcAudiences(a['oidc-audiences'] || {})
    setExecutionHookTemplates(a['execution-hook-templates'] || [])
    setScanEnforcement(a['security-scan-enforcement'] || 'advisory')
    setScanEngine(a['security-scan-engine'] || 'checkov')
    setScanThreshold(a['security-scan-severity-threshold'] || 'high')
    setAiSummaryMode(a['ai-summary-mode'] || 'default')
    setAiPolicyMode(a['ai-policy-mode'] || 'default')
    setAiSummaryContext(a['ai-summary-context'] || '')
    setTerragruntEnabled(a['terragrunt-enabled'] ?? false)
    setTerragruntVersion(a['terragrunt-version'] || '1.0')
    setRuleVcsWorkflow(a['vcs-workflow'] || 'merge_then_apply')
    setRuleAutoMerge(a['auto-merge'] ?? false)
    setRuleAutoMergeStrategy(a['auto-merge-strategy'] || 'merge')
    setDriftEnabled(a['drift-detection-enabled'] ?? true)
    setDriftInterval(a['drift-detection-interval-seconds'] ?? 86400)
    setDriftIgnoreRules(a['drift-ignore-rules'] || [])
    setPlanExpiry(a['plan-expiry-seconds'] ? String(a['plan-expiry-seconds']) : '')
    setRuleSlackChannel(a['slack-channel'] || '')
    setRuleDebugMode(a['debug-mode'] ?? false)
    setRuleAllowForkPrPlans(a['allow-fork-pr-plans'] ?? false)
    setRunTaskTemplates(a['run-task-templates'] || [])
    setNotificationTemplates(a['notification-templates'] || [])
    setShowForm(true)
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    setError('')
    setSuccess('')

    const ignorePatterns = ignorePatternsText
      .split('\n')
      .map(s => s.trim())
      .filter(Boolean)

    const attrs: Record<string, unknown> = {
      name,
      'vcs-connection-id': vcsConnectionId,
      'repo-url': repoUrl,
      branch,
      pattern,
      'ignore-patterns': ignorePatterns,
      'name-template': nameTemplate,
      enabled,
      'execution-mode': executionMode,
      'execution-backend': executionBackend,
      'agent-pool-id': agentPoolId || null,
      'engine-version': terraformVersion,
      'ansible-version': ansibleVersion.trim(),
      'ansible-execution-environment': ansibleEE.trim(),
      'ansible-collections': parseCollectionLines(ansibleCollections),
      // Which operations the materialised workspaces have (#1966). Always
      // sent, like every other attribute in this form: the rule holds a
      // concrete value for each, so there is no "leave alone" to express.
      'plan-apply-enabled': planApplyEnabled,
      'configure-enabled': configureEnabled,
      'auto-configure-enabled': autoConfigureEnabled,
      'configure-after-apply': configureAfterApply,
      'configure-all-hosts': configureAllHosts,
      'dynamic-inventory-enabled': dynamicInventoryEnabled,
      'inventory-scripts-enabled': inventoryScriptsEnabled,
      'ansible-host-key-checking': ansibleHostKeyChecking,
      // Where the configure operation runs (#1929). Sent verbatim and never
      // null — an explicit null is a 422 naming the field (#1966) — with ''
      // meaning the configuration version's root.
      'config-directory': configDirectory.trim(),
      'inventory-paths': inventoryPaths.map(s => s.trim()).filter(Boolean),
      'default-playbook': defaultPlaybook.trim(),
      'default-limit': defaultLimit.trim(),
      'resource-cpu': resourceCpu,
      'parallelism': Number(parallelism),
      engine,
      ...(engine === 'pulumi' ? { 'pulumi-bind-plan': bindPlan } : {}),
      'resource-memory': resourceMemory,
      'ee-resource-cpu': eeResourceCpu.trim(),
      'ee-resource-memory': eeResourceMemory.trim(),
      'auto-apply-mode': autoApplyMode,
      'on-directory-delete': onDirectoryDelete,
      labels,
      'owner-email': ownerEmail,
      'var-files': varFiles.map(s => s.trim()).filter(Boolean),
      // Blank audience rows are refused rather than dropped, and so is a
      // provider whose list came out empty, so an empty row left in the editor
      // would otherwise 422 the whole rule.
      'oidc-audiences': sanitizeOidcAudiences(oidcAudiences),
      'execution-hook-templates': executionHookTemplates.map(s => s.trim()).filter(Boolean),
      // Omitted entirely on a Pulumi rule, because the rule endpoint refuses
      // the mere PRESENCE of either key there: a Pulumi run is not
      // security-scanned, so `_rule_attrs` 422s with "<key> applies only to a
      // Terraform rule" and then defaults the enforcement to `off` itself.
      //
      // The form sent both unconditionally, so **no Pulumi rule could be
      // created or saved from this page at all** -- `engine: 'pulumi'` plus
      // `security-scan-enforcement: 'advisory'` is a guaranteed 422, which is
      // every Pulumi rule this form has ever submitted. Shaped exactly like
      // the `pulumi-bind-plan` line above, which is the mirror case: a setting
      // the endpoint accepts for one engine and refuses for the other.
      //
      // The threshold is NOT in here: it is engine-agnostic and the endpoint
      // takes it either way.
      ...(engine === 'pulumi'
        ? {}
        : {
            'security-scan-enforcement': scanEnforcement,
            'security-scan-engine': scanEngine,
          }),
      'security-scan-severity-threshold': scanThreshold,
      'ai-summary-mode': aiSummaryMode,
      'ai-policy-mode': aiPolicyMode,
      'ai-summary-context': aiSummaryContext,
      'terragrunt-enabled': terragruntEnabled,
      'terragrunt-version': terragruntVersion.trim() || '1.0',
      'vcs-workflow': ruleVcsWorkflow,
      'auto-merge': ruleAutoMerge,
      'auto-merge-strategy': ruleAutoMergeStrategy,
      'drift-detection-enabled': driftEnabled,
      'drift-detection-interval-seconds': driftInterval,
      'drift-ignore-rules': driftIgnoreRules.map(r => r.trim()).filter(Boolean),
      'plan-expiry-seconds': planExpiry.trim() ? Number(planExpiry) : null,
      'slack-channel': ruleSlackChannel.trim(),
      'debug-mode': ruleDebugMode,
      'allow-fork-pr-plans': ruleAllowForkPrPlans,
      'run-task-templates': runTaskTemplates,
      'notification-templates': notificationTemplates,
    }

    try {
      const path = editingId
        ? `/api/terrapod/v1/autodiscovery-rules/${editingId}`
        : '/api/terrapod/v1/autodiscovery-rules'
      const method = editingId ? 'PATCH' : 'POST'
      const res = await apiFetch(path, {
        method,
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'autodiscovery-rules', attributes: attrs } }),
      })
      if (!res.ok) {
        // Through `parseApiError`, not `res.text()` (#2413 F2). The server's
        // refusals here NAME the thing they refuse -- a malformed collection
        // comes back as "ansible-collections: 'community general' is not a
        // collection name" -- and collection parsing deliberately validates
        // nothing in the browser, so this message is the WHOLE of the feedback.
        // Interpolating the raw body shipped it wrapped in a JSON envelope.
        throw new Error(await parseApiError(res, t('errors.save')))
      }
      setSuccess(editingId ? t('success.updated', { name }) : t('success.created', { name }))
      setShowForm(false)
      resetForm()
      loadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.save'))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleDelete(id: string) {
    setError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/autodiscovery-rules/${id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(t('errors.deleteRule'))
      setSuccess(t('success.deleted'))
      setDeleteId(null)
      loadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.delete'))
    }
  }

  async function openPreview(id: string, ruleName: string) {
    setPreviewModal({ id, ruleName, loading: true })
    try {
      const res = await apiFetch(`/api/terrapod/v1/autodiscovery-rules/${id}/preview`)
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || t('errors.previewFailed', { status: res.status }))
      }
      const json = await res.json()
      const attrs = json?.data?.attributes ?? {}
      setPreviewModal({
        id,
        ruleName,
        ref: attrs.ref,
        filesWalked: attrs['files-walked'],
        entries: attrs.entries ?? [],
      })
    } catch (err) {
      setPreviewModal({
        id,
        ruleName,
        error: err instanceof Error ? err.message : t('errors.preview'),
      })
    }
  }

  // Preview an *unsaved* rule — the create/edit form's "Preview" button
  // wires here so the operator can iterate on pattern + name_template +
  // ignore_patterns before any persistence (which would trigger an
  // immediate initial scan).
  async function previewFormRule() {
    if (!vcsConnectionId || !repoUrl || !pattern) {
      setError(t('errors.previewPrereq'))
      return
    }
    // id is empty for the unsaved-rule preview; the modal hides the
    // Provision button when collisions === 0 or when no id is set.
    setPreviewModal({ id: '', ruleName: name || t('unsavedRule'), loading: true })
    try {
      const payload = {
        data: {
          type: 'autodiscovery-rules',
          attributes: {
            name: name || 'preview',
            'vcs-connection-id': vcsConnectionId,
            'repo-url': repoUrl,
            branch,
            pattern,
            'ignore-patterns': ignorePatternsText
              .split('\n')
              .map(s => s.trim())
              .filter(Boolean),
            'name-template': nameTemplate,
            'execution-mode': executionMode,
            'agent-pool-id': agentPoolId || null,
            'execution-backend': executionBackend,
            'engine-version': terraformVersion,
            'resource-cpu': resourceCpu,
      'parallelism': Number(parallelism),
            'resource-memory': resourceMemory,
            'ee-resource-cpu': eeResourceCpu.trim(),
            'ee-resource-memory': eeResourceMemory.trim(),
            'auto-apply-mode': autoApplyMode,
            labels,
            'owner-email': ownerEmail,
          },
        },
      }
      const res = await apiFetch('/api/terrapod/v1/autodiscovery-rules/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify(payload),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || t('errors.previewFailed', { status: res.status }))
      }
      const json = await res.json()
      const attrs = json?.data?.attributes ?? {}
      setPreviewModal({
        id: '',
        ruleName: name || t('unsavedRule'),
        ref: attrs.ref,
        filesWalked: attrs['files-walked'],
        entries: attrs.entries ?? [],
      })
    } catch (err) {
      setPreviewModal({
        id: '',
        ruleName: name || t('unsavedRule'),
        error: err instanceof Error ? err.message : t('errors.preview'),
      })
    }
  }

  async function handleProvision() {
    if (!previewModal) return
    setPreviewModal({ ...previewModal, scanning: true, error: undefined })
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/autodiscovery-rules/${previewModal.id}/scan`,
        { method: 'POST' },
      )
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || t('errors.scanFailed', { status: res.status }))
      }
      const json = await res.json()
      const created = json?.data?.attributes?.['workspaces-created'] ?? 0
      setSuccess(t('success.provisioned', { count: created, ruleName: previewModal.ruleName }))
      setPreviewModal(null)
      loadAll()
    } catch (err) {
      setPreviewModal({
        ...previewModal,
        scanning: false,
        error: err instanceof Error ? err.message : t('errors.scan'),
      })
    }
  }

  // ── the workspace template, in the shared sections (#2413 F4) ───────────
  //
  // The rule's own fields -- what it scans and what it does with what it finds
  // -- stay hand-written above. Everything it TEMPLATES onto the workspaces it
  // materialises is rendered from `FIELDS`, in `GROUPS` order, by the same
  // `Group`/`Field` pair the create form and the Configuration tab use.
  //
  // What that buys, and it is the whole of F3, F5 and F6 as well as F4:
  //
  //   - the orchestrator's CPU and memory requests are adjacent, because the
  //     table says they are, instead of being separated by the Ansible pair
  //     and the engine picker (F3);
  //   - a section title is a real `<h4>` rather than a `<label>` pointing at
  //     nothing, and there is one per group instead of four spanning three or
  //     four groups each (F5);
  //   - a field whose gate is unmet renders DISABLED (F6 as #2413 F20 reshaped
  //     it) -- so with Configure set to No the whole Ansible block is visible
  //     and untypeable, where before all of it rendered live and the
  //     materialised workspace ignored every value;
  //   - nothing collapses: the `<details>` this replaced hid the entire
  //     template behind one click (F4).
  //
  // Four fields the table declares are deliberately NOT rendered here, and
  // each is a judgement rather than an omission:
  //
  //   `name` and `working-directory` -- the rule DERIVES both, from
  //     `name-template` and from the directory a match was found in. Offering
  //     either would take a value the materialiser overwrites.
  //   `vcs-connection-id`, `vcs-repo-url`, `vcs-branch` -- these are the
  //     rule's own SCAN TARGET, above, not a template. A rule has exactly one
  //     and the workspaces it creates inherit it, so a second control would
  //     be two ways to say one thing.
  //   `trigger-prefixes` and `security-scan-skip-rules` -- exempt in
  //     `test_workspace_setting_parity.py` with a reason each.
  //
  // `vcs-connection-id` is still in `ruleValues` below even though it has no
  // control: three vcs fields gate on it, so leaving it out would disable the
  // VCS workflow and auto-merge controls on every rule for ever.

  /**
   * The template's current values, keyed as the API names the attribute.
   *
   * A projection of the form's own state rather than a second store: the
   * payload in `handleSubmit` is still built from the `useState` values, so
   * this cannot drift into being the source of truth for what gets saved.
   * Booleans are real booleans -- `RuleYesNo` renders the yes/no pair itself,
   * and `dependencyMet` reads a boolean gate directly.
   */
  const ruleValues: Record<string, unknown> = {
    engine,
    'execution-mode': executionMode,
    'execution-backend': executionBackend,
    'engine-version': terraformVersion,
    'auto-apply-mode': autoApplyMode,
    'terragrunt-enabled': terragruntEnabled,
    'terragrunt-version': terragruntVersion,
    'pulumi-bind-plan': bindPlan,
    parallelism,
    'resource-cpu': resourceCpu,
    'resource-memory': resourceMemory,
    'plan-apply-enabled': planApplyEnabled,
    'configure-enabled': configureEnabled,
    'auto-configure-enabled': autoConfigureEnabled,
    'configure-after-apply': configureAfterApply,
    'configure-all-hosts': configureAllHosts,
    'config-directory': configDirectory,
    'default-playbook': defaultPlaybook,
    'default-limit': defaultLimit,
    'inventory-paths': inventoryPaths,
    'dynamic-inventory-enabled': dynamicInventoryEnabled,
    'inventory-scripts-enabled': inventoryScriptsEnabled,
    'ansible-host-key-checking': ansibleHostKeyChecking,
    'ansible-version': ansibleVersion,
    'ansible-execution-environment': ansibleEE,
    'ansible-collections': ansibleCollections,
    'ee-resource-cpu': eeResourceCpu,
    'ee-resource-memory': eeResourceMemory,
    'agent-pool-ids': agentPoolId,
    // No control of its own -- the rule's connection is chosen above. Here
    // because `vcs-workflow` and `auto-merge` gate on it.
    'vcs-connection-id': vcsConnectionId,
    'vcs-workflow': ruleVcsWorkflow,
    'auto-merge': ruleAutoMerge,
    'auto-merge-strategy': ruleAutoMergeStrategy,
    'allow-fork-pr-plans': ruleAllowForkPrPlans,
    'drift-detection-enabled': driftEnabled,
    'drift-detection-interval-seconds': driftInterval,
    'drift-ignore-rules': driftIgnoreRules,
    'plan-expiry-seconds': planExpiry,
    'ai-summary-mode': aiSummaryMode,
    'ai-summary-context': aiSummaryContext,
    'ai-policy-mode': aiPolicyMode,
    'security-scan-engine': scanEngine,
    'security-scan-enforcement': scanEnforcement,
    'security-scan-severity-threshold': scanThreshold,
    'owner-email': ownerEmail,
    labels,
    'var-files': varFiles,
    'oidc-audiences': oidcAudiences,
    'debug-mode': ruleDebugMode,
    'slack-channel': ruleSlackChannel,
  }

  /**
   * One dispatch for every control in the template, bespoke ones included.
   *
   * Every attribute this form renders has a case -- including the ones whose
   * control is hand-written below, which route through here rather than
   * calling their setter inline. A control that set its own state directly
   * would be the one place a future switch to a `Field` primitive silently
   * stopped writing anything, and a no-op control is indistinguishable from a
   * working one until a save comes back without the value.
   *
   * `execution-mode` has no case because it is read-only: `RULE_READ_ONLY`
   * disables it, so its `onChange` cannot fire.
   */
  function setRuleValue(attr: string, v: unknown) {
    const s = String(v ?? '')
    const b = Boolean(v)
    switch (attr) {
      case 'engine': setEngine(s); break
      case 'execution-backend': setExecutionBackend(s as 'tofu' | 'terraform'); break
      case 'engine-version': setTerraformVersion(s); break
      case 'auto-apply-mode': setAutoApplyMode(s); break
      case 'terragrunt-enabled': setTerragruntEnabled(b); break
      case 'terragrunt-version': setTerragruntVersion(s); break
      case 'pulumi-bind-plan': setBindPlan(b); break
      case 'parallelism': setParallelism(s); break
      case 'resource-cpu': setResourceCpu(s); break
      case 'resource-memory': setResourceMemory(s); break
      case 'plan-apply-enabled': setPlanApplyEnabled(b); break
      case 'configure-enabled': setConfigureEnabled(b); break
      case 'auto-configure-enabled': setAutoConfigureEnabled(b); break
      case 'configure-after-apply': setConfigureAfterApply(b); break
      case 'configure-all-hosts': setConfigureAllHosts(b); break
      case 'config-directory': setConfigDirectory(s); break
      case 'default-playbook': setDefaultPlaybook(s); break
      case 'default-limit': setDefaultLimit(s); break
      case 'inventory-paths': setInventoryPaths(v as string[]); break
      case 'dynamic-inventory-enabled': setDynamicInventoryEnabled(b); break
      case 'inventory-scripts-enabled': setInventoryScriptsEnabled(b); break
      case 'ansible-host-key-checking': setAnsibleHostKeyChecking(b); break
      case 'ansible-version': setAnsibleVersion(s); break
      case 'ansible-execution-environment': setAnsibleEE(s); break
      case 'ansible-collections': setAnsibleCollections(v as string[]); break
      case 'ee-resource-cpu': setEeResourceCpu(s); break
      case 'ee-resource-memory': setEeResourceMemory(s); break
      case 'agent-pool-ids': setAgentPoolId(s); break
      case 'vcs-workflow': setRuleVcsWorkflow(s); break
      case 'auto-merge': setRuleAutoMerge(b); break
      case 'auto-merge-strategy': setRuleAutoMergeStrategy(s); break
      case 'allow-fork-pr-plans': setRuleAllowForkPrPlans(b); break
      case 'drift-detection-enabled': setDriftEnabled(b); break
      case 'drift-detection-interval-seconds': setDriftInterval(Number(v)); break
      case 'drift-ignore-rules': setDriftIgnoreRules(v as string[]); break
      case 'plan-expiry-seconds': setPlanExpiry(s); break
      case 'ai-summary-mode': setAiSummaryMode(s); break
      case 'ai-summary-context': setAiSummaryContext(s); break
      case 'ai-policy-mode': setAiPolicyMode(s); break
      case 'security-scan-engine': setScanEngine(s); break
      case 'security-scan-enforcement': setScanEnforcement(s); break
      case 'security-scan-severity-threshold': setScanThreshold(s); break
      case 'owner-email': setOwnerEmail(s); break
      case 'labels': setLabels(v as Record<string, string>); break
      case 'var-files': setVarFiles(v as string[]); break
      case 'oidc-audiences': setOidcAudiences(sanitizeOidcAudiences(v as OidcAudiences)); break
      case 'debug-mode': setRuleDebugMode(b); break
      case 'slack-channel': setRuleSlackChannel(s); break
    }
  }

  /**
   * The label for a templated attribute.
   *
   * `LABEL_KEY` by default, so a rule's template and the setting it produces
   * are described in the same words in every locale -- the reason this page
   * already reads `workspaceDetail` rather than carrying its own copies.
   *
   * Two overrides, both because the CONTROL differs in cardinality and the
   * shared label would then be wrong rather than merely different:
   * `agent-pool-ids` is a list on a workspace and a single pool here, and
   * `engine-version` is the version of whichever engine the rule templates,
   * which the rule form has always named after the tool.
   */
  function ruleLabel(attr: string): string {
    if (attr === 'agent-pool-ids') return t('form.agentPool')
    if (attr === 'engine-version') return t('form.terraformVersion')
    return tWs(LABEL_KEY[attr])
  }

  const hintText = (node: ReactNode) => <p className="mt-1 text-xs text-slate-500">{node}</p>

  /**
   * The explanatory line under a templated field.
   *
   * Every one of these already existed on this form or on the create form, so
   * nothing here is a new string in 32 catalogues.
   *
   * Four of them are a `title=` this form used to hang on a label, moved to a
   * visible line: `ansible-version`, `ansible-execution-environment`,
   * `default-playbook` and `default-limit` each explained themselves on hover
   * only, which AGENTS.md forbids, and `Field` has no label `title` to hang
   * them on. Moved rather than dropped -- the information is what an operator
   * needs to pick a value. `config-directory` keeps only its short line and
   * loses the 257-character `configDirectoryTitle`, matching the create form:
   * it already has an always-on sentence, and the mechanism behind it is
   * exactly what #2413 F15's ⓘ is for.
   */
  function ruleHint(attr: string): ReactNode {
    switch (attr) {
      case 'execution-mode':
        return hintText(t('form.executionModeHint'))
      case 'pulumi-bind-plan':
        // `t.rich`, not `t`: this key carries a <docs> link, and plain `t`
        // throws FORMATTING_ERROR on a missing tag. The create form records
        // the same trap -- bind-plan renders for Pulumi only, so a form that
        // defaults to Terraform never reveals it until somebody switches.
        return (
          <p className="mt-1 text-xs text-slate-500">
            {tWs.rich('fields.bindPlanHelp', {
              docs: (chunks) => (
                <a
                  href="https://github.com/mattrobinsonsre/terrapod/blob/main/docs/pulumi-cli-surface.md"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-brand-400 hover:text-brand-300 underline"
                >
                  {chunks}
                </a>
              ),
            })}
          </p>
        )
      case 'plan-apply-enabled':
        return hintText(tWs('fields.planApplyEnabledHint'))
      case 'configure-enabled':
        return hintText(tWs('fields.configureEnabledHint'))
      case 'auto-configure-enabled':
        return hintText(tWs('fields.autoConfigureEnabledHint'))
      case 'configure-after-apply':
        return hintText(tWs('fields.configureAfterApplyHint'))
      case 'configure-all-hosts':
        return hintText(tWs('fields.configureAllHostsHint'))
      case 'config-directory':
        return hintText(tWs('fields.configDirectoryRootHint'))
      case 'default-playbook':
        return hintText(tWs('fields.defaultPlaybookTitle'))
      case 'default-limit':
        return hintText(tWs('fields.defaultLimitTitle'))
      case 'inventory-paths':
        return hintText(tWs('fields.inventoryPathsHint'))
      case 'dynamic-inventory-enabled':
        return hintText(tWs('fields.dynamicInventoryEnabledHint'))
      case 'inventory-scripts-enabled':
        return hintText(tWs('fields.inventoryScriptsEnabledHint'))
      case 'ansible-host-key-checking':
        return hintText(tWs('fields.ansibleHostKeyCheckingHint'))
      case 'ansible-version':
        return hintText(
          envs.listing === 'complete' && envs.lines.length > 0
            ? tWs('fields.ansibleVersionTitle')
            : // The `unknown` branch says BOTH things: what the field is for,
              // and that the offered set could not be confirmed. The second
              // used to be a `title` on the input, which is where an operator
              // looking at an unexpected free-text box would never find it.
              `${tWs('fields.ansibleVersionTitle')} ${tWs('fields.ansibleVersionUnconfirmed')}`,
        )
      case 'ansible-execution-environment':
        return hintText(tWs('fields.ansibleExecutionEnvironmentTitle'))
      case 'ansible-collections':
        return hintText(tWs('fields.ansibleCollectionsHint'))
      case 'var-files':
        // `t.rich`: carries a <code> tag.
        return (
          <p className="mt-1 text-xs text-slate-500">
            {t.rich('form.varFilesHint', { code: (c) => <code>{c}</code> })}
          </p>
        )
      case 'oidc-audiences':
        return hintText(t('form.oidcAudiencesHint'))
      case 'ai-summary-context':
        return hintText(tWs('aiSummary.contextHint'))
      case 'debug-mode':
        return hintText(t('form.debugModeHint'))
      case 'allow-fork-pr-plans':
        return hintText(t('form.allowForkPrPlansHint'))
      case 'plan-expiry-seconds':
        return undefined
      default:
        return undefined
    }
  }

  const ruleCtx: GroupCtx = {
    mode: 'create',
    values: ruleValues,
    onChange: setRuleValue,
    engine,
    label: ruleLabel,
    hint: ruleHint,
    // `execution-mode` always, plus `security-scan-engine` on a PULUMI rule.
    //
    // The endpoint refuses the mere PRESENCE of either scan key on a Pulumi
    // rule (`autodiscovery_rules.py`), so the payload omits both -- but the
    // shared table hides `security-scan-enforcement` and NOT the engine
    // picker, because `engines: ['terraform']` there would also hide it on a
    // Pulumi WORKSPACE, where the endpoint does accept it, and two other
    // surfaces read that table.
    //
    // So the control would otherwise render live while its value is silently
    // discarded, which is the lie F20 exists to remove: a field the surface
    // cannot write is rendered DISABLED, not rendered as though it worked.
    // `GroupCtx.readOnly` is exactly that hook -- "attributes this caller may
    // not change, rendered disabled rather than hidden" -- so this needs no
    // change to the shared component and no rule-local escape hatch.
    readOnly:
      engine === 'pulumi'
        ? new Set([...RULE_READ_ONLY, 'security-scan-engine'])
        : RULE_READ_ONLY,
    busy: submitting,
  }

  // Option sets. Every label is a `t(...)` call or a wire identifier read out
  // of an array, never a literal in the JSX below -- a raw literal would be a
  // new entry in the hardcoded-string allowlist.
  const engineOptions = [
    { value: 'terraform', label: tWs('fields.engineTerraform') },
    { value: 'pulumi', label: tWs('fields.enginePulumi') },
  ]
  // Only agent: autodiscovery is VCS-driven, so there is one value and
  // `RULE_READ_ONLY` disables the control rather than hiding the choice.
  const execModeOptions = [{ value: 'agent', label: tWs('fields.execModeAgent') }]
  // The two Terraform-family binaries. Product names, so not translated in any
  // catalogue; read from here rather than written into the JSX so they are not
  // new hardcoded literals either.
  const backendOptions = [
    { value: 'tofu', label: 'OpenTofu' },
    { value: 'terraform', label: 'Terraform' },
  ]
  const autoApplyOptions = [
    { value: 'never', label: tMode('never') },
    { value: 'always', label: tMode('always') },
    { value: 'create', label: tMode('create') },
    { value: 'create_update', label: tMode('createUpdate') },
  ]
  const vcsWorkflowOptions = [
    { value: 'merge_then_apply', label: tWs('fields.vcsWorkflowMergeThenApply') },
    { value: 'apply_then_merge', label: tWs('fields.vcsWorkflowApplyThenMerge') },
  ]
  // Git's own merge-method names, as the API spells them.
  const mergeStrategyOptions = ['merge', 'squash', 'rebase'].map((s) => ({ value: s, label: s }))
  const aiModeOptions = [
    { value: 'default', label: tWs('aiSummary.modeDefault') },
    { value: 'enabled', label: tWs('aiSummary.modeEnabled') },
    { value: 'disabled', label: tWs('aiSummary.modeDisabled') },
  ]
  const scanEnforcementOptions = [
    { value: 'off', label: tWs('securityScan.enforcementOff') },
    { value: 'advisory', label: tWs('securityScan.enforcementAdvisory') },
    { value: 'enforced', label: tWs('securityScan.enforcementEnforced') },
  ]
  const scanEngineOptions = [
    { value: 'checkov', label: tWs('securityScan.engineCheckov') },
    { value: 'trivy', label: tWs('securityScan.engineTrivy') },
    { value: 'both', label: tWs('securityScan.engineBoth') },
  ]
  const scanThresholdOptions = [
    { value: 'critical', label: tWs('securityScan.thresholdCritical') },
    { value: 'high', label: tWs('securityScan.thresholdHigh') },
    { value: 'medium', label: tWs('securityScan.thresholdMedium') },
    { value: 'low', label: tWs('securityScan.thresholdLow') },
  ]

  /**
   * A group's own sentence, where one exists.
   *
   * The four sections this replaced each carried a line of prose, and three of
   * them said the same thing -- "applied to every workspace this rule creates"
   * -- which now appears once above the whole template rather than three times
   * inside it. What stays is the two that say something specific to their
   * group, plus the both-operations-off banner, which belongs beside the two
   * controls it is about rather than beside the Save button it disables.
   */
  function groupDescription(g: GroupId): ReactNode {
    if (g === 'operations') {
      return (
        <>
          <p className="mb-2 text-xs text-slate-500">{tWs('fields.operationsHint')}</p>
          {ruleHasNoOperations && (
            <p
              className="mb-2 rounded-lg border border-red-500/50 bg-red-500/10 p-2 text-xs text-red-300"
              data-testid="ad-operations-none"
            >
              {tWs('fields.operationsNone')}
            </p>
          )}
        </>
      )
    }
    if (g === 'ansible') {
      return <p className="mb-2 text-xs text-slate-500">{t('form.ansibleConfigureHint')}</p>
    }
    return undefined
  }

  /**
   * The groups' contents, keyed by group so the render walks `GROUPS` and
   * keeps this form in the table's order.
   */
  const groupBody: Record<GroupId, ReactNode> = {
    // `name` is absent: the rule derives every workspace name from
    // `name-template` above, so a control here would be overwritten.
    identity: <Field attr="engine" ctx={ruleCtx} kind="select" options={engineOptions} />,

    execution: (
      <>
        <Field attr="execution-mode" ctx={ruleCtx} kind="select" options={execModeOptions} />
        <Field attr="execution-backend" ctx={ruleCtx} kind="select" options={backendOptions} />
        <Field attr="engine-version" ctx={ruleCtx} kind="text" />
        {/* `working-directory` is absent: the rule sets it to the directory a
            match was found in, so an operator's value would be overwritten. */}
        <Field attr="auto-apply-mode" ctx={ruleCtx} kind="select" options={autoApplyOptions} />
        {/* Was a checkbox with the version input tucked underneath it, labelled
            only by `aria-label` (#2413 F4). Two fields, two real labels. */}
        <Field attr="terragrunt-enabled" ctx={ruleCtx} kind="checkbox" />
        <Field
          attr="terragrunt-version"
          ctx={ruleCtx}
          kind="text"
          placeholder={tWs('fields.terragruntVersionPlaceholder')}
        />
        <RuleYesNo attr="pulumi-bind-plan" ctx={ruleCtx} />
        {/* The three the orchestrator sizes itself with, adjacent because the
            table says they are -- they used to be separated by the Ansible
            pair and the engine picker (#2413 F3). */}
        <Field attr="parallelism" ctx={ruleCtx} kind="number" min={1} max={256} />
        <Field attr="resource-cpu" ctx={ruleCtx} kind="text" />
        <Field attr="resource-memory" ctx={ruleCtx} kind="text" />
      </>
    ),

    operations: (
      <>
        <RuleYesNo attr="plan-apply-enabled" ctx={ruleCtx} />
        <RuleYesNo attr="configure-enabled" ctx={ruleCtx} />
        <RuleYesNo attr="auto-configure-enabled" ctx={ruleCtx} />
        <RuleYesNo attr="configure-after-apply" ctx={ruleCtx} />
        <RuleYesNo attr="configure-all-hosts" ctx={ruleCtx} />
      </>
    ),

    // Every field here gates on `configure-enabled`, so with Configure set to
    // No the whole block renders disabled rather than live-and-ignored
    // (#2413 F6, in the shape #2413 F20 gave it).
    ansible: (
      <>
        <Field
          attr="config-directory"
          ctx={ruleCtx}
          kind="text"
          placeholder={tWs('fields.configDirectoryPlaceholder')}
        />
        <Field
          attr="default-playbook"
          ctx={ruleCtx}
          kind="text"
          placeholder={tWs('fields.defaultPlaybookPlaceholder')}
        />
        <Field
          attr="default-limit"
          ctx={ruleCtx}
          kind="text"
          placeholder={tWs('fields.defaultLimitPlaceholder')}
        />
        <Field
          attr="inventory-paths"
          ctx={ruleCtx}
          kind="list"
          control={
            <StringListEditor
              values={inventoryPaths}
              onChange={(next) => setRuleValue('inventory-paths', next)}
              placeholder={tWs('fields.inventoryPathsPlaceholder')}
              addLabel={tWs('fields.inventoryPathsAdd')}
            />
          }
        />
        {/* Which inventory sources ansible may resolve on the runner (#2033).
            Terrapod itself never runs either kind, in either state. */}
        <RuleYesNo attr="dynamic-inventory-enabled" ctx={ruleCtx} />
        <RuleYesNo attr="inventory-scripts-enabled" ctx={ruleCtx} />
        {/* Defaults to yes, which is both the rule column's default and
            ansible's own behaviour (#2318). */}
        <RuleYesNo attr="ansible-host-key-checking" ctx={ruleCtx} />
        <Field
          attr="ansible-version"
          ctx={ruleCtx}
          kind="text"
          control={
            envs.listing === 'complete' && envs.lines.length > 0 ? (
              <select
                id="ws-ansible-version"
                data-testid="rule-ansible-version"
                value={ansibleVersion}
                disabled={submitting}
                onChange={(e) => setRuleValue('ansible-version', e.target.value)}
                className={CREATE_INPUT}
              >
                <option value="">{t('form.none')}</option>
                {envs.lines.map((l) => (
                  <option key={l} value={l}>
                    {l}
                  </option>
                ))}
              </select>
            ) : (
              /* `unknown` means the server accepts ANY line, so free text is
                 correct here -- an empty picker would block work the server
                 would have allowed. The placeholder names a LINE, not a patch:
                 a patch pins an immutable execution-environment tag, which
                 never receives a patched rebuild (#2413 F1). */
              <input
                id="ws-ansible-version"
                data-testid="rule-ansible-version"
                value={ansibleVersion}
                disabled={submitting}
                onChange={(e) => setRuleValue('ansible-version', e.target.value)}
                placeholder={tWs('fields.ansibleVersionPlaceholder')}
                className={CREATE_INPUT}
              />
            )
          }
        />
        <Field
          attr="ansible-execution-environment"
          ctx={ruleCtx}
          kind="select"
          control={
            /* Always a picker: operator labels come from configuration, not
               from a registry, so they are authoritative even when the
               built-in listing is unknown. A label the deployment has not
               configured is refused at the write. */
            <select
              id="ws-ansible-ee"
              data-testid="rule-ansible-ee"
              value={ansibleEE}
              disabled={submitting || envs.labels.length === 0}
              onChange={(e) => setRuleValue('ansible-execution-environment', e.target.value)}
              className={CREATE_INPUT}
            >
              <option value="">
                {envs.labels.length === 0
                  ? tWs('fields.ansibleExecutionEnvironmentNone')
                  : t('form.none')}
              </option>
              {envs.labels.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          }
        />
        {/* The wire shape is `{name, version?}` objects; the editor holds
            `name:version` lines and `handleSubmit` parses them, which is the
            same helper pair the create form uses. */}
        <Field
          attr="ansible-collections"
          ctx={ruleCtx}
          kind="list"
          control={
            <StringListEditor
              values={ansibleCollections}
              onChange={(next) => setRuleValue('ansible-collections', next)}
              placeholder={tWs('fields.ansibleCollectionsPlaceholder')}
              addLabel={tWs('fields.ansibleCollectionsAdd')}
            />
          }
        />
        {/* Empty is a real value and means "inherit the deployment default"
            (#2154), which is why neither is pre-filled. */}
        <Field
          attr="ee-resource-cpu"
          ctx={ruleCtx}
          kind="text"
          placeholder={t('form.eeInherit')}
        />
        <Field
          attr="ee-resource-memory"
          ctx={ruleCtx}
          kind="text"
          placeholder={t('form.eeInherit')}
        />
      </>
    ),

    pools: (
      /* ONE pool, where a workspace holds a set: the rule template carries a
         single `agent-pool-id`, so the label is this form's own rather than
         the shared plural. Exempt in `test_workspace_setting_parity.py` for
         exactly that reason. */
      <Field
        attr="agent-pool-ids"
        ctx={ruleCtx}
        kind="select"
        control={
          <select
            id="ws-pool"
            data-testid="ws-pool"
            value={agentPoolId}
            disabled={submitting}
            onChange={(e) => setRuleValue('agent-pool-ids', e.target.value)}
            className={CREATE_INPUT}
          >
            <option value="">{t('form.none')}</option>
            {pools.map((p) => (
              <option key={p.id} value={p.id}>
                {p.attributes.name}
              </option>
            ))}
          </select>
        }
      />
    ),

    // The rule's own connection, repository and branch are its SCAN TARGET and
    // stay above. What is templated is how a materialised workspace treats the
    // pull requests it sees.
    //
    // Three different gates among the four, and the third is the interesting
    // one: the workflow and auto-merge gate on `vcs-connection-id`, so they
    // are disabled until the rule's connection is chosen above; the strategy
    // gates on auto-merge; and `allow-fork-pr-plans` gates on NOTHING, by the
    // table's own decision -- it is a feature's own toggle, and hiding or
    // disabling one of those removes the only way to discover the feature
    // exists, where a dependent only loses something meaningless.
    vcs: (
      <>
        <Field attr="vcs-workflow" ctx={ruleCtx} kind="select" options={vcsWorkflowOptions} />
        <Field attr="auto-merge" ctx={ruleCtx} kind="checkbox" />
        <Field
          attr="auto-merge-strategy"
          ctx={ruleCtx}
          kind="select"
          options={mergeStrategyOptions}
        />
        <Field attr="allow-fork-pr-plans" ctx={ruleCtx} kind="checkbox" />
      </>
    ),

    governance: (
      <>
        <Field attr="drift-detection-enabled" ctx={ruleCtx} kind="checkbox" />
        <Field
          attr="drift-detection-interval-seconds"
          ctx={ruleCtx}
          kind="number"
          min={0}
        />
        <Field
          attr="drift-ignore-rules"
          ctx={ruleCtx}
          kind="list"
          control={
            <StringListEditor
              values={driftIgnoreRules}
              onChange={(next) => setRuleValue('drift-ignore-rules', next)}
              // A Terraform address, not UX copy — identical in every locale.
              placeholder="aws_instance.web.tags[&quot;LastSeen&quot;]" // i18n-ignore
              addLabel={t('form.addDriftIgnoreRule')}
            />
          }
        />
        <Field
          attr="plan-expiry-seconds"
          ctx={ruleCtx}
          kind="number"
          control={
            /* The state is a STRING whose emptiness means "no expiry", and
               `handleSubmit` sends null for it. The number primitive coerces
               with `Number()`, so clearing the box would send 0 -- a real
               value, and not the one the operator asked for. */
            <input
              id="ws-plan-expiry"
              data-testid="ws-plan-expiry"
              type="number"
              min={0}
              value={planExpiry}
              disabled={submitting}
              onChange={(e) => setRuleValue('plan-expiry-seconds', e.target.value)}
              placeholder={t('form.noExpiry')}
              className={CREATE_INPUT}
            />
          }
        />
      </>
    ),

    ai: (
      <>
        <Field attr="ai-summary-mode" ctx={ruleCtx} kind="select" options={aiModeOptions} />
        <Field attr="ai-policy-mode" ctx={ruleCtx} kind="select" options={aiModeOptions} />
        {/* Free prose up to 4000 characters, so a textarea rather than the
            single-line primitive. */}
        <Field
          attr="ai-summary-context"
          ctx={ruleCtx}
          kind="text"
          control={
            <textarea
              id="ws-ai-summary-context"
              data-testid="ws-ai-summary-context"
              rows={3}
              maxLength={4000}
              value={aiSummaryContext}
              disabled={submitting}
              onChange={(e) => setRuleValue('ai-summary-context', e.target.value)}
              placeholder={tWs('aiSummary.contextPlaceholder')}
              className={CREATE_INPUT}
            />
          }
        />
      </>
    ),

    security: (
      <>
        <Field
          attr="security-scan-enforcement"
          ctx={ruleCtx}
          kind="select"
          options={scanEnforcementOptions}
        />
        <Field
          attr="security-scan-engine"
          ctx={ruleCtx}
          kind="select"
          options={scanEngineOptions}
        />
        <Field
          attr="security-scan-severity-threshold"
          ctx={ruleCtx}
          kind="select"
          options={scanThresholdOptions}
        />
        {/* `security-scan-skip-rules` is absent and exempt: a skip list is
            per-workspace reasoning, not a rule default. */}
      </>
    ),

    metadata: (
      <>
        {/* Offered here, unlike on the create form: that path sets
            `owner_email` to the creator, where a rule materialises workspaces
            nobody is sitting in front of, so the template is the only place an
            owner can be named. */}
        <Field attr="owner-email" ctx={ruleCtx} kind="text" placeholder="platform@example.com" />
        <Field
          attr="labels"
          ctx={ruleCtx}
          kind="text"
          control={
            <LabelsEditor labels={labels} onChange={(next) => setRuleValue('labels', next)} />
          }
        />
        <Field
          attr="var-files"
          ctx={ruleCtx}
          kind="list"
          control={
            <StringListEditor
              values={varFiles}
              onChange={(next) => setRuleValue('var-files', next)}
              placeholder="env/prod.tfvars" // i18n-ignore
              addLabel={t('form.addVarFile')}
            />
          }
        />
      </>
    ),

    cloudIdentity: (
      /* A map of provider configuration to its audiences, not a list -- one
         token is minted per key. No provenance partition on this surface: the
         value IS the override, so there is nothing to subtract. */
      <Field
        attr="oidc-audiences"
        ctx={ruleCtx}
        kind="text"
        control={
          <OidcAudiencesEditor
            value={oidcAudiences}
            inert={!oidcDefaults.issuerEnabled}
            onChange={(next) => setRuleValue('oidc-audiences', next)}
            audiencePlaceholder={tWs('fields.oidcAudiencesPlaceholder')}
            addAudienceLabel={tWs('fields.oidcAudiencesAdd')}
          />
        }
      />
    ),

    diagnostics: (
      <>
        <Field attr="debug-mode" ctx={ruleCtx} kind="checkbox" />
        <Field
          attr="slack-channel"
          ctx={ruleCtx}
          kind="text"
          placeholder={tWs('slack.channelPlaceholder')}
        />
      </>
    ),
  }

  if (loading) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={t('title')}
          description={t('description')}
          actions={
            <button
              onClick={() => (showForm ? setShowForm(false) : openCreateForm())}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showForm ? t('actions.cancel') : t('actions.newRule')}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}
        {success && (
          <div className="mb-4 px-4 py-3 rounded-lg bg-green-900/30 border border-green-800 text-green-300 text-sm">
            {success}
          </div>
        )}

        {showForm && (
          <form onSubmit={handleSubmit} className="mb-6 p-6 rounded-lg bg-slate-900/60 border border-slate-800 space-y-4">
            <h2 className="text-lg font-semibold text-slate-100">
              {editingId ? t('form.editTitle') : t('form.createTitle')}
            </h2>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm text-slate-300 mb-1">{t('form.name')}</label>
                <input
                  required
                  value={name}
                  onChange={e => setName(e.target.value)}
                  placeholder="monorepo"
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label className="block text-sm text-slate-300 mb-1">{t('form.vcsConnection')}</label>
                <select
                  required
                  value={vcsConnectionId}
                  onChange={e => setVcsConnectionId(e.target.value)}
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                >
                  <option value="">{t('form.selectConnection')}</option>
                  {connections.map(c => (
                    <option key={c.id} value={c.id}>
                      {c.attributes.name} ({c.attributes.provider})
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm text-slate-300 mb-1">{t('form.repoUrl')}</label>
                <input
                  required
                  value={repoUrl}
                  onChange={e => setRepoUrl(e.target.value)}
                  placeholder="https://github.com/org/monorepo"
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label className="block text-sm text-slate-300 mb-1">{t('form.branch')}</label>
                <input
                  value={branch}
                  onChange={e => setBranch(e.target.value)}
                  placeholder="main"
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                />
              </div>
            </div>

            <div>
              <label className="block text-sm text-slate-300 mb-1">{t('form.matchPattern')}</label>
              <input
                required
                value={pattern}
                onChange={e => setPattern(e.target.value)}
                placeholder="accounts/*/**/*.tf"
                className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm font-mono"
              />
              <p className="text-xs text-slate-500 mt-1">{t.rich('form.matchPatternHint', { code: (c) => <code>{c}</code> })}</p>
            </div>

            <div>
              <label className="block text-sm text-slate-300 mb-1">{t('form.ignorePatterns')}</label>
              <textarea
                rows={3}
                value={ignorePatternsText}
                onChange={e => setIgnorePatternsText(e.target.value)}
                placeholder="modules/**&#10;deprecated/**"
                className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm font-mono"
              />
            </div>

            <div>
              <label className="block text-sm text-slate-300 mb-1">{t('form.nameTemplate')}</label>
              <input
                value={nameTemplate}
                onChange={e => setNameTemplate(e.target.value)}
                placeholder="ws-{path}"
                className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm font-mono"
              />
              <p className="text-xs text-slate-500 mt-1">{t.rich('form.nameTemplateHint', { code: (c) => <code>{c}</code>, path: () => <code>{'{path}'}</code>, root: () => <code>{'{root}'}</code> })}</p>
            </div>

            {/* The rule's own lifecycle: what it does when a matched
                directory disappears, and whether it scans at all. Both are
                properties of the RULE rather than of the workspaces it
                materialises, so they stay beside the scan target above and
                outside the template below. */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div className="min-w-0">
                <label htmlFor="ad-on-directory-delete" className="block text-sm text-slate-300 mb-1">
                  {t('form.onDirectoryDelete')}
                </label>
                <select
                  id="ad-on-directory-delete"
                  value={onDirectoryDelete}
                  onChange={e => setOnDirectoryDelete(e.target.value as 'flag' | 'destroy')}
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
                >
                  <option value="flag">{t('form.onDeleteFlag')}</option>
                  <option value="destroy">{t('form.onDeleteDestroy')}</option>
                </select>
                <p className="text-xs text-slate-500 mt-1">{t('form.onDirectoryDeleteHint')}</p>
                {onDirectoryDelete === 'destroy' && (
                  <p className="text-xs text-red-400 mt-2 font-medium">
                    {t('form.onDeleteDestroyWarning')}
                  </p>
                )}
              </div>
              {/* Top-aligned rather than bottom-aligned: the cell beside it grows a
                  hint and, on `destroy`, a warning, so `items-end` would walk this
                  down the row as the sibling changes height. */}
              <div className="min-w-0">
                <label className="flex min-h-11 items-center gap-2 text-sm text-slate-300">
                  <input
                    type="checkbox"
                    checked={enabled}
                    onChange={e => setEnabled(e.target.checked)}
                    className="h-4 w-4 rounded border-slate-600 bg-slate-700 text-brand-500 focus:ring-1 focus:ring-brand-500"
                  />
                  {t('form.enabled')}
                </label>
              </div>
            </div>

            {/* ── The workspace template, in the shared sections (#2413 F4) ──
                Was a `<details>` holding four hand-written sections whose
                titles were `<label>`s pointing at nothing. It is now the
                twelve `GROUPS` in the table's own order, each a real `<h4>`,
                and NOTHING COLLAPSES: the maintainer's decision is that
                sections carry navigability and collapse does not, so an
                operator sees what the form offers without an interaction
                first.

                The heading and the sentence are the `<summary>`'s own words
                and the one the three "applied to every workspace this rule
                creates" lines all said, kept once at the top of the block they
                are true of rather than repeated inside it. */}
            <section className="space-y-4 border-t border-slate-800 pt-4 min-w-0">
              <div>
                <h3 className="text-sm font-semibold text-slate-200">
                  {t('form.templateDefaults')}
                </h3>
                <p className="mt-1 text-xs text-slate-500">{t('form.governanceHint')}</p>
              </div>

              {GROUPS.map((g) => (
                <Group
                  key={g}
                  id={g}
                  ctx={ruleCtx}
                  title={tWs(`groups.${g}`)}
                  description={groupDescription(g)}
                >
                  {groupBody[g]}
                </Group>
              ))}

              {/* The three collections the field table has no group for, and
                  deliberately: each is a list of OTHER resources the rule
                  attaches to what it creates rather than a workspace setting
                  with a value, so none is in `FIELDS` and none is checked by
                  the parity gate. They keep the group idiom -- a real heading
                  and a section -- so the form reads as one list of sections. */}
              <section className="min-w-0">
                <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
                  {t('form.executionHooks')}
                </h4>
                <p className="text-xs text-slate-500 mb-2">
                  {t.rich('form.executionHooksHint', { page: (c) => <span className="text-slate-400">{c}</span> })}
                </p>
                {hooks.length === 0 ? (
                  <p className="text-xs text-slate-500 italic">
                    {t('form.noHooks')}
                  </p>
                ) : (
                  <div className="space-y-1.5 rounded-lg border border-slate-800 bg-slate-900/40 p-3 max-h-56 overflow-y-auto">
                    {hooks.map(h => {
                      const checked = executionHookTemplates.includes(h.id)
                      return (
                        <label key={h.id} className="flex items-center gap-2 text-sm cursor-pointer">
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={e => {
                              setExecutionHookTemplates(prev =>
                                e.target.checked
                                  ? [...prev, h.id]
                                  : prev.filter(id => id !== h.id),
                              )
                            }}
                            className="rounded border-slate-600 bg-slate-800 text-brand-500 focus:ring-brand-500"
                          />
                          <span className="text-slate-200">{h.attributes.name}</span>
                          <span className="font-mono text-xs text-slate-500">{h.attributes['hook-point']}</span>
                          {!h.attributes.enabled && (
                            <span className="text-xs text-amber-400/80">{t('form.hookDisabled')}</span>
                          )}
                        </label>
                      )
                    })}
                  </div>
                )}
                {/* Preserve any templated hook id no longer present in the library (e.g. a
                    since-deleted hook) so editing an existing rule doesn't silently drop it. */}
                {executionHookTemplates
                  .filter(id => !hooks.some(h => h.id === id))
                  .map(id => (
                    <label key={id} className="flex items-center gap-2 text-xs mt-1.5 text-slate-500">
                      <input
                        type="checkbox"
                        checked
                        onChange={() =>
                          setExecutionHookTemplates(prev => prev.filter(x => x !== id))
                        }
                        className="rounded border-slate-600 bg-slate-800 text-brand-500 focus:ring-brand-500"
                      />
                      <span className="font-mono">{id}</span>
                      <span className="text-amber-400/80">{t('form.hookNotInLibrary')}</span>
                    </label>
                  ))}
              </section>

              <section className="min-w-0">
                <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
                  {t('form.runTaskTemplates')}
                </h4>
                <RunTaskTemplatesEditor items={runTaskTemplates} onChange={setRunTaskTemplates} />
              </section>

              <section className="min-w-0">
                <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
                  {t('form.notificationTemplates')}
                </h4>
                <NotificationTemplatesEditor
                  items={notificationTemplates}
                  onChange={setNotificationTemplates}
                />
              </section>
            </section>

            <div className="flex gap-2 pt-2">
              <button
                type="submit"
                // A rule that declares neither operation would materialise
                // inert workspaces on every scan (#1966), so saving it is
                // blocked and the banner beside the controls says why.
                disabled={submitting || ruleHasNoOperations}
                className="px-4 py-2 rounded-lg bg-brand-600 hover:bg-brand-500 text-white text-sm font-medium disabled:opacity-50"
              >
                {submitting ? t('form.saving') : (editingId ? t('actions.update') : t('actions.create'))}
              </button>
              <button
                type="button"
                onClick={previewFormRule}
                disabled={submitting || ruleHasNoOperations}
                className="px-4 py-2 rounded-lg bg-slate-700 hover:bg-slate-600 text-slate-100 text-sm disabled:opacity-50"
                title={t('form.previewTitle')}
              >
                {t('actions.preview')}
              </button>
              <button
                type="button"
                onClick={() => { setShowForm(false); resetForm() }}
                className="px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm"
              >
                {t('actions.cancel')}
              </button>
            </div>
          </form>
        )}

        {sortedItems.length === 0 ? (
          <EmptyState message={t('empty')} />
        ) : (
          <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-start text-slate-400 border-b border-slate-800">
              <tr>
                <SortableHeader label={t('table.name')} sortKey="name" sortState={sortState} onSort={toggleSort} />
                <SortableHeader label={t('table.repo')} sortKey="repo" sortState={sortState} onSort={toggleSort} />
                <SortableHeader label={t('table.pattern')} sortKey="pattern" sortState={sortState} onSort={toggleSort} />
                <SortableHeader label={t('table.enabled')} sortKey="enabled" sortState={sortState} onSort={toggleSort} />
                <SortableHeader label={t('table.created')} sortKey="created" sortState={sortState} onSort={toggleSort} />
                <th className="py-2"></th>
              </tr>
            </thead>
            <tbody>
              {sortedItems.map(r => (
                <tr key={r.id} className="border-b border-slate-900 hover:bg-slate-900/40">
                  <td className="py-3 text-slate-200">{r.attributes.name}</td>
                  <td className="py-3 text-slate-400 font-mono text-xs">
                    {r.attributes['repo-url'].replace(/^https?:\/\//, '')}
                  </td>
                  <td className="py-3 text-slate-400 font-mono text-xs">{r.attributes.pattern}</td>
                  <td className="py-3">
                    {r.attributes.enabled ? (
                      <span className="text-green-400">{t('status.enabled')}</span>
                    ) : (
                      <span className="text-slate-500">{t('status.disabled')}</span>
                    )}
                  </td>
                  <td className="py-3 text-slate-400">
                    {fmt.date(r.attributes['created-at'])}
                  </td>
                  <td className="py-3 text-end">
                    <div className="flex justify-end gap-2">
                      <button
                        onClick={() => openPreview(r.id, r.attributes.name)}
                        className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-brand-100 transition-colors"
                        title={t('table.previewTitle')}
                      >
                        {t('actions.preview')}
                      </button>
                      <button
                        onClick={() => openEditForm(r)}
                        className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors"
                      >
                        {t('actions.edit')}
                      </button>
                      <button
                        onClick={() => setDeleteId(r.id)}
                        className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors"
                      >
                        {t('actions.delete')}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}

        {deleteId && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
            <div className="bg-slate-900 border border-slate-700 rounded-lg p-6 max-w-md w-full">
              <h3 className="text-lg font-semibold text-slate-100 mb-2">{t('deleteModal.title')}</h3>
              <p className="text-sm text-slate-400 mb-4">
                {t('deleteModal.body')}
              </p>
              <div className="flex gap-2 justify-end">
                <button
                  onClick={() => setDeleteId(null)}
                  className="px-4 py-2 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm"
                >
                  {t('actions.cancel')}
                </button>
                <button
                  onClick={() => handleDelete(deleteId)}
                  className="px-4 py-2 rounded bg-red-600 hover:bg-red-500 text-white text-sm"
                >
                  {t('actions.delete')}
                </button>
              </div>
            </div>
          </div>
        )}

        {previewModal && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
            <div className="bg-slate-900 border border-slate-700 rounded-lg p-6 max-w-3xl w-full max-h-[90vh] flex flex-col">
              <h3 className="text-lg font-semibold text-slate-100 mb-1">
                {t('preview.title', { ruleName: previewModal.ruleName })}
              </h3>
              <p className="text-xs text-slate-500 mb-4">
                {previewModal.ref ? (
                  t.rich('preview.walkedFiles', {
                    count: previewModal.filesWalked ?? 0,
                    ref: previewModal.ref,
                    files: (c) => <span className="text-slate-300">{c}</span>,
                    refspan: (c) => <span className="text-slate-300 font-mono">{c}</span>,
                  })
                ) : (
                  t('preview.walking')
                )}
              </p>

              {previewModal.loading && <LoadingSpinner />}

              {previewModal.error && (
                <div className="text-sm text-red-300 bg-red-900/20 border border-red-800/50 rounded p-3 mb-4">
                  {previewModal.error}
                </div>
              )}

              {previewModal.entries && previewModal.entries.length === 0 && (
                <div className="text-sm text-slate-400 py-4">
                  {t('preview.noMatches')}
                </div>
              )}

              {previewModal.entries && previewModal.entries.length > 0 && (
                <div className="overflow-auto flex-1 mb-4 border border-slate-700/50 rounded">
                  <table className="w-full text-sm">
                    <thead className="bg-slate-800/50 sticky top-0">
                      <tr className="border-b border-slate-700/50">
                        <th className="text-start px-3 py-2 text-xs font-medium text-slate-400 uppercase">{t('preview.colWorkspace')}</th>
                        <th className="text-start px-3 py-2 text-xs font-medium text-slate-400 uppercase">{t('preview.colDirectory')}</th>
                        <th className="text-start px-3 py-2 text-xs font-medium text-slate-400 uppercase">{t('preview.colAction')}</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-700/30">
                      {previewModal.entries.map(e => (
                        <tr key={e.working_directory}>
                          <td className="px-3 py-2 font-mono text-slate-200">{e.workspace_name}</td>
                          <td className="px-3 py-2 font-mono text-slate-400">{e.working_directory || t('preview.repoRoot')}</td>
                          <td className="px-3 py-2">
                            {!e.collision ? (
                              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-green-900/40 text-green-300">
                                {t('preview.actionCreate')}
                              </span>
                            ) : e.existing_autodiscovered ? (
                              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-700/40 text-slate-300" title={t('preview.skipDiscoveredTitle')}>
                                {t('preview.skipDiscovered')}
                              </span>
                            ) : (
                              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900/40 text-amber-300" title={t('preview.skipCollisionTitle')}>
                                {t('preview.skipCollision')}
                              </span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {previewModal.entries && previewModal.entries.length > 0 && (
                <p className="text-xs text-slate-500 mb-4">
                  {t('preview.willCreate', { count: previewModal.entries.filter(e => !e.collision).length })}
                </p>
              )}

              <div className="flex gap-2 justify-end">
                <button
                  onClick={() => setPreviewModal(null)}
                  disabled={previewModal.scanning}
                  className="px-4 py-2 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm disabled:opacity-50"
                >
                  {t('actions.close')}
                </button>
                {previewModal.id && previewModal.entries && previewModal.entries.filter(e => !e.collision).length > 0 && (
                  <button
                    onClick={handleProvision}
                    disabled={previewModal.scanning}
                    className="px-4 py-2 rounded bg-brand-600 hover:bg-brand-500 text-white text-sm disabled:opacity-50"
                  >
                    {previewModal.scanning
                      ? t('preview.provisioning')
                      : t('preview.provision', { count: previewModal.entries.filter(e => !e.collision).length })}
                  </button>
                )}
                {!previewModal.id && previewModal.entries && previewModal.entries.length > 0 && (
                  <span className="self-center text-xs text-slate-500 italic">
                    {t('preview.saveToProvision')}
                  </span>
                )}
              </div>
            </div>
          </div>
        )}
      </main>
    </>
  )
}
