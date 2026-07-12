'use client'

import { useEffect, useState, useCallback, Suspense } from 'react'
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
import { HealthConditions } from '@/components/health-conditions'
import { PlanSummaryBadges } from '@/components/plan-summary-badges'
import { WorkspacePicker } from '@/components/workspace-picker'
import { SensitiveValueInput } from '@/components/sensitive-value-input'
import { MobileCardList, MobileCard } from '@/components/mobile-card-list'
import { StateGraphTab } from '@/components/state-graph-tab'
import { useIsTouch } from '@/lib/use-media-query'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { useSortable } from '@/lib/use-sortable'
import { useRunEvents } from '@/lib/use-run-events'

interface WorkspacePermissions {
  'can-update': boolean
  'can-destroy': boolean
  'can-queue-run': boolean
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
  'execution-mode': string
  'execution-backend': string
  'auto-apply': boolean
  'terraform-version': string
  'terragrunt-enabled': boolean
  'terragrunt-version': string
  'working-directory': string
  locked: boolean
  'resource-cpu': string
  'resource-memory': string
  'agent-pool-id': string | null
  'agent-pool-name': string | null
  labels: Record<string, string>
  'owner-email': string
  'var-files': string[]
  'trigger-prefixes': string[]
  'drift-ignore-rules': string[]
  'vcs-repo-url': string
  'vcs-branch': string
  'vcs-connection-id': string | null
  'vcs-connection-name': string | null
  'vcs-workflow': 'merge_then_apply' | 'apply_then_merge'
  'auto-merge': boolean
  'auto-merge-strategy': 'merge' | 'squash' | 'rebase'
  'ai-summary-mode': 'default' | 'enabled' | 'disabled'
  'ai-summary-context': string
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
    hcl: boolean
    sensitive: boolean
    description: string
  }
}

interface RunItem {
  id: string
  attributes: {
    status: string
    source: string
    message: string
    'plan-only': boolean
    'is-destroy': boolean
    'created-at': string
    'created-by': string
    'plan-started-at': string | null
    'apply-finished-at': string | null
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

type Tab = 'overview' | 'variables' | 'runs' | 'state' | 'state-graph' | 'configurations' | 'notifications' | 'run-tasks' | 'run-triggers' | 'sharing'

const VALID_TABS: Set<string> = new Set(['overview', 'variables', 'runs', 'state', 'state-graph', 'configurations', 'notifications', 'run-tasks', 'run-triggers', 'sharing'])

export default function WorkspaceDetailPage() {
  return (
    <Suspense fallback={<><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>}>
      <WorkspaceDetailContent />
    </Suspense>
  )
}

function WorkspaceDetailContent() {
  const t = useTranslations('workspaceDetail')
  const router = useRouter()
  const params = useParams()
  const searchParams = useSearchParams()
  const workspaceId = params.id as string

  const tabParam = searchParams.get('tab') || 'overview'
  const activeTab: Tab = VALID_TABS.has(tabParam) ? (tabParam as Tab) : 'overview'

  function setActiveTab(tab: Tab) {
    router.replace(`?tab=${tab}`, { scroll: false })
  }

  const [workspace, setWorkspace] = useState<Workspace | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [lastQueuedRunId, setLastQueuedRunId] = useState<string | null>(null)

  // Overview editing
  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState('')
  const [nameChanged, setNameChanged] = useState(false)
  const [editCpu, setEditCpu] = useState('')
  const [editMemory, setEditMemory] = useState('')
  const [editAutoApply, setEditAutoApply] = useState(false)
  const [editExecMode, setEditExecMode] = useState('')
  const [editBackend, setEditBackend] = useState('')
  const [editVersion, setEditVersion] = useState('')
  const [editTerragruntEnabled, setEditTerragruntEnabled] = useState(false)
  const [editTerragruntVersion, setEditTerragruntVersion] = useState('')
  const [editPoolId, setEditPoolId] = useState<string | null>(null)
  const [editLabels, setEditLabels] = useState<Record<string, string>>({})
  const [editOwner, setEditOwner] = useState('')
  const [editVarFiles, setEditVarFiles] = useState<string[]>([])
  const [newVarFile, setNewVarFile] = useState('')
  const [editTriggerPrefixes, setEditTriggerPrefixes] = useState<string[]>([])
  const [newTriggerPrefix, setNewTriggerPrefix] = useState('')
  const [editDriftIgnoreRules, setEditDriftIgnoreRules] = useState<string[]>([])
  const [newDriftIgnoreRule, setNewDriftIgnoreRule] = useState('')
  const [editWorkingDir, setEditWorkingDir] = useState('')
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

  // Agent pools
  const [agentPools, setAgentPools] = useState<AgentPool[]>([])
  const [poolsLoaded, setPoolsLoaded] = useState(false)

  // VCS connections
  const [vcsConnections, setVcsConnections] = useState<{ id: string; attributes: { name: string; provider: string } }[]>([])
  const [vcsConnectionsLoaded, setVcsConnectionsLoaded] = useState(false)

  // Version suggestions
  const [versionSuggestions, setVersionSuggestions] = useState<string[]>([])
  const [versionsBackend, setVersionsBackend] = useState('')

  // Variables
  const [variables, setVariables] = useState<Variable[]>([])
  const [varsLoading, setVarsLoading] = useState(false)
  const [showAddVar, setShowAddVar] = useState(false)
  const [varKey, setVarKey] = useState('')
  const [varValue, setVarValue] = useState('')
  const [varCategory, setVarCategory] = useState('terraform')
  const [varSensitive, setVarSensitive] = useState(false)
  const [varHcl, setVarHcl] = useState(false)
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
  const [planOnly, setPlanOnly] = useState(true)
  const [queueingDestroy, setQueueingDestroy] = useState(false)
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
  const [editVarCategory, setEditVarCategory] = useState('terraform')
  const [editVarSensitive, setEditVarSensitive] = useState(false)
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
  const [aiSummaryContextDraft, setAiSummaryContextDraft] = useState<string | null>(null)

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
        case 'type': return item.attributes['is-destroy'] ? 'destroy' : item.attributes['plan-only'] ? 'plan only' : 'plan + apply'
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
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}`)
      if (!res.ok) throw new Error(t('errors.loadWorkspace'))
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
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/runs`)
      if (!res.ok) throw new Error(t('errors.loadRuns'))
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
    if (activeTab === 'configurations') loadConfigurations()
    if (activeTab === 'notifications') loadNotifications()
    if (activeTab === 'run-tasks') loadRunTasks()
    if (activeTab === 'run-triggers') loadRunTriggers()
  }, [activeTab, workspace, loadRuns])

  // Load VCS refs when plan options panel opens on a VCS-connected workspace
  useEffect(() => {
    if (showPlanOptions && workspace?.attributes['vcs-repo-url']) {
      loadVcsRefs()
    }
  }, [showPlanOptions, workspace, loadVcsRefs])

  // Force plan-only when a non-default VCS ref is selected
  useEffect(() => {
    if (vcsRef) {
      setPlanOnly(true)
    }
  }, [vcsRef])

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
  }, [activeTab, loadRuns, loadWorkspace]))

  async function loadVariables() {
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/vars`)
      if (!res.ok) throw new Error(t('errors.loadVariables'))
      const data = await res.json()
      setVariables(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadVariables'))
    } finally {
      setVarsLoading(false)
    }
  }

  async function loadStateVersions() {
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/state-versions`)
      if (!res.ok) throw new Error(t('errors.loadStateVersions'))
      const data = await res.json()
      setStateVersions(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadStateVersions'))
    } finally {
      setStateLoading(false)
    }
  }

  async function downloadStateVersion(sv: StateVersionItem) {
    try {
      const resp = await apiFetch(`/api/v2/state-versions/${sv.id}/download`)
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
        `/api/v2/workspaces/${workspaceId}/configuration-versions?page%5Bsize%5D=100`,
      )
      if (!res.ok) throw new Error(t('errors.loadConfigurations'))
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
      const res = await apiFetch(`/api/terrapod/v1/workspaces/${workspaceId}/notification-configurations`)
      if (!res.ok) throw new Error(t('errors.loadNotifications'))
      const data = await res.json()
      setNotifications(data.data || [])
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
      const [outRes, inRes] = await Promise.all([
        apiFetch(`${base}?filter[remote-state-consumer][type]=outbound`),
        apiFetch(`${base}?filter[remote-state-consumer][type]=inbound`),
      ])
      if (!outRes.ok) throw new Error(t('errors.loadOutboundConsumers'))
      if (!inRes.ok) throw new Error(t('errors.loadInboundConsumers'))
      const outData = await outRes.json()
      const inData = await inRes.json()
      setRscOutbound((outData.data || []).map(_rscFromRow))
      setRscInbound((inData.data || []).map(_rscFromRow))
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
      const [inRes, outRes] = await Promise.all([
        apiFetch(`${base}?filter[run-trigger][type]=inbound`),
        apiFetch(`${base}?filter[run-trigger][type]=outbound`),
      ])
      if (!inRes.ok) throw new Error(t('errors.loadInboundTriggers'))
      if (!outRes.ok) throw new Error(t('errors.loadOutboundTriggers'))
      const inData = await inRes.json()
      const outData = await outRes.json()
      setTrgInbound((inData.data || []).map(_trgFromRow))
      setTrgOutbound((outData.data || []).map(_trgFromRow))
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
      const res = await apiFetch(`/api/terrapod/v1/workspaces/${workspaceId}/run-tasks`)
      if (!res.ok) throw new Error(t('errors.loadRunTasks'))
      const data = await res.json()
      setRunTasks(data.data || [])
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
      if (!res.ok) throw new Error(t('errors.update'))
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
      if (!res.ok) throw new Error(t('errors.delete'))
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
    setEditMemory(workspace.attributes['resource-memory'])
    setEditAutoApply(workspace.attributes['auto-apply'])
    setEditExecMode(workspace.attributes['execution-mode'])
    setEditBackend(workspace.attributes['execution-backend'] || 'tofu')
    setEditVersion(workspace.attributes['terraform-version'] || '')
    setEditTerragruntEnabled(workspace.attributes['terragrunt-enabled'] ?? false)
    setEditTerragruntVersion(workspace.attributes['terragrunt-version'] || '')
    setEditPoolId(workspace.attributes['agent-pool-id'])
    setEditLabels(workspace.attributes.labels || {})
    setEditOwner(workspace.attributes['owner-email'] || '')
    setEditVarFiles(workspace.attributes['var-files'] || [])
    setNewVarFile('')
    setEditTriggerPrefixes(workspace.attributes['trigger-prefixes'] || [])
    setNewTriggerPrefix('')
    setEditDriftIgnoreRules(workspace.attributes['drift-ignore-rules'] || [])
    setNewDriftIgnoreRule('')
    setEditWorkingDir(workspace.attributes['working-directory'] || '')
    setEditVcsConnectionId(workspace.attributes['vcs-connection-id'] || null)
    setEditVcsRepoUrl(workspace.attributes['vcs-repo-url'] || '')
    setEditVcsBranch(workspace.attributes['vcs-branch'] || '')
    setEditVcsWorkflow(workspace.attributes['vcs-workflow'] || 'merge_then_apply')
    setEditAutoMerge(workspace.attributes['auto-merge'] || false)
    setEditAutoMergeStrategy(workspace.attributes['auto-merge-strategy'] || 'merge')
    setEditing(true)
    if (!poolsLoaded) {
      apiFetch('/api/terrapod/v1/agent-pools').then(res => res.ok ? res.json() : { data: [] }).then(data => {
        const allPools: AgentPool[] = data.data || []
        setAgentPools(allPools.filter(p => p.attributes.permission === 'write' || p.attributes.permission === 'admin'))
        setPoolsLoaded(true)
      }).catch(() => {})
    }
    if (!vcsConnectionsLoaded) {
      apiFetch('/api/terrapod/v1/vcs-connections').then(res => res.ok ? res.json() : { data: [] }).then(data => {
        setVcsConnections(data.data || [])
        setVcsConnectionsLoaded(true)
      }).catch(() => {})
    }
    const backend = workspace.attributes['execution-backend'] || 'tofu'
    if (versionsBackend !== backend) {
      apiFetch(`/api/terrapod/v1/binary-cache/versions?tool=${backend}`)
        .then(res => res.ok ? res.json() : { data: [] })
        .then(data => {
          setVersionSuggestions(data.data || [])
          setVersionsBackend(backend)
        })
        .catch(() => {})
    }
  }

  async function handleSave(force = false) {
    setSaving(true)
    setError('')
    setLockoutWarning('')
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'workspaces',
            attributes: {
              name: editName,
              'resource-cpu': editCpu,
              'resource-memory': editMemory,
              'auto-apply': editAutoApply,
              'execution-mode': editExecMode,
              'execution-backend': editBackend,
              'terraform-version': editVersion,
              'terragrunt-enabled': editTerragruntEnabled,
              'terragrunt-version': editTerragruntVersion || '1.0',
              'agent-pool-id': editPoolId,
              'working-directory': editWorkingDir,
              'var-files': editVarFiles,
              'trigger-prefixes': editTriggerPrefixes,
              'drift-ignore-rules': editDriftIgnoreRules,
              'vcs-repo-url': editVcsRepoUrl,
              'vcs-branch': editVcsBranch,
              'vcs-workflow': editVcsWorkflow,
              'auto-merge': editAutoMerge,
              'auto-merge-strategy': editAutoMergeStrategy,
              labels: editLabels,
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
      if (!res.ok) throw new Error(t('errors.updateWorkspace'))
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
      // lock/unlock are TFE V2 CLI-contract endpoints — only at /api/v2/.
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/actions/${action}`, {
        method: 'POST',
      })
      if (!res.ok) throw new Error(action === 'unlock' ? t('errors.unlockWorkspace') : t('errors.lockWorkspace'))
      await loadWorkspace()
    } catch (err) {
      setError(err instanceof Error ? err.message : action === 'unlock' ? t('errors.unlockWorkspace') : t('errors.lockWorkspace'))
    }
  }

  // AI plan summary (#401)
  async function handleAiSummaryAttrUpdate(patch: { 'ai-summary-mode'?: string; 'ai-summary-context'?: string }) {
    if (!workspace) return
    setSavingAiSummary(true)
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'workspaces', attributes: patch } }),
      })
      if (!res.ok) {
        const body = await res.text()
        throw new Error(body || t('errors.updateAiSummary'))
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

  // Slack run notifications (#556)
  async function handleSlackChannelUpdate(channel: string) {
    if (!workspace) return
    setSavingSlackChannel(true)
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'workspaces', attributes: { 'slack-channel': channel } },
        }),
      })
      if (!res.ok) {
        const body = await res.text()
        throw new Error(body || t('errors.updateSlackChannel'))
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
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'workspaces', attributes: { 'drift-detection-enabled': newEnabled } },
        }),
      })
      if (!res.ok) throw new Error(t('errors.updateDriftSettings'))
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
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'workspaces', attributes: { 'drift-detection-interval-seconds': seconds } },
        }),
      })
      if (!res.ok) throw new Error(t('errors.updateDriftInterval'))
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
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'workspaces',
            attributes: { 'plan-expiry-seconds': seconds > 0 ? seconds : null },
          },
        }),
      })
      if (!res.ok) throw new Error(t('errors.updatePlanExpiry'))
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
      const res = await apiFetch(`/api/v2/runs`, {
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
      if (!res.ok) throw new Error(t('errors.deleteWorkspace'))
      router.push('/workspaces')
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.deleteWorkspace'))
      setDeleting(false)
    }
  }

  async function handleAddVariable(e: React.FormEvent) {
    e.preventDefault()
    setAddingVar(true)
    setError('')
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/vars`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'vars',
            attributes: {
              key: varKey,
              value: varValue,
              category: varCategory,
              sensitive: varSensitive,
              hcl: varHcl,
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
      setVarCategory('terraform')
      setVarSensitive(false)
      setVarHcl(false)
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
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/vars/${varId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(t('errors.deleteVariable'))
      await loadVariables()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.deleteVariable'))
    }
  }

  async function handleQueuePlan() {
    // Touch fat-finger guard — queuing a run is a state change (and on a
    // non-VCS agent workspace a `plan + apply` run can change infrastructure),
    // so a stray tap on a touch device (any width) gets a native confirm; a
    // precise pointer runs immediately.
    if (isTouch) {
      const msg = planOnly
        ? t('runs.queuePlanConfirm')
        : t('runs.queueApplyConfirm')
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
      if (planAllowEmpty) attrs['allow-empty-apply'] = true
      if (vcsRef) attrs['vcs-ref'] = vcsRef

      const res = await apiFetch(`/api/v2/runs`, {
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
        throw new Error(data.detail || t('errors.queuePlanStatus', { status: res.status }))
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
      setPlanOnly(true)
      setVcsRef('')
      await loadRuns()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.queuePlan'))
    } finally {
      setQueueingPlan(false)
    }
  }

  async function handleQueueDestroy() {
    setQueueingDestroy(true)
    setError('')
    try {
      const res = await apiFetch(`/api/v2/runs`, {
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
    setEditVarValue(v.attributes.sensitive ? '' : v.attributes.value)
    setEditVarCategory(v.attributes.category)
    setEditVarSensitive(v.attributes.sensitive)
    setEditVarHcl(v.attributes.hcl)
  }

  async function handleSaveVar() {
    if (!editingVarId) return
    setSavingVar(true)
    setError('')
    try {
      const attrs: Record<string, unknown> = {
        key: editVarKey,
        category: editVarCategory,
        sensitive: editVarSensitive,
        hcl: editVarHcl,
      }
      if (editVarValue !== '') {
        attrs.value = editVarValue
      }
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/vars/${editingVarId}`, {
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
      if (!res.ok) throw new Error(t('errors.update'))
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
      if (!res.ok) throw new Error(t('errors.delete'))
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
      if (!res.ok) throw new Error(t('errors.verificationFailed'))
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

  const tabs: { key: Tab; label: string }[] = [
    { key: 'overview', label: t('tabs.overview') },
    { key: 'variables', label: t('tabs.variables') },
    { key: 'runs', label: t('tabs.runs') },
    { key: 'state', label: t('tabs.state') },
    { key: 'state-graph', label: t('tabs.stateGraph') },
    { key: 'configurations', label: t('tabs.configurations') },
    { key: 'notifications', label: t('tabs.notifications') },
    { key: 'run-tasks', label: t('tabs.runTasks') },
    { key: 'run-triggers', label: t('tabs.runTriggers') },
    { key: 'sharing', label: t('tabs.sharing') },
  ]

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

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={attrs.name}
          description={t('header.executionMode', { mode: attrs['execution-mode'] })}
          actions={<ConnectionStatus connected={sseConnected} />}
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
            value={activeTab}
            onChange={(e) => setActiveTab(e.target.value as Tab)}
            className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2.5 text-sm font-medium text-slate-100 focus:border-brand-500 focus:outline-none"
          >
            {tabs.map((tab) => (
              <option key={tab.key} value={tab.key}>
                {tab.label}
              </option>
            ))}
          </select>
        </div>
        <div className="hidden border-b border-slate-700/50 mb-6 md:block">
          <div className="flex gap-1 -mb-px">
            {tabs.map((tab) => (
              <button
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
                className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                  activeTab === tab.key
                    ? 'border-brand-500 text-brand-400'
                    : 'border-transparent text-slate-400 hover:text-slate-200 hover:border-slate-600'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>

        {/* Overview Tab */}
        {activeTab === 'overview' && (
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
                      disabled={saving}
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
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.name')}</dt>
                  {editing ? (
                    <input type="text" value={editName} onChange={(e) => setEditName(e.target.value)}
                      pattern="[a-zA-Z0-9][a-zA-Z0-9_\-]*" maxLength={90}
                      title={t('fields.nameTitle')}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs.name}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.executionMode')}</dt>
                  {editing ? (
                    <select value={editExecMode} onChange={(e) => setEditExecMode(e.target.value)} className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500">
                      <option value="local">{t('fields.execModeLocal')}</option>
                      <option value="agent">{t('fields.execModeAgent')}</option>
                    </select>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['execution-mode']}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.autoApply')}</dt>
                  {editing ? (
                    <label className="flex items-center gap-2 mt-1">
                      <input type="checkbox" checked={editAutoApply} onChange={(e) => setEditAutoApply(e.target.checked)} className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                      <span className="text-sm text-slate-200">{editAutoApply ? t('common.enabled') : t('common.disabled')}</span>
                    </label>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['auto-apply'] ? t('common.enabled') : t('common.disabled')}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.cpuRequest')}</dt>
                  {editing ? (
                    <input type="text" value={editCpu} onChange={(e) => setEditCpu(e.target.value)}
                      pattern="[0-9]+m|[0-9]+(\.[0-9]+)?"
                      title={t('fields.cpuRequestTitle')}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['resource-cpu']}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.memoryRequest')}</dt>
                  {editing ? (
                    <input type="text" value={editMemory} onChange={(e) => setEditMemory(e.target.value)}
                      pattern="[0-9]+(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E|m)?"
                      title={t('fields.memoryRequestTitle')}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['resource-memory']}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.executionBackend')}</dt>
                  {editing ? (
                    <select value={editBackend} onChange={(e) => setEditBackend(e.target.value)} className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500">
                      <option value="tofu">OpenTofu</option>
                      <option value="terraform">Terraform</option>
                    </select>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['execution-backend'] === 'terraform' ? 'Terraform' : 'OpenTofu'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.version')}</dt>
                  {editing ? (
                    <>
                      <input type="text" list="edit-version-suggestions" value={editVersion} onChange={(e) => setEditVersion(e.target.value)} placeholder={t('fields.versionPlaceholder')}
                        pattern="[0-9]+\.[0-9]+(\.[0-9]+)?"
                        title={t('fields.versionTitle')}
                        className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                      <datalist id="edit-version-suggestions">
                        {versionSuggestions.map(v => (
                          <option key={v} value={v} />
                        ))}
                      </datalist>
                    </>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['terraform-version'] || t('common.default')}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Terragrunt</dt>
                  {editing ? (
                    <div className="mt-1 space-y-2">
                      <label className="flex items-center gap-2">
                        <input type="checkbox" checked={editTerragruntEnabled} onChange={(e) => setEditTerragruntEnabled(e.target.checked)} className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                        <span className="text-sm text-slate-200">{editTerragruntEnabled ? t('fields.terragruntEnabledAgent') : t('common.disabled')}</span>
                      </label>
                      {editTerragruntEnabled && (
                        <input type="text" value={editTerragruntVersion} onChange={(e) => setEditTerragruntVersion(e.target.value)} placeholder={t('fields.terragruntVersionPlaceholder')}
                          pattern="[0-9]+\.[0-9]+(\.[0-9]+)?"
                          title={t('fields.terragruntVersionTitle')}
                          className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                      )}
                    </div>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['terragrunt-enabled'] ? t('fields.terragruntEnabledVersion', { version: attrs['terragrunt-version'] || '1.0' }) : t('common.disabled')}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.workingDirectory')}</dt>
                  {editing ? (
                    <input type="text" value={editWorkingDir} onChange={(e) => setEditWorkingDir(e.target.value)} placeholder={t('fields.workingDirectoryPlaceholder')}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['working-directory'] || '/'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.agentPool')}</dt>
                  {editing ? (
                    <select
                      value={editPoolId || ''}
                      onChange={(e) => setEditPoolId(e.target.value || null)}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                    >
                      <option value="">{t('common.none')}</option>
                      {agentPools.map((p) => (
                        <option key={p.id} value={p.id}>{p.attributes.name}</option>
                      ))}
                    </select>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">
                      {attrs['agent-pool-name'] || (attrs['agent-pool-id'] ? attrs['agent-pool-id'] : t('common.none'))}
                    </dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.owner')}</dt>
                  {editing && isAdmin() ? (
                    <input type="email" value={editOwner} onChange={(e) => setEditOwner(e.target.value)} placeholder="user@example.com" className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['owner-email'] || t('common.none')}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.vcsConnection')}</dt>
                  {editing ? (
                    <select
                      value={editVcsConnectionId || ''}
                      onChange={(e) => setEditVcsConnectionId(e.target.value || null)}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                    >
                      <option value="">{t('common.none')}</option>
                      {vcsConnections.map((c) => (
                        <option key={c.id} value={c.id}>{c.attributes.name} ({c.attributes.provider})</option>
                      ))}
                    </select>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['vcs-connection-name'] || t('common.none')}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.vcsRepository')}</dt>
                  {editing ? (
                    <input type="text" value={editVcsRepoUrl} onChange={(e) => setEditVcsRepoUrl(e.target.value)} placeholder="https://github.com/org/repo"
                      pattern="https?://.+"
                      title={t('fields.vcsRepositoryTitle')}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">
                      {attrs['vcs-repo-url'] ? (
                        <a href={attrs['vcs-repo-url']} target="_blank" rel="noopener noreferrer" className="text-brand-400 hover:text-brand-300">{attrs['vcs-repo-url']}</a>
                      ) : t('common.none')}
                    </dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.vcsBranch')}</dt>
                  {editing ? (
                    <input type="text" value={editVcsBranch} onChange={(e) => setEditVcsBranch(e.target.value)} placeholder={t('fields.vcsBranchPlaceholder')} className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['vcs-branch'] || t('common.default')}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.vcsWorkflow')}</dt>
                  {editing ? (
                    <select
                      value={editVcsWorkflow}
                      onChange={(e) => setEditVcsWorkflow(e.target.value as 'merge_then_apply' | 'apply_then_merge')}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                    >
                      <option value="merge_then_apply">{t('fields.vcsWorkflowMergeThenApply')}</option>
                      <option value="apply_then_merge">{t('fields.vcsWorkflowApplyThenMerge')}</option>
                    </select>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['vcs-workflow']}</dd>
                  )}
                </div>
                {editing && editVcsWorkflow === 'apply_then_merge' && (
                  <div className="sm:col-span-2 rounded border border-amber-700 bg-amber-900/30 p-3 text-xs text-amber-100">
                    <p className="font-medium">{t('vcsWorkflowWarning.title')}</p>
                    <p className="mt-1">
                      {t.rich('vcsWorkflowWarning.rbac', { em: (chunks) => <em> {chunks}</em> })}
                    </p>
                    <p className="mt-1">
                      {t.rich('vcsWorkflowWarning.recommended', { strong: (chunks) => <strong>{chunks}</strong> })}
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
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.autoMerge')}</dt>
                  {editing ? (
                    <label className="mt-1 flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={editAutoMerge}
                        onChange={(e) => setEditAutoMerge(e.target.checked)}
                        className="rounded border-slate-600 bg-slate-700 text-brand-600"
                      />
                      <span className="text-sm text-slate-200">{editAutoMerge ? t('common.enabled') : t('common.disabled')}</span>
                    </label>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['auto-merge'] ? t('common.enabled') : t('common.disabled')}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{t('fields.autoMergeStrategy')}</dt>
                  {editing ? (
                    <select
                      value={editAutoMergeStrategy}
                      onChange={(e) => setEditAutoMergeStrategy(e.target.value as 'merge' | 'squash' | 'rebase')}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                    >
                      <option value="merge">merge</option>
                      <option value="squash">squash</option>
                      <option value="rebase">rebase</option>
                    </select>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['auto-merge-strategy']}</dd>
                  )}
                </div>
                {attrs['vcs-connection-name'] && !editing && (
                  <div>
                    <dt className="text-xs text-slate-500">{t('fields.vcsPolling')}</dt>
                    <dd className="mt-1 text-sm">
                      {attrs['vcs-last-error'] ? (
                        <span className="text-red-400" title={attrs['vcs-last-error']}>{t('vcsPolling.error')}{attrs['vcs-last-error-at'] ? ` (${new Date(attrs['vcs-last-error-at']).toLocaleString()})` : ''}</span>
                      ) : attrs['vcs-last-polled-at'] ? (
                        <span className="text-green-400">{t('vcsPolling.ok', { time: new Date(attrs['vcs-last-polled-at']).toLocaleString() })}</span>
                      ) : (
                        <span className="text-slate-400">{t('vcsPolling.notPolled')}</span>
                      )}
                    </dd>
                  </div>
                )}
                <div className="sm:col-span-2">
                  <dt
                    className="text-xs text-slate-500 mb-1"
                    title={t('fields.labelsTitle')}
                  >
                    {t('fields.labels')}
                  </dt>
                  {editing && perms['can-update'] ? (
                    <LabelsEditor labels={editLabels} onChange={setEditLabels} />
                  ) : (
                    <dd className="mt-1">
                      <LabelsEditor labels={attrs.labels || {}} readOnly />
                    </dd>
                  )}
                </div>
                <div className="sm:col-span-2">
                  <dt className="text-xs text-slate-500 mb-1">{t('fields.varFiles')}</dt>
                  {editing && perms['can-update'] ? (
                    <div className="space-y-2">
                      {editVarFiles.map((f, i) => (
                        <div key={f} className="flex items-center gap-2">
                          <code className="text-sm text-slate-200 bg-slate-700 px-2 py-0.5 rounded flex-1 truncate">{f}</code>
                          <button
                            onClick={() => setEditVarFiles(editVarFiles.filter((_, j) => j !== i))}
                            className="text-xs text-red-400 hover:text-red-300"
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
                          className="text-xs text-brand-400 hover:text-brand-300"
                        >{t('actions.add')}</button>
                      </div>
                    </div>
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
                <div className="sm:col-span-2">
                  <dt className="text-xs text-slate-500 mb-1">{t('fields.triggerPrefixes')}</dt>
                  {editing && perms['can-update'] ? (
                    <div className="space-y-2">
                      <p className="text-xs text-slate-400">{t('fields.triggerPrefixesHint')}</p>
                      {editTriggerPrefixes.map((f, i) => (
                        <div key={f} className="flex items-center gap-2">
                          <code className="text-sm text-slate-200 bg-slate-700 px-2 py-0.5 rounded flex-1 truncate">{f}</code>
                          <button
                            onClick={() => setEditTriggerPrefixes(editTriggerPrefixes.filter((_, j) => j !== i))}
                            className="text-xs text-red-400 hover:text-red-300"
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
                          className="text-xs text-brand-400 hover:text-brand-300"
                        >{t('actions.add')}</button>
                      </div>
                    </div>
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
                <div className="sm:col-span-2">
                  <dt className="text-xs text-slate-500 mb-1">{t('fields.driftIgnoreRules')}</dt>
                  {editing && perms['can-update'] ? (
                    <div className="space-y-2">
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
                            className="text-xs text-red-400 hover:text-red-300"
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
                          className="text-xs text-brand-400 hover:text-brand-300"
                        >{t('actions.add')}</button>
                      </div>
                    </div>
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
              </dl>
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
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-sm font-medium text-slate-300">{t('lock.title')}</h3>
                  <p className="text-sm text-slate-400 mt-1">
                    {attrs.locked ? t('lock.lockedDesc') : t('lock.unlockedDesc')}
                  </p>
                </div>
                {perms['can-lock'] && (
                  <button
                    onClick={handleLockToggle}
                    className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
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
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <dt className="text-xs text-slate-500">{t('drift.checkInterval')}</dt>
                  <dd className="mt-1">
                    <select
                      value={attrs['drift-detection-interval-seconds'] || 86400}
                      onChange={(e) => handleDriftIntervalChange(Number(e.target.value))}
                      disabled={savingDrift}
                      className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                    >
                      {DRIFT_INTERVALS.map((di) => (
                        <option key={di.value} value={di.value}>{di.label}</option>
                      ))}
                    </select>
                  </dd>
                </div>
                <div>
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
                <div>
                  <dt className="text-xs text-slate-500">{t('drift.lastChecked')}</dt>
                  <dd className="mt-1 text-sm text-slate-200">
                    {attrs['drift-last-checked-at'] ? new Date(attrs['drift-last-checked-at']).toLocaleString() : t('common.never')}
                  </dd>
                </div>
                {perms['can-queue-run'] && (
                  <div className="flex items-end">
                    <button
                      onClick={handleCheckDriftNow}
                      disabled={checkingDrift || attrs.locked || !attrs['drift-detection-enabled']}
                      className="px-3 py-1.5 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
                      title={!attrs['drift-detection-enabled'] ? t('drift.checkNowTitleDisabled') : attrs.locked ? t('common.workspaceLocked') : t('drift.checkNowTitle')}
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
                <h3 className="text-sm font-medium text-slate-300">{t('planExpiry.title')}</h3>
                <p className="text-xs text-slate-500 mt-1">
                  {t('planExpiry.description')}
                </p>
              </div>
              <dl>
                <div>
                  <dt className="text-xs text-slate-500">{t('planExpiry.expireAfter')}</dt>
                  <dd className="mt-1">
                    <select
                      value={attrs['plan-expiry-seconds'] || 0}
                      onChange={(e) => handlePlanExpiryChange(Number(e.target.value))}
                      disabled={savingPlanExpiry}
                      className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                    >
                      {PLAN_EXPIRY_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </select>
                  </dd>
                </div>
              </dl>
            </div>

            {/* AI Plan Summary (#401) */}
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
              <div className="flex items-center justify-between mb-4">
                <div>
                  <h3 className="text-sm font-medium text-slate-300">{t('aiSummary.title')}</h3>
                  <p className="text-xs text-slate-500 mt-1">
                    {t('aiSummary.description')}
                  </p>
                </div>
              </div>
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <dt className="text-xs text-slate-500">{t('aiSummary.mode')}</dt>
                  <dd className="mt-1">
                    {perms['can-update'] ? (
                      <select
                        value={attrs['ai-summary-mode'] || 'default'}
                        onChange={(e) => handleAiSummaryAttrUpdate({ 'ai-summary-mode': e.target.value })}
                        disabled={savingAiSummary}
                        className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                      >
                        <option value="default">{t('aiSummary.modeDefault')}</option>
                        <option value="enabled">{t('aiSummary.modeEnabled')}</option>
                        <option value="disabled">{t('aiSummary.modeDisabled')}</option>
                      </select>
                    ) : (
                      <span className="text-sm text-slate-200">
                        {attrs['ai-summary-mode'] === 'enabled'
                          ? t('aiSummary.modeEnabled')
                          : attrs['ai-summary-mode'] === 'disabled'
                            ? t('aiSummary.modeDisabled')
                            : t('aiSummary.modeDefault')}
                      </span>
                    )}
                  </dd>
                </div>
                <div className="sm:col-span-2">
                  <dt className="text-xs text-slate-500">
                    {t('aiSummary.contextLabel')}
                    <span className="ml-2 text-slate-600">{t('aiSummary.contextLabelSuffix')}</span>
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
                    <p className="text-xs text-slate-500 mt-1">
                      {t('aiSummary.contextHint')}
                      {savingAiSummary && <span className="ml-2 text-brand-400">{t('actions.saving')}</span>}
                    </p>
                  </dd>
                </div>
              </dl>
            </div>

            {/* Slack notifications (#556) */}
            <div className="bg-slate-800/50 rounded-lg border border-slate-700 p-6">
              <h3 className="text-sm font-medium text-slate-200">{t('slack.title')}</h3>
              <p className="text-sm text-slate-400 mt-1">
                {t('slack.description')}
              </p>
              <dl className="mt-4">
                <div>
                  <dt className="text-xs text-slate-500">{t('slack.channel')}</dt>
                  <dd className="mt-1">
                    {perms['can-update'] ? (
                      <input
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
                        className="w-full sm:w-96 px-3 py-2 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-1 focus:ring-brand-500 font-mono"
                      />
                    ) : attrs['slack-channel'] ? (
                      <p className="text-sm text-slate-200 font-mono">{attrs['slack-channel']}</p>
                    ) : (
                      <p className="text-sm text-slate-500 italic">{t('slack.notSet')}</p>
                    )}
                    <p className="text-xs text-slate-500 mt-1">
                      {t('slack.hint')}
                      {savingSlackChannel && <span className="ml-2 text-brand-400">{t('actions.saving')}</span>}
                    </p>
                  </dd>
                </div>
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
            {perms['can-update-variable'] && (
              <div className="flex justify-end mb-4">
                <button
                  onClick={() => setShowAddVar(!showAddVar)}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors"
                >
                  {showAddVar ? t('actions.cancel') : t('variables.addVariable')}
                </button>
              </div>
            )}

            {showAddVar && (
              <form onSubmit={handleAddVariable} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="var-key" className="block text-sm font-medium text-slate-300 mb-1">{t('variables.key')}</label>
                    <input id="var-key" type="text" value={varKey} onChange={(e) => setVarKey(e.target.value)} required placeholder="AWS_REGION" className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="var-val" className="block text-sm font-medium text-slate-300 mb-1">{t('variables.value')}</label>
                    <SensitiveValueInput id="var-val" value={varValue} onChange={setVarValue} sensitive={varSensitive} placeholder="us-east-1" rows={2} className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent resize-y" />
                  </div>
                  <div>
                    <label htmlFor="var-cat" className="block text-sm font-medium text-slate-300 mb-1">{t('variables.category')}</label>
                    <select id="var-cat" value={varCategory} onChange={(e) => setVarCategory(e.target.value)} className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      <option value="terraform">Terraform</option>
                      <option value="env">{t('variables.categoryEnv')}</option>
                    </select>
                  </div>
                  <div className="flex items-end gap-4">
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input type="checkbox" checked={varSensitive} onChange={(e) => setVarSensitive(e.target.checked)} className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                      <span className="text-sm text-slate-300">{t('variables.sensitive')}</span>
                    </label>
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input type="checkbox" checked={varHcl} onChange={(e) => setVarHcl(e.target.checked)} className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                      <span className="text-sm text-slate-300">HCL</span>
                    </label>
                  </div>
                </div>
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
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">{t('common.actions')}</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {sortedVars.map((v) =>
                      editingVarId === v.id ? (
                        <tr key={v.id} className="bg-slate-700/20">
                          <td className="px-4 py-3">
                            <input type="text" value={editVarKey} onChange={(e) => setEditVarKey(e.target.value)}
                              className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 font-mono focus:outline-none focus:ring-1 focus:ring-brand-500" />
                          </td>
                          <td className="px-4 py-3">
                            <SensitiveValueInput value={editVarValue} onChange={setEditVarValue}
                              sensitive={editVarSensitive}
                              placeholder={editVarSensitive ? t('variables.enterNewValue') : ''}
                              rows={2}
                              className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 font-mono focus:outline-none focus:ring-1 focus:ring-brand-500 resize-y" />
                          </td>
                          <td className="px-4 py-3 hidden sm:table-cell">
                            <div className="flex items-center gap-3">
                              <select value={editVarCategory} onChange={(e) => setEditVarCategory(e.target.value)}
                                className="px-2 py-1 text-xs border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500">
                                <option value="terraform">terraform</option>
                                <option value="env">env</option>
                              </select>
                              <label className="flex items-center gap-1 cursor-pointer">
                                <input type="checkbox" checked={editVarSensitive} onChange={(e) => setEditVarSensitive(e.target.checked)}
                                  className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                                <span className="text-xs text-slate-400">{t('variables.sensitive')}</span>
                              </label>
                              <label className="flex items-center gap-1 cursor-pointer">
                                <input type="checkbox" checked={editVarHcl} onChange={(e) => setEditVarHcl(e.target.checked)}
                                  className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                                <span className="text-xs text-slate-400">HCL</span>
                              </label>
                            </div>
                          </td>
                          <td className="px-4 py-3 text-right">
                            <div className="flex justify-end gap-2">
                              <button onClick={() => setEditingVarId(null)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200">{t('actions.cancel')}</button>
                              <button onClick={handleSaveVar} disabled={savingVar} className="px-2.5 py-1 rounded-md text-xs font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white">
                                {savingVar ? t('actions.savingEllipsis') : t('actions.save')}
                              </button>
                            </div>
                          </td>
                        </tr>
                      ) : (
                        <tr key={v.id} className="hover:bg-slate-700/20 transition-colors">
                          <td className="px-4 py-3 text-sm text-slate-200 font-mono">{v.attributes.key}</td>
                          <td className="px-4 py-3 text-sm text-slate-400 font-mono">
                            {v.attributes.sensitive ? '***' : (v.attributes.value || <span className="text-slate-600 italic">{t('variables.emptyValue')}</span>)}
                          </td>
                          <td className="px-4 py-3 text-xs text-slate-400 hidden sm:table-cell">
                            <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                              v.attributes.category === 'terraform' ? 'bg-purple-900/50 text-purple-300' : 'bg-cyan-900/50 text-cyan-300'
                            }`}>
                              {v.attributes.category}
                            </span>
                          </td>
                          {perms['can-update-variable'] && (
                            <td className="px-4 py-3 text-right">
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
                      <div className="space-y-2">
                        <input
                          type="text"
                          value={editVarKey}
                          onChange={(e) => setEditVarKey(e.target.value)}
                          placeholder={t('variables.key')}
                          className="w-full px-2 py-1.5 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 font-mono focus:outline-none focus:ring-1 focus:ring-brand-500"
                        />
                        <SensitiveValueInput
                          value={editVarValue}
                          onChange={setEditVarValue}
                          sensitive={editVarSensitive}
                          placeholder={editVarSensitive ? t('variables.enterNewValue') : ''}
                          rows={2}
                          className="w-full px-2 py-1.5 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 font-mono focus:outline-none focus:ring-1 focus:ring-brand-500 resize-y"
                        />
                        <div className="flex flex-wrap items-center gap-3">
                          <select
                            value={editVarCategory}
                            onChange={(e) => setEditVarCategory(e.target.value)}
                            className="px-2 py-1 text-xs border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                          >
                            <option value="terraform">terraform</option>
                            <option value="env">env</option>
                          </select>
                          <label className="flex items-center gap-1 cursor-pointer">
                            <input type="checkbox" checked={editVarSensitive} onChange={(e) => setEditVarSensitive(e.target.checked)} className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                            <span className="text-xs text-slate-400">{t('variables.sensitive')}</span>
                          </label>
                          <label className="flex items-center gap-1 cursor-pointer">
                            <input type="checkbox" checked={editVarHcl} onChange={(e) => setEditVarHcl(e.target.checked)} className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                            <span className="text-xs text-slate-400">HCL</span>
                          </label>
                        </div>
                        <div className="flex gap-2 pt-1">
                          <button onClick={() => setEditingVarId(null)} className="px-3 py-1.5 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 text-slate-200">{t('actions.cancel')}</button>
                          <button onClick={handleSaveVar} disabled={savingVar} className="px-3 py-1.5 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white">
                            {savingVar ? t('actions.saving') : t('actions.save')}
                          </button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <div className="flex items-start justify-between gap-2 mb-1.5">
                          <span className="text-sm font-mono font-medium text-slate-200 break-all">{v.attributes.key}</span>
                          <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                            v.attributes.category === 'terraform' ? 'bg-purple-900/50 text-purple-300' : 'bg-cyan-900/50 text-cyan-300'
                          }`}>
                            {v.attributes.category}
                          </span>
                        </div>
                        <div className="mb-2 text-sm text-slate-400 font-mono break-all">
                          {v.attributes.sensitive ? '***' : (v.attributes.value || <span className="text-slate-600 italic">{t('variables.emptyValue')}</span>)}
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
                  {!showDestroyConfirm ? (
                    <button
                      onClick={() => setShowDestroyConfirm(true)}
                      disabled={queueingDestroy || attrs.locked}
                      className="px-4 py-2 rounded-lg text-sm font-medium bg-red-600/20 hover:bg-red-600/40 text-red-400 transition-colors"
                      title={attrs.locked ? t('common.workspaceLocked') : t('runs.queueDestroyTitle')}
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
                  )}
                  <button
                    onClick={handleQueuePlan}
                    disabled={queueingPlan || attrs.locked}
                    className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
                    title={attrs.locked ? t('common.workspaceLocked') : undefined}
                  >
                    {queueingPlan ? t('actions.queuing') : planOnly ? t('runs.queuePlan') : t('runs.queueRun')}
                  </button>
                </div>
                {showPlanOptions && (
                  <div className="mt-3 p-4 bg-slate-800/50 rounded-lg border border-slate-700/50">
                    <h4 className="text-sm font-medium text-slate-300 mb-3">{t('runs.planOptions')}</h4>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <div>
                        <label className="block text-xs text-slate-400 mb-1">{t('runs.targetResources')} <span className="text-slate-500">{t('runs.commaSeparated')}</span></label>
                        <input
                          type="text"
                          value={planTargets}
                          onChange={e => setPlanTargets(e.target.value)}
                          placeholder="e.g. aws_instance.web, aws_s3_bucket.data"
                          className="w-full px-3 py-2 bg-slate-900 border border-slate-600 rounded-lg text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-brand-500 font-mono"
                        />
                      </div>
                      <div>
                        <label className="block text-xs text-slate-400 mb-1">{t('runs.replaceResources')} <span className="text-slate-500">{t('runs.commaSeparated')}</span></label>
                        <input
                          type="text"
                          value={planReplaces}
                          onChange={e => setPlanReplaces(e.target.value)}
                          placeholder="e.g. aws_instance.web"
                          className="w-full px-3 py-2 bg-slate-900 border border-slate-600 rounded-lg text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-brand-500 font-mono"
                        />
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-4 mt-3">
                      <label className={`flex items-center gap-2 text-sm cursor-pointer ${vcsRef ? 'text-slate-500' : 'text-slate-300'}`}>
                        <input
                          type="checkbox"
                          checked={planOnly}
                          onChange={e => setPlanOnly(e.target.checked)}
                          disabled={!!vcsRef}
                          className="rounded border-slate-600 bg-slate-900 text-brand-500 focus:ring-brand-500 disabled:opacity-50"
                        />
                        {t('runs.planOnly')}
                      </label>
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
                      {!vcsRef && (
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
                            {t('runs.nonDefaultRefNote')}
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
                      <th className="text-left px-4 py-2 text-xs font-medium text-slate-400 uppercase tracking-wider hidden md:table-cell">{t('runs.changes')}</th>
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
                            <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${statusColor(run.attributes.status)}`}>
                              {run.attributes.status}
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-3 hidden sm:table-cell">
                          {run.attributes['is-destroy'] ? (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-900/50 text-red-300">
                              {t('runs.typeDestroy')}
                            </span>
                          ) : run.attributes['plan-only'] ? (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-cyan-900/50 text-cyan-300">
                              {t('runs.typePlanOnly')}
                            </span>
                          ) : (
                            <span className="text-xs text-slate-500">{t('runs.typePlanApply')}</span>
                          )}
                        </td>
                        <td className="px-4 py-3 hidden md:table-cell">
                          {run.attributes['plan-summary'] ? (
                            <PlanSummaryBadges summary={run.attributes['plan-summary']} size="sm" />
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
                          <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${statusColor(run.attributes.status)}`}>
                            {run.attributes.status}
                          </span>
                        )
                      }
                      fields={[
                        {
                          label: t('runs.type'),
                          value: run.attributes['is-destroy'] ? (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-900/50 text-red-300">
                              {t('runs.typeDestroy')}
                            </span>
                          ) : run.attributes['plan-only'] ? (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-cyan-900/50 text-cyan-300">
                              {t('runs.typePlanOnly')}
                            </span>
                          ) : (
                            <span className="text-slate-400">{t('runs.typePlanApply')}</span>
                          ),
                        },
                        ...(run.attributes['plan-summary']
                          ? [{
                              label: t('runs.changes'),
                              value: <PlanSummaryBadges summary={run.attributes['plan-summary']} size="sm" />,
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
                        <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">{t('state.run')}</th>
                        <SortableHeader label={t('state.size')} sortKey="size" sortState={stateSortState} onSort={toggleStateSort} />
                        <SortableHeader label={t('state.created')} sortKey="created-at" sortState={stateSortState} onSort={toggleStateSort} className="hidden lg:table-cell" />
                        <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">{t('common.actions')}</th>
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
                            <td className="px-4 py-3 text-right">
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
          </div>
        )}

        {/* State Graph Tab (#765) */}
        {activeTab === 'state-graph' && workspace && (
          <StateGraphTab workspaceId={workspace.id} />
        )}

        {/* Configurations Tab */}
        {activeTab === 'configurations' && (
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
              <EmptyState message={t('configurations.empty')} />
            ) : (
              <>
                {/* Desktop (md+): the table with per-row compare checkboxes. */}
                <div className="hidden md:block overflow-hidden rounded-xl border border-slate-800">
                  <table className="w-full text-sm">
                    <thead className="bg-slate-900/50 text-slate-400">
                      <tr>
                        <th className="w-8 px-2 py-3" aria-hidden />
                        <th className="px-4 py-3 text-left font-medium">{t('configurations.id')}</th>
                        <th className="px-4 py-3 text-left font-medium">{t('configurations.source')}</th>
                        <th className="px-4 py-3 text-left font-medium">{t('configurations.status')}</th>
                        <th className="px-4 py-3 text-left font-medium">{t('configurations.created')}</th>
                        <th className="px-4 py-3 text-right font-medium">{t('actions.download')}</th>
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
                                <span className="ml-2 inline-flex items-center rounded bg-green-900/40 px-1.5 py-0.5 text-xs font-medium text-green-300">
                                  {t('configurations.current')}
                                </span>
                              )}
                            </td>
                            <td className="px-4 py-3 text-slate-300">{cv.attributes.source}</td>
                            <td className="px-4 py-3 text-slate-400">{cv.attributes.status}</td>
                            <td className="px-4 py-3 text-slate-400">
                              {new Date(cv.attributes['created-at']).toLocaleString()}
                            </td>
                            <td className="px-4 py-3 text-right">
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
              {t('runTriggers.description')}
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
                      <span className="ml-2 text-xs text-slate-500">{t('runTriggers.destinationNote')}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}

        {/* Sharing Tab — cross-workspace remote-state allowlist (#344, #349) */}
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
                          <span className="ml-2 text-xs text-slate-500">{t('sharing.grantedBy', { by: e.createdBy })}</span>
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
                      <span className="ml-2 text-xs text-slate-500">{t('sharing.producerNote')}</span>
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
