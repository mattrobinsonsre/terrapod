'use client'

import { useEffect, useState, useCallback, Suspense } from 'react'
import { useRouter, useParams, useSearchParams } from 'next/navigation'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { LabelsEditor } from '@/components/labels-editor'
import { HealthConditions } from '@/components/health-conditions'
import { PlanSummaryBadges } from '@/components/plan-summary-badges'
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
  'vcs-repo-url': string
  'vcs-branch': string
  'vcs-connection-id': string | null
  'vcs-connection-name': string | null
  'vcs-workflow': 'merge_then_apply' | 'apply_then_merge'
  'auto-merge': boolean
  'auto-merge-strategy': 'merge' | 'squash' | 'rebase'
  'drift-detection-enabled': boolean
  'drift-detection-interval-seconds': number
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

type Tab = 'overview' | 'variables' | 'runs' | 'state' | 'configurations' | 'notifications' | 'run-tasks' | 'sharing'

const VALID_TABS: Set<string> = new Set(['overview', 'variables', 'runs', 'state', 'configurations', 'notifications', 'run-tasks', 'sharing'])

export default function WorkspaceDetailPage() {
  return (
    <Suspense fallback={<><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>}>
      <WorkspaceDetailContent />
    </Suspense>
  )
}

function WorkspaceDetailContent() {
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
  const [editPoolId, setEditPoolId] = useState<string | null>(null)
  const [editLabels, setEditLabels] = useState<Record<string, string>>({})
  const [editOwner, setEditOwner] = useState('')
  const [editVarFiles, setEditVarFiles] = useState<string[]>([])
  const [newVarFile, setNewVarFile] = useState('')
  const [editTriggerPrefixes, setEditTriggerPrefixes] = useState<string[]>([])
  const [newTriggerPrefix, setNewTriggerPrefix] = useState('')
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
  const [checkingDrift, setCheckingDrift] = useState(false)
  const [dismissingDrift, setDismissingDrift] = useState(false)

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
  const [rscAddName, setRscAddName] = useState('')
  const [rscAdding, setRscAdding] = useState(false)

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
  const [deleteNotifId, setDeleteNotifId] = useState<string | null>(null)
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
  const [deleteRtId, setDeleteRtId] = useState<string | null>(null)

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
      if (!res.ok) throw new Error('Failed to load workspace')
      const data = await res.json()
      setWorkspace(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load workspace')
    } finally {
      setLoading(false)
    }
  }, [workspaceId])

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadWorkspace()
  }, [router, loadWorkspace])

  const loadRuns = useCallback(async () => {
    setRunsLoading(true)
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/runs`)
      if (!res.ok) throw new Error('Failed to load runs')
      const data = await res.json()
      setRuns(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load runs')
    } finally {
      setRunsLoading(false)
    }
  }, [workspaceId])

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
  useRunEvents(workspaceId, useCallback((event) => {
    loadWorkspace()
    if (activeTab === 'runs') loadRuns()
    if (activeTab === 'state' && (event.event === 'state_version_created' || event.event === 'reconnect')) loadStateVersions()
  }, [activeTab, loadRuns, loadWorkspace]))

  async function loadVariables() {
    setVarsLoading(true)
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/vars`)
      if (!res.ok) throw new Error('Failed to load variables')
      const data = await res.json()
      setVariables(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load variables')
    } finally {
      setVarsLoading(false)
    }
  }

  async function loadStateVersions() {
    setStateLoading(true)
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/state-versions`)
      if (!res.ok) throw new Error('Failed to load state versions')
      const data = await res.json()
      setStateVersions(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load state versions')
    } finally {
      setStateLoading(false)
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
      if (!res.ok) throw new Error('Failed to load configuration versions')
      const data = await res.json()
      setCvs(data.data || [])
      setCvCurrentId(data.meta?.['current-id'] ?? null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load configuration versions')
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
        throw new Error(errBody.detail || `Download failed (${res.status})`)
      }
      const body = await res.json()
      const url = body?.data?.attributes?.url
      if (!url) throw new Error('Download ticket response missing URL')
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
      setError(err instanceof Error ? err.message : 'Download failed')
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
        throw new Error(errBody.detail || `Diff failed (${res.status})`)
      }
      const data = await res.json()
      setCvDiff(data.data.attributes)
    } catch (err) {
      setCvDiffError(err instanceof Error ? err.message : 'Diff failed')
    } finally {
      setCvDiffLoading(false)
    }
  }

  async function loadNotifications() {
    setNotifLoading(true)
    try {
      const res = await apiFetch(`/api/terrapod/v1/workspaces/${workspaceId}/notification-configurations`)
      if (!res.ok) throw new Error('Failed to load notifications')
      const data = await res.json()
      setNotifications(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load notifications')
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
      if (!outRes.ok) throw new Error('Failed to load outbound remote-state consumers')
      if (!inRes.ok) throw new Error('Failed to load inbound remote-state consumers')
      const outData = await outRes.json()
      const inData = await inRes.json()
      setRscOutbound((outData.data || []).map(_rscFromRow))
      setRscInbound((inData.data || []).map(_rscFromRow))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load remote-state consumers')
    } finally {
      setRscLoading(false)
    }
  }

  async function addRemoteStateConsumer() {
    const name = rscAddName.trim()
    if (!name) return
    setRscAdding(true)
    try {
      // Resolve consumer workspace name → id via the by-name endpoint.
      const lookup = await apiFetch(`/api/v2/organizations/default/workspaces/${encodeURIComponent(name)}`)
      if (!lookup.ok) {
        throw new Error(lookup.status === 404 ? `Workspace "${name}" not found` : 'Failed to resolve consumer workspace')
      }
      const wsBody = await lookup.json()
      const consumerId = wsBody.data?.id
      if (!consumerId) throw new Error('Workspace lookup returned no id')

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
        const err = await res.json().catch(() => ({ detail: 'Failed to authorize consumer' }))
        throw new Error(err.detail || 'Failed to authorize consumer')
      }
      setRscAddName('')
      await loadRemoteStateConsumers()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to authorize consumer')
    } finally {
      setRscAdding(false)
    }
  }

  async function revokeRemoteStateConsumer(edgeId: string) {
    try {
      const res = await apiFetch(`/api/terrapod/v1/remote-state-consumers/${edgeId}`, { method: 'DELETE' })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Failed to revoke' }))
        throw new Error(err.detail || 'Failed to revoke')
      }
      await loadRemoteStateConsumers()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to revoke consumer')
    }
  }

  async function loadRunTasks() {
    setRunTasksLoading(true)
    try {
      const res = await apiFetch(`/api/terrapod/v1/workspaces/${workspaceId}/run-tasks`)
      if (!res.ok) throw new Error('Failed to load run tasks')
      const data = await res.json()
      setRunTasks(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load run tasks')
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
        throw new Error(data.detail || `Failed to create run task (${res.status})`)
      }
      setRtName('')
      setRtUrl('')
      setRtStage('post_plan')
      setRtEnforcement('mandatory')
      setRtHmacKey('')
      setShowAddRunTask(false)
      await loadRunTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create run task')
    } finally {
      setAddingRunTask(false)
    }
  }

  async function handleToggleRunTask(rt: RunTaskItem) {
    try {
      const res = await apiFetch(`/api/terrapod/v1/run-tasks/${rt.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'run-tasks', attributes: { enabled: !rt.attributes.enabled } } }),
      })
      if (!res.ok) throw new Error('Failed to update')
      await loadRunTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to toggle run task')
    }
  }

  async function handleDeleteRunTask(rtId: string) {
    try {
      const res = await apiFetch(`/api/terrapod/v1/run-tasks/${rtId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete')
      setDeleteRtId(null)
      await loadRunTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete run task')
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
    setEditPoolId(workspace.attributes['agent-pool-id'])
    setEditLabels(workspace.attributes.labels || {})
    setEditOwner(workspace.attributes['owner-email'] || '')
    setEditVarFiles(workspace.attributes['var-files'] || [])
    setNewVarFile('')
    setEditTriggerPrefixes(workspace.attributes['trigger-prefixes'] || [])
    setNewTriggerPrefix('')
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
              'agent-pool-id': editPoolId,
              'working-directory': editWorkingDir,
              'var-files': editVarFiles,
              'trigger-prefixes': editTriggerPrefixes,
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
        const detail = errData.errors?.[0]?.detail || 'This label change would reduce your access.'
        setLockoutWarning(detail)
        return
      }
      if (!res.ok) throw new Error('Failed to update workspace')
      const data = await res.json()
      const wasRenamed = workspace && data.data.attributes.name !== workspace.attributes.name
      setWorkspace(data.data)
      setEditing(false)
      if (wasRenamed) setNameChanged(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update workspace')
    } finally {
      setSaving(false)
    }
  }

  async function handleLockToggle() {
    if (!workspace) return
    const action = workspace.attributes.locked ? 'unlock' : 'lock'
    try {
      // lock/unlock are TFE V2 CLI-contract endpoints — only at /api/v2/.
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/actions/${action}`, {
        method: 'POST',
      })
      if (!res.ok) throw new Error(`Failed to ${action} workspace`)
      await loadWorkspace()
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${action} workspace`)
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
      if (!res.ok) throw new Error('Failed to update drift settings')
      const data = await res.json()
      setWorkspace(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update drift settings')
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
      if (!res.ok) throw new Error('Failed to update drift interval')
      const data = await res.json()
      setWorkspace(data.data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update drift interval')
    } finally {
      setSavingDrift(false)
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
              message: 'Manual drift check from UI',
            },
            relationships: {
              workspace: { data: { type: 'workspaces', id: workspaceId } },
            },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to queue drift check (${res.status})`)
      }
      const runData = await res.json().catch(() => null)
      const newRunId = runData?.data?.id as string | undefined
      if (newRunId) {
        setLastQueuedRunId(newRunId)
        setTimeout(() => setLastQueuedRunId((prev) => prev === newRunId ? null : prev), 8000)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to queue drift check')
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
        throw new Error(data.detail || `Failed to dismiss drift (${res.status})`)
      }
      await loadWorkspace()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to dismiss drift')
    } finally {
      setDismissingDrift(false)
    }
  }

  async function handleDelete() {
    setDeleting(true)
    try {
      const res = await apiFetch(`/api/terrapod/v1/workspaces/${workspaceId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete workspace')
      router.push('/workspaces')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete workspace')
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
        throw new Error(data.detail || `Failed to add variable (${res.status})`)
      }
      setVarKey('')
      setVarValue('')
      setVarCategory('terraform')
      setVarSensitive(false)
      setVarHcl(false)
      setShowAddVar(false)
      await loadVariables()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add variable')
    } finally {
      setAddingVar(false)
    }
  }

  async function handleDeleteVariable(varId: string) {
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/vars/${varId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete variable')
      await loadVariables()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete variable')
    }
  }

  async function handleQueuePlan() {
    setQueueingPlan(true)
    setError('')
    try {
      const attrs: Record<string, unknown> = {
        'plan-only': planOnly,
        message: planOnly ? 'Queued from UI (speculative)' : 'Queued from UI',
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
        throw new Error(data.detail || `Failed to queue plan (${res.status})`)
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
      setError(err instanceof Error ? err.message : 'Failed to queue plan')
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
              message: 'Destroy queued from UI',
            },
            relationships: {
              workspace: { data: { type: 'workspaces', id: workspaceId } },
            },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to queue destroy (${res.status})`)
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
      setError(err instanceof Error ? err.message : 'Failed to queue destroy')
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
        throw new Error(data.detail || 'Failed to update variable')
      }
      setEditingVarId(null)
      await loadVariables()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update variable')
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
        throw new Error(data.detail || `Failed to create notification (${res.status})`)
      }
      setNotifName('')
      setNotifUrl('')
      setNotifToken('')
      setNotifEmails('')
      setNotifTriggers(new Set())
      setShowAddNotif(false)
      await loadNotifications()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create notification')
    } finally {
      setAddingNotif(false)
    }
  }

  async function handleToggleNotif(nc: NotificationConfig) {
    try {
      const res = await apiFetch(`/api/terrapod/v1/notification-configurations/${nc.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'notification-configurations', attributes: { enabled: !nc.attributes.enabled } } }),
      })
      if (!res.ok) throw new Error('Failed to update')
      await loadNotifications()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to toggle notification')
    }
  }

  async function handleDeleteNotif(ncId: string) {
    try {
      const res = await apiFetch(`/api/terrapod/v1/notification-configurations/${ncId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete')
      setDeleteNotifId(null)
      await loadNotifications()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete notification')
    }
  }

  async function handleVerifyNotif(ncId: string) {
    setVerifyingId(ncId)
    setError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/notification-configurations/${ncId}/actions/verify`, { method: 'POST' })
      if (!res.ok) throw new Error('Verification failed')
      const data = await res.json()
      const success = data?.data?.attributes?.success
      if (success) {
        setError('')
      } else {
        setError(`Verification delivery failed: ${data?.data?.attributes?.body || 'Unknown error'}`)
      }
      await loadNotifications()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Verification failed')
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
    { key: 'overview', label: 'Overview' },
    { key: 'variables', label: 'Variables' },
    { key: 'runs', label: 'Runs' },
    { key: 'state', label: 'State' },
    { key: 'configurations', label: 'Configurations' },
    { key: 'notifications', label: 'Notifications' },
    { key: 'run-tasks', label: 'Run Tasks' },
    { key: 'sharing', label: 'Sharing' },
  ]

  function statusColor(status: string): string {
    switch (status) {
      case 'applied': return 'bg-green-900/50 text-green-300'
      case 'planned': return 'bg-blue-900/50 text-blue-300'
      case 'planning': case 'applying': return 'bg-yellow-900/50 text-yellow-300'
      case 'errored': return 'bg-red-900/50 text-red-300'
      case 'canceled': case 'discarded': return 'bg-slate-700 text-slate-400'
      default: return 'bg-slate-700 text-slate-400'
    }
  }

  function driftStatusBadge(s: string): { cls: string; label: string } {
    switch (s) {
      case 'no_drift': return { cls: 'bg-green-900/50 text-green-300', label: 'No Drift' }
      case 'drifted': return { cls: 'bg-amber-900/50 text-amber-300', label: 'Drifted' }
      case 'errored': return { cls: 'bg-red-900/50 text-red-300', label: 'Errored' }
      default: return { cls: 'bg-slate-700 text-slate-400', label: 'Unchecked' }
    }
  }

  const DRIFT_INTERVALS = [
    { label: '1 hour', value: 3600 },
    { label: '4 hours', value: 14400 },
    { label: '12 hours', value: 43200 },
    { label: '24 hours', value: 86400 },
    { label: '48 hours', value: 172800 },
    { label: '7 days', value: 604800 },
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
  if (!workspace) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><ErrorBanner message="Workspace not found" /></main></>

  const attrs = workspace.attributes
  const perms = attrs.permissions || {} as WorkspacePermissions

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={attrs.name}
          description={`${attrs['execution-mode']} execution mode`}
        />

        {error && <ErrorBanner message={error} />}

        {attrs['lifecycle-state'] === 'pending_deletion' && (
          <div className="mb-4 p-4 rounded-lg bg-amber-900/30 border border-amber-700/50">
            <p className="text-sm font-semibold text-amber-300">Pending deletion</p>
            <p className="text-sm text-amber-200/80 mt-1">
              {attrs['lifecycle-reason'] || 'This workspace is marked for deletion and requires manual action.'}
            </p>
          </div>
        )}

        {attrs['lifecycle-state'] === 'archived' && (
          <div className="mb-4 p-4 rounded-lg bg-slate-800/60 border border-slate-600/50">
            <p className="text-sm font-semibold text-slate-300">Archived</p>
            <p className="text-sm text-slate-400 mt-1">
              {attrs['lifecycle-reason'] || 'This workspace has been archived.'}
            </p>
          </div>
        )}

        {lastQueuedRunId && (
          <div className="mb-4 p-3 bg-brand-900/30 rounded-lg border border-brand-700/50 flex items-center justify-between">
            <p className="text-sm text-brand-300">
              Run queued successfully.
            </p>
            <button
              onClick={() => { setLastQueuedRunId(null); router.push(`/workspaces/${workspaceId}/runs/${lastQueuedRunId}`) }}
              className="text-sm font-medium text-brand-400 hover:text-brand-300 transition-colors"
            >
              View Run &rarr;
            </button>
          </div>
        )}

        {/* Tabs */}
        <div className="border-b border-slate-700/50 mb-6">
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
                <h3 className="text-sm font-medium text-slate-300">Settings</h3>
                {!editing ? (
                  perms['can-update'] && <button onClick={startEditing} className="text-xs text-brand-400 hover:text-brand-300">
                    Edit
                  </button>
                ) : (
                  <div className="flex gap-2">
                    <button onClick={() => setEditing(false)} className="text-xs text-slate-400 hover:text-slate-200">Cancel</button>
                    <button onClick={() => handleSave()} disabled={saving} className="text-xs text-brand-400 hover:text-brand-300">
                      {saving ? 'Saving...' : 'Save'}
                    </button>
                  </div>
                )}
              </div>
              {nameChanged && (
                <div className="mb-4 rounded-lg border border-blue-500/50 bg-blue-500/10 p-3 text-sm text-blue-300">
                  Workspace renamed. Update the <code className="bg-slate-700 px-1 rounded">name</code> field in your <code className="bg-slate-700 px-1 rounded">cloud {'{'}{'}' }</code> block to match.
                </div>
              )}
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <dt className="text-xs text-slate-500">Name</dt>
                  {editing ? (
                    <input type="text" value={editName} onChange={(e) => setEditName(e.target.value)}
                      pattern="[a-zA-Z0-9][a-zA-Z0-9_\-]*" maxLength={90}
                      title="Letters, numbers, hyphens, underscores. Must start with a letter or number."
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs.name}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Execution Mode</dt>
                  {editing ? (
                    <select value={editExecMode} onChange={(e) => setEditExecMode(e.target.value)} className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500">
                      <option value="local">Local</option>
                      <option value="agent">Agent</option>
                    </select>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['execution-mode']}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Auto Apply</dt>
                  {editing ? (
                    <label className="flex items-center gap-2 mt-1">
                      <input type="checkbox" checked={editAutoApply} onChange={(e) => setEditAutoApply(e.target.checked)} className="rounded border-slate-600 bg-slate-700 text-brand-600" />
                      <span className="text-sm text-slate-200">{editAutoApply ? 'Enabled' : 'Disabled'}</span>
                    </label>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['auto-apply'] ? 'Enabled' : 'Disabled'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">CPU Request</dt>
                  {editing ? (
                    <input type="text" value={editCpu} onChange={(e) => setEditCpu(e.target.value)}
                      pattern="[0-9]+m|[0-9]+(\.[0-9]+)?"
                      title="Kubernetes CPU quantity: whole cores (1, 2) or millicores (500m, 100m)"
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['resource-cpu']}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Memory Request</dt>
                  {editing ? (
                    <input type="text" value={editMemory} onChange={(e) => setEditMemory(e.target.value)}
                      pattern="[0-9]+(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E|m)?"
                      title="Kubernetes memory quantity: bytes (1000) or with suffix (512Mi, 2Gi, 1Ti)"
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['resource-memory']}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Execution Backend</dt>
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
                  <dt className="text-xs text-slate-500">Version</dt>
                  {editing ? (
                    <>
                      <input type="text" list="edit-version-suggestions" value={editVersion} onChange={(e) => setEditVersion(e.target.value)} placeholder="e.g. 1.11 or 1.11.5"
                        pattern="[0-9]+\.[0-9]+(\.[0-9]+)?"
                        title="Version in X.Y or X.Y.Z format (e.g. 1.11 or 1.11.5)"
                        className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                      <datalist id="edit-version-suggestions">
                        {versionSuggestions.map(v => (
                          <option key={v} value={v} />
                        ))}
                      </datalist>
                    </>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['terraform-version'] || 'Default'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Working Directory</dt>
                  {editing ? (
                    <input type="text" value={editWorkingDir} onChange={(e) => setEditWorkingDir(e.target.value)} placeholder="e.g. environments/dev"
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['working-directory'] || '/'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Agent Pool</dt>
                  {editing ? (
                    <select
                      value={editPoolId || ''}
                      onChange={(e) => setEditPoolId(e.target.value || null)}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                    >
                      <option value="">None</option>
                      {agentPools.map((p) => (
                        <option key={p.id} value={p.id}>{p.attributes.name}</option>
                      ))}
                    </select>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">
                      {attrs['agent-pool-name'] || (attrs['agent-pool-id'] ? attrs['agent-pool-id'] : 'None')}
                    </dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Owner</dt>
                  {editing && isAdmin() ? (
                    <input type="email" value={editOwner} onChange={(e) => setEditOwner(e.target.value)} placeholder="user@example.com" className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['owner-email'] || 'None'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">VCS Connection</dt>
                  {editing ? (
                    <select
                      value={editVcsConnectionId || ''}
                      onChange={(e) => setEditVcsConnectionId(e.target.value || null)}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                    >
                      <option value="">None</option>
                      {vcsConnections.map((c) => (
                        <option key={c.id} value={c.id}>{c.attributes.name} ({c.attributes.provider})</option>
                      ))}
                    </select>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['vcs-connection-name'] || 'None'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">VCS Repository</dt>
                  {editing ? (
                    <input type="text" value={editVcsRepoUrl} onChange={(e) => setEditVcsRepoUrl(e.target.value)} placeholder="https://github.com/org/repo"
                      pattern="https?://.+"
                      title="Must be an HTTP or HTTPS URL"
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">
                      {attrs['vcs-repo-url'] ? (
                        <a href={attrs['vcs-repo-url']} target="_blank" rel="noopener noreferrer" className="text-brand-400 hover:text-brand-300">{attrs['vcs-repo-url']}</a>
                      ) : 'None'}
                    </dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">VCS Branch</dt>
                  {editing ? (
                    <input type="text" value={editVcsBranch} onChange={(e) => setEditVcsBranch(e.target.value)} placeholder="main (default)" className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['vcs-branch'] || 'Default'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">VCS Workflow</dt>
                  {editing ? (
                    <select
                      value={editVcsWorkflow}
                      onChange={(e) => setEditVcsWorkflow(e.target.value as 'merge_then_apply' | 'apply_then_merge')}
                      className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
                    >
                      <option value="merge_then_apply">merge_then_apply (default — TFE / HCP standard)</option>
                      <option value="apply_then_merge">apply_then_merge (Atlantis standard — opt-in)</option>
                    </select>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['vcs-workflow']}</dd>
                  )}
                </div>
                {editing && editVcsWorkflow === 'apply_then_merge' && (
                  <div className="sm:col-span-2 rounded border border-amber-700 bg-amber-900/30 p-3 text-xs text-amber-100">
                    <p className="font-medium">Authorization delegated to your VCS repository.</p>
                    <p className="mt-1">
                      Anyone who can merge the PR can apply. Branch protection rules (required reviews,
                      status checks, code owner approval) become the gate. Terrapod role/label RBAC does
                      <em> not</em> apply to comment-driven actions in this mode.
                    </p>
                    <p className="mt-1">
                      <strong>Recommended:</strong> require linear history (rebase/squash before merge) so the
                      commit you apply is the commit that lands.
                    </p>
                    <p className="mt-1">
                      Credit: this workflow is modelled on{' '}
                      <a href="https://www.runatlantis.io/" target="_blank" rel="noopener noreferrer" className="underline">Atlantis</a>.
                      Atlantis remains the right tool for teams who only want PR-comment-driven applies and no platform UI.
                    </p>
                  </div>
                )}
                <div>
                  <dt className="text-xs text-slate-500">Auto-merge after apply</dt>
                  {editing ? (
                    <label className="mt-1 flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={editAutoMerge}
                        onChange={(e) => setEditAutoMerge(e.target.checked)}
                        className="rounded border-slate-600 bg-slate-700 text-brand-600"
                      />
                      <span className="text-sm text-slate-200">{editAutoMerge ? 'Enabled' : 'Disabled'}</span>
                    </label>
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['auto-merge'] ? 'Enabled' : 'Disabled'}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Auto-merge Strategy</dt>
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
                    <dt className="text-xs text-slate-500">VCS Polling</dt>
                    <dd className="mt-1 text-sm">
                      {attrs['vcs-last-error'] ? (
                        <span className="text-red-400" title={attrs['vcs-last-error']}>Error{attrs['vcs-last-error-at'] ? ` (${new Date(attrs['vcs-last-error-at']).toLocaleString()})` : ''}</span>
                      ) : attrs['vcs-last-polled-at'] ? (
                        <span className="text-green-400">OK ({new Date(attrs['vcs-last-polled-at']).toLocaleString()})</span>
                      ) : (
                        <span className="text-slate-400">Not polled yet</span>
                      )}
                    </dd>
                  </div>
                )}
                <div className="sm:col-span-2">
                  <dt
                    className="text-xs text-slate-500 mb-1"
                    title='Workspace labels do double duty: label-based RBAC matching, and as the "tags" matched by terraform/tofu cloud { workspaces { tags = ... } } blocks.'
                  >
                    Labels (tags)
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
                  <dt className="text-xs text-slate-500 mb-1">Var Files</dt>
                  {editing && perms['can-update'] ? (
                    <div className="space-y-2">
                      {editVarFiles.map((f, i) => (
                        <div key={f} className="flex items-center gap-2">
                          <code className="text-sm text-slate-200 bg-slate-700 px-2 py-0.5 rounded flex-1 truncate">{f}</code>
                          <button
                            onClick={() => setEditVarFiles(editVarFiles.filter((_, j) => j !== i))}
                            className="text-xs text-red-400 hover:text-red-300"
                          >Remove</button>
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
                          placeholder="e.g. envs/dev.tfvars"
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
                        >Add</button>
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
                        <span className="text-slate-500">None</span>
                      )}
                    </dd>
                  )}
                </div>
                <div className="sm:col-span-2">
                  <dt className="text-xs text-slate-500 mb-1">Trigger Prefixes</dt>
                  {editing && perms['can-update'] ? (
                    <div className="space-y-2">
                      <p className="text-xs text-slate-400">Directories that trigger runs. Overrides working directory filtering when set.</p>
                      {editTriggerPrefixes.map((f, i) => (
                        <div key={f} className="flex items-center gap-2">
                          <code className="text-sm text-slate-200 bg-slate-700 px-2 py-0.5 rounded flex-1 truncate">{f}</code>
                          <button
                            onClick={() => setEditTriggerPrefixes(editTriggerPrefixes.filter((_, j) => j !== i))}
                            className="text-xs text-red-400 hover:text-red-300"
                          >Remove</button>
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
                          placeholder="e.g. modules"
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
                        >Add</button>
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
                        <span className="text-slate-500">None (uses working directory)</span>
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
                      Revert Labels
                    </button>
                    <button
                      onClick={() => handleSave(true)}
                      disabled={saving}
                      className="px-3 py-1 rounded text-xs text-amber-200 hover:text-white bg-amber-700 hover:bg-amber-600"
                    >
                      {saving ? 'Saving...' : 'Save Anyway'}
                    </button>
                  </div>
                </div>
              )}
            </div>

            {/* Lock / Unlock */}
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-sm font-medium text-slate-300">Lock Status</h3>
                  <p className="text-sm text-slate-400 mt-1">
                    {attrs.locked ? 'This workspace is locked. No plans or applies can run.' : 'This workspace is unlocked and ready for runs.'}
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
                    {attrs.locked ? 'Unlock' : 'Lock'}
                  </button>
                )}
              </div>
            </div>

            {/* Drift Detection */}
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-sm font-medium text-slate-300">Drift Detection</h3>
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
                    {savingDrift ? 'Saving...' : attrs['drift-detection-enabled'] ? 'Enabled' : 'Disabled'}
                  </button>
                ) : (
                  <span className={`px-3 py-1.5 rounded-lg text-sm font-medium ${
                    attrs['drift-detection-enabled'] ? 'bg-green-900/50 text-green-300' : 'bg-slate-700 text-slate-400'
                  }`}>
                    {attrs['drift-detection-enabled'] ? 'Enabled' : 'Disabled'}
                  </span>
                )}
              </div>
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <dt className="text-xs text-slate-500">Check Interval</dt>
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
                  <dt className="text-xs text-slate-500">Status</dt>
                  <dd className="mt-1 flex items-center gap-2">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${driftStatusBadge(attrs['drift-status']).cls}`}>
                      {driftStatusBadge(attrs['drift-status']).label}
                    </span>
                    {perms['can-queue-run'] && (attrs['drift-status'] === 'drifted' || attrs['drift-status'] === 'errored') && (
                      <button
                        onClick={handleDismissDrift}
                        disabled={dismissingDrift}
                        title="Clear the reported drift state. The next scheduled check will repopulate it from reality."
                        className="text-xs text-slate-400 hover:text-slate-200 disabled:text-slate-600 transition-colors"
                      >
                        {dismissingDrift ? 'Dismissing…' : 'Dismiss'}
                      </button>
                    )}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Last Checked</dt>
                  <dd className="mt-1 text-sm text-slate-200">
                    {attrs['drift-last-checked-at'] ? new Date(attrs['drift-last-checked-at']).toLocaleString() : 'Never'}
                  </dd>
                </div>
                {perms['can-queue-run'] && (
                  <div className="flex items-end">
                    <button
                      onClick={handleCheckDriftNow}
                      disabled={checkingDrift || attrs.locked || !attrs['drift-detection-enabled']}
                      className="px-3 py-1.5 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
                      title={!attrs['drift-detection-enabled'] ? 'Enable drift detection first' : attrs.locked ? 'Workspace is locked' : 'Queue a plan-only run to check for drift'}
                    >
                      {checkingDrift ? 'Queuing...' : 'Check Now'}
                    </button>
                  </div>
                )}
              </dl>
            </div>

            {/* Delete */}
            {perms['can-destroy'] && (
              <div className="bg-slate-800/50 rounded-lg border border-red-900/30 p-6">
                <div className="flex items-center justify-between">
                  <div>
                    <h3 className="text-sm font-medium text-red-400">Delete Workspace</h3>
                    <p className="text-sm text-slate-400 mt-1">Permanently delete this workspace and all associated state, variables, and runs.</p>
                  </div>
                  {!showDeleteConfirm ? (
                    <button
                      onClick={() => setShowDeleteConfirm(true)}
                      className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600/20 hover:bg-red-600/40 text-red-400 transition-colors"
                    >
                      Delete
                    </button>
                  ) : (
                    <div className="flex gap-2">
                      <button onClick={() => setShowDeleteConfirm(false)} className="px-3 py-1.5 rounded-lg text-sm font-medium text-slate-400 hover:text-slate-200">
                        Cancel
                      </button>
                      <button
                        onClick={handleDelete}
                        disabled={deleting}
                        className="px-3 py-1.5 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-500 text-white transition-colors"
                      >
                        {deleting ? 'Deleting...' : 'Confirm Delete'}
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
                  {showAddVar ? 'Cancel' : 'Add Variable'}
                </button>
              </div>
            )}

            {showAddVar && (
              <form onSubmit={handleAddVariable} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="var-key" className="block text-sm font-medium text-slate-300 mb-1">Key</label>
                    <input id="var-key" type="text" value={varKey} onChange={(e) => setVarKey(e.target.value)} required placeholder="AWS_REGION" className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="var-val" className="block text-sm font-medium text-slate-300 mb-1">Value</label>
                    <textarea id="var-val" value={varValue} onChange={(e) => setVarValue(e.target.value)} placeholder="us-east-1" rows={2} className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent resize-y" />
                  </div>
                  <div>
                    <label htmlFor="var-cat" className="block text-sm font-medium text-slate-300 mb-1">Category</label>
                    <select id="var-cat" value={varCategory} onChange={(e) => setVarCategory(e.target.value)} className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      <option value="terraform">Terraform</option>
                      <option value="env">Environment</option>
                    </select>
                  </div>
                  <div className="flex items-end gap-4">
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input type="checkbox" checked={varSensitive} onChange={(e) => setVarSensitive(e.target.checked)} className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                      <span className="text-sm text-slate-300">Sensitive</span>
                    </label>
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input type="checkbox" checked={varHcl} onChange={(e) => setVarHcl(e.target.checked)} className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                      <span className="text-sm text-slate-300">HCL</span>
                    </label>
                  </div>
                </div>
                <button type="submit" disabled={addingVar} className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {addingVar ? 'Adding...' : 'Add Variable'}
                </button>
              </form>
            )}

            {varsLoading ? (
              <LoadingSpinner />
            ) : variables.length === 0 ? (
              <EmptyState message="No variables configured for this workspace." />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <SortableHeader label="Key" sortKey="key" sortState={varSortState} onSort={toggleVarSort} />
                      <SortableHeader label="Value" sortKey="value" sortState={varSortState} onSort={toggleVarSort} />
                      <SortableHeader label="Category" sortKey="category" sortState={varSortState} onSort={toggleVarSort} className="hidden sm:table-cell" />
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">Actions</th>
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
                            <textarea value={editVarValue} onChange={(e) => setEditVarValue(e.target.value)}
                              placeholder={editVarSensitive ? 'Enter new value' : ''}
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
                                <span className="text-xs text-slate-400">Sensitive</span>
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
                              <button onClick={() => setEditingVarId(null)} className="text-xs text-slate-400 hover:text-slate-200">Cancel</button>
                              <button onClick={handleSaveVar} disabled={savingVar} className="text-xs text-brand-400 hover:text-brand-300">
                                {savingVar ? 'Saving...' : 'Save'}
                              </button>
                            </div>
                          </td>
                        </tr>
                      ) : (
                        <tr key={v.id} className="hover:bg-slate-700/20 transition-colors">
                          <td className="px-4 py-3 text-sm text-slate-200 font-mono">{v.attributes.key}</td>
                          <td className="px-4 py-3 text-sm text-slate-400 font-mono">
                            {v.attributes.sensitive ? '***' : (v.attributes.value || <span className="text-slate-600 italic">empty</span>)}
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
                                <button onClick={() => startEditingVar(v)} className="text-xs text-brand-400 hover:text-brand-300">Edit</button>
                                <button onClick={() => handleDeleteVariable(v.id)} className="text-xs text-red-400 hover:text-red-300">Delete</button>
                              </div>
                            </td>
                          )}
                        </tr>
                      )
                    )}
                  </tbody>
                </table>
              </div>
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
                    {showPlanOptions ? 'Hide Options' : 'Options'}
                  </button>
                  {!showDestroyConfirm ? (
                    <button
                      onClick={() => setShowDestroyConfirm(true)}
                      disabled={queueingDestroy || attrs.locked}
                      className="px-4 py-2 rounded-lg text-sm font-medium bg-red-600/20 hover:bg-red-600/40 text-red-400 transition-colors"
                      title={attrs.locked ? 'Workspace is locked' : 'Queue a destroy plan'}
                    >
                      Queue Destroy
                    </button>
                  ) : (
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-red-400">Destroy all resources?</span>
                      <button
                        onClick={() => setShowDestroyConfirm(false)}
                        className="px-3 py-1.5 rounded-lg text-sm font-medium text-slate-400 hover:text-slate-200 transition-colors"
                      >
                        Cancel
                      </button>
                      <button
                        onClick={handleQueueDestroy}
                        disabled={queueingDestroy}
                        className="px-4 py-2 rounded-lg text-sm font-medium bg-red-600 hover:bg-red-500 disabled:bg-red-800 text-white transition-colors"
                      >
                        {queueingDestroy ? 'Queuing...' : 'Confirm Destroy'}
                      </button>
                    </div>
                  )}
                  <button
                    onClick={handleQueuePlan}
                    disabled={queueingPlan || attrs.locked}
                    className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
                    title={attrs.locked ? 'Workspace is locked' : undefined}
                  >
                    {queueingPlan ? 'Queuing...' : planOnly ? 'Queue Plan' : 'Queue Run'}
                  </button>
                </div>
                {showPlanOptions && (
                  <div className="mt-3 p-4 bg-slate-800/50 rounded-lg border border-slate-700/50">
                    <h4 className="text-sm font-medium text-slate-300 mb-3">Plan Options</h4>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <div>
                        <label className="block text-xs text-slate-400 mb-1">Target resources <span className="text-slate-500">(comma-separated)</span></label>
                        <input
                          type="text"
                          value={planTargets}
                          onChange={e => setPlanTargets(e.target.value)}
                          placeholder="e.g. aws_instance.web, aws_s3_bucket.data"
                          className="w-full px-3 py-2 bg-slate-900 border border-slate-600 rounded-lg text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-brand-500 font-mono"
                        />
                      </div>
                      <div>
                        <label className="block text-xs text-slate-400 mb-1">Replace resources <span className="text-slate-500">(comma-separated)</span></label>
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
                        Plan Only
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
                        Refresh Only
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
                        Skip Refresh
                      </label>
                      {!vcsRef && (
                        <label className="flex items-center gap-2 text-sm text-slate-300 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={planAllowEmpty}
                            onChange={e => setPlanAllowEmpty(e.target.checked)}
                            className="rounded border-slate-600 bg-slate-900 text-brand-500 focus:ring-brand-500"
                          />
                          Allow Empty Apply
                        </label>
                      )}
                    </div>
                    {attrs['vcs-repo-url'] && (
                      <div className="mt-4 pt-3 border-t border-slate-700/50">
                        <label className="block text-xs text-slate-400 mb-2">VCS Ref</label>
                        <div className="flex gap-2">
                          <select
                            value={vcsRefType}
                            onChange={e => {
                              setVcsRefType(e.target.value as 'branch' | 'tag')
                              setVcsRef('')
                            }}
                            className="px-2 py-2 bg-slate-900 border border-slate-600 rounded-lg text-sm text-slate-200 focus:outline-none focus:ring-2 focus:ring-brand-500"
                          >
                            <option value="branch">Branch</option>
                            <option value="tag">Tag</option>
                          </select>
                          <select
                            value={vcsRef}
                            onChange={e => setVcsRef(e.target.value)}
                            disabled={vcsRefsLoading}
                            className="flex-1 px-2 py-2 bg-slate-900 border border-slate-600 rounded-lg text-sm text-slate-200 focus:outline-none focus:ring-2 focus:ring-brand-500 disabled:opacity-50"
                          >
                            <option value="">
                              {vcsRefsLoading
                                ? 'Loading...'
                                : `Default${vcsDefaultBranch ? ` (${vcsDefaultBranch})` : ''}`}
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
                            Non-default ref selected — run will be plan-only
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
              <EmptyState message="No runs yet for this workspace." />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <SortableHeader label="Run ID" sortKey="id" sortState={runSortState} onSort={toggleRunSort} />
                      <SortableHeader label="Status" sortKey="status" sortState={runSortState} onSort={toggleRunSort} />
                      <SortableHeader label="Type" sortKey="type" sortState={runSortState} onSort={toggleRunSort} className="hidden sm:table-cell" />
                      <th className="text-left px-4 py-2 text-xs font-medium text-slate-400 uppercase tracking-wider hidden md:table-cell">Changes</th>
                      <SortableHeader label="Source" sortKey="source" sortState={runSortState} onSort={toggleRunSort} className="hidden sm:table-cell" />
                      <SortableHeader label="Triggered By" sortKey="created-by" sortState={runSortState} onSort={toggleRunSort} className="hidden lg:table-cell" />
                      <SortableHeader label="Created" sortKey="created-at" sortState={runSortState} onSort={toggleRunSort} className="hidden md:table-cell" />
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
                              needs confirm
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
                              destroy
                            </span>
                          ) : run.attributes['plan-only'] ? (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-cyan-900/50 text-cyan-300">
                              plan only
                            </span>
                          ) : (
                            <span className="text-xs text-slate-500">plan + apply</span>
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
                            <span className="text-purple-400">module test</span>
                          ) : run.attributes.source === 'module-publish' ? (
                            <span className="text-purple-400">module publish</span>
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
                    ? `Delete state version #${confirmStateAction.sv.attributes.serial}? This cannot be undone.`
                    : `Rollback to state version #${confirmStateAction.sv.attributes.serial}? A new version will be created with the same content.`}
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
                            const err = await resp.json().catch(() => ({ detail: 'Failed' }))
                            throw new Error(err.detail || 'Failed to delete state version')
                          }
                        } else {
                          const resp = await apiFetch(`/api/terrapod/v1/state-versions/${sv.id}/actions/rollback`, { method: 'POST' })
                          if (!resp.ok) {
                            const err = await resp.json().catch(() => ({ detail: 'Failed' }))
                            throw new Error(err.detail || 'Failed to rollback state version')
                          }
                        }
                        setConfirmStateAction(null)
                        loadStateVersions()
                      } catch (err) {
                        setError(err instanceof Error ? err.message : 'State action failed')
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
                    {stateActionLoading ? 'Processing...' : confirmStateAction.action === 'delete' ? 'Confirm Delete' : 'Confirm Rollback'}
                  </button>
                  <button
                    onClick={() => setConfirmStateAction(null)}
                    className="px-3 py-1.5 rounded text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-300 transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}

            {/* Upload state button */}
            {perms['can-create-state-versions'] && (
              <div className="flex justify-end mb-4">
                <label className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors cursor-pointer">
                  Upload State
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
                          const err = await resp.json().catch(() => ({ detail: 'Failed' }))
                          throw new Error(err.detail || 'Failed to upload state')
                        }
                        loadStateVersions()
                      } catch (err) {
                        setError(err instanceof Error ? err.message : 'Failed to upload state file')
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
              <EmptyState message="No state versions yet for this workspace." />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <SortableHeader label="Serial" sortKey="serial" sortState={stateSortState} onSort={toggleStateSort} />
                      <SortableHeader label="Created By" sortKey="created-by" sortState={stateSortState} onSort={toggleStateSort} className="hidden sm:table-cell" />
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden sm:table-cell">Run</th>
                      <SortableHeader label="Size" sortKey="size" sortState={stateSortState} onSort={toggleStateSort} className="hidden md:table-cell" />
                      <SortableHeader label="Created" sortKey="created-at" sortState={stateSortState} onSort={toggleStateSort} className="hidden lg:table-cell" />
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">Actions</th>
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
                          <td className="px-4 py-3 text-xs text-slate-400 hidden sm:table-cell">
                            {sv.attributes['created-by'] || <span className="text-slate-500">runner</span>}
                          </td>
                          <td className="px-4 py-3 text-xs hidden sm:table-cell">
                            {runData ? (
                              <a href={`/workspaces/${workspaceId}/runs/${runData.id}`} className="text-brand-400 hover:text-brand-300">
                                {runData.id.replace('run-', '').slice(0, 8)}
                              </a>
                            ) : (
                              <span className="text-slate-500">-</span>
                            )}
                          </td>
                          <td className="px-4 py-3 text-xs text-slate-400 hidden md:table-cell">
                            {sv.attributes.size > 0 ? `${(sv.attributes.size / 1024).toFixed(1)} KB` : '-'}
                          </td>
                          <td className="px-4 py-3 text-xs text-slate-500 hidden lg:table-cell">
                            {sv.attributes['created-at'] ? new Date(sv.attributes['created-at']).toLocaleString() : ''}
                          </td>
                          <td className="px-4 py-3 text-right space-x-2">
                            <button
                              onClick={async () => {
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
                                  alert('Failed to download state file')
                                }
                              }}
                              className="text-xs text-brand-400 hover:text-brand-300"
                            >
                              Download
                            </button>
                            {!isLatest && perms['can-create-state-versions'] && (
                              <button
                                onClick={() => setConfirmStateAction({ action: 'rollback', sv })}
                                className="text-xs text-amber-400 hover:text-amber-300"
                              >
                                Rollback
                              </button>
                            )}
                            {!isLatest && perms['can-update'] && (
                              <button
                                onClick={() => setConfirmStateAction({ action: 'delete', sv })}
                                className="text-xs text-red-400 hover:text-red-300"
                              >
                                Delete
                              </button>
                            )}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {/* Configurations Tab */}
        {activeTab === 'configurations' && (
          <div>
            <div className="flex items-center justify-between mb-4">
              <p className="text-sm text-slate-400">
                Uploaded source archives for this workspace, newest first. Pick two rows to compare.
              </p>
              <div className="flex items-center gap-3">
                <span className="text-xs text-slate-500">{cvSelected.size}/2 selected</span>
                <button
                  type="button"
                  onClick={compareSelectedCvs}
                  disabled={cvSelected.size !== 2 || cvDiffLoading}
                  className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {cvDiffLoading ? 'Comparing…' : 'Compare'}
                </button>
              </div>
            </div>

            {cvLoading ? (
              <LoadingSpinner />
            ) : cvs.length === 0 ? (
              <EmptyState message="No configuration versions yet. They appear here as soon as a `terraform plan` uploads or a VCS push triggers a run." />
            ) : (
              <div className="overflow-hidden rounded-xl border border-slate-800">
                <table className="w-full text-sm">
                  <thead className="bg-slate-900/50 text-slate-400">
                    <tr>
                      <th className="w-8 px-2 py-3" aria-hidden />
                      <th className="px-4 py-3 text-left font-medium">ID</th>
                      <th className="px-4 py-3 text-left font-medium">Source</th>
                      <th className="px-4 py-3 text-left font-medium">Status</th>
                      <th className="px-4 py-3 text-left font-medium">Created</th>
                      <th className="px-4 py-3 text-right font-medium">Download</th>
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
                              aria-label={`Select ${cv.id} for compare`}
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
                                current
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
                                Download
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
                    Diff <span className="text-slate-500 text-sm font-normal">{cvDiff['total-files-changed']} files changed</span>
                  </h3>
                  <button
                    type="button"
                    onClick={() => { setCvDiff(null); setCvSelected(new Set()) }}
                    className="text-sm text-slate-400 hover:text-slate-200"
                  >
                    Close
                  </button>
                </div>

                {cvDiff.oversized.length > 0 && (
                  <div className="rounded-md border border-amber-900/50 bg-amber-900/20 px-4 py-3 text-sm text-amber-200">
                    Skipped {cvDiff.oversized.length} oversized file
                    {cvDiff.oversized.length > 1 ? 's' : ''}: {cvDiff.oversized.join(', ')}
                  </div>
                )}

                {cvDiff.files.length === 0 ? (
                  <p className="text-slate-500 text-sm italic">
                    No content differences between these versions.
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
                          {f.type}
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
                          Binary file changed — diff not rendered.
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
                  {showAddNotif ? 'Cancel' : 'Add Notification'}
                </button>
              </div>
            )}

            {showAddNotif && (
              <form onSubmit={handleAddNotification} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="notif-name" className="block text-sm font-medium text-slate-300 mb-1">Name</label>
                    <input id="notif-name" type="text" value={notifName} onChange={(e) => setNotifName(e.target.value)} required placeholder="Deploy notifications"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="notif-type" className="block text-sm font-medium text-slate-300 mb-1">Destination Type</label>
                    <select id="notif-type" value={notifType} onChange={(e) => setNotifType(e.target.value as 'generic' | 'slack' | 'email')}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      <option value="generic">Generic Webhook</option>
                      <option value="slack">Slack</option>
                      <option value="email">Email</option>
                    </select>
                  </div>
                  {notifType !== 'email' && (
                    <div>
                      <label htmlFor="notif-url" className="block text-sm font-medium text-slate-300 mb-1">URL</label>
                      <input id="notif-url" type="url" value={notifUrl} onChange={(e) => setNotifUrl(e.target.value)} required
                        placeholder={notifType === 'slack' ? 'https://hooks.slack.com/services/...' : 'https://example.com/webhook'}
                        className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                    </div>
                  )}
                  {notifType === 'generic' && (
                    <div>
                      <label htmlFor="notif-token" className="block text-sm font-medium text-slate-300 mb-1">HMAC Token (optional)</label>
                      <input id="notif-token" type="password" value={notifToken} onChange={(e) => setNotifToken(e.target.value)}
                        placeholder="Signing secret"
                        className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                    </div>
                  )}
                  {notifType === 'email' && (
                    <div className="sm:col-span-2">
                      <label htmlFor="notif-emails" className="block text-sm font-medium text-slate-300 mb-1">Email Addresses (comma-separated)</label>
                      <input id="notif-emails" type="text" value={notifEmails} onChange={(e) => setNotifEmails(e.target.value)} required
                        placeholder="team@example.com, ops@example.com"
                        className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                    </div>
                  )}
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-2">Trigger Events</label>
                  <div className="flex flex-wrap gap-2">
                    {ALL_TRIGGERS.map(t => (
                      <label key={t} className="flex items-center gap-1.5 cursor-pointer">
                        <input type="checkbox" checked={notifTriggers.has(t)} onChange={() => toggleTrigger(t)}
                          className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                        <span className="text-xs text-slate-300">{t}</span>
                      </label>
                    ))}
                  </div>
                </div>
                <button type="submit" disabled={addingNotif} className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {addingNotif ? 'Creating...' : 'Create Notification'}
                </button>
              </form>
            )}

            {notifLoading ? (
              <LoadingSpinner />
            ) : notifications.length === 0 ? (
              <EmptyState message="No notification configurations for this workspace." />
            ) : (
              <div className="space-y-3">
                {notifications.map((nc) => {
                  const a = nc.attributes
                  const responses = a['delivery-responses'] || []
                  const lastResponse = responses.length > 0 ? responses[responses.length - 1] : null
                  const isExpanded = expandedNotifId === nc.id

                  return (
                    <div key={nc.id} className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                      <div className="px-4 py-3 flex items-center gap-3">
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 mb-1">
                            <span className="text-sm font-medium text-slate-200 truncate">{a.name}</span>
                            <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${destTypeBadge(a['destination-type'])}`}>
                              {a['destination-type']}
                            </span>
                            <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                              a.enabled ? 'bg-green-900/50 text-green-300' : 'bg-slate-700 text-slate-400'
                            }`}>
                              {a.enabled ? 'Enabled' : 'Disabled'}
                            </span>
                          </div>
                          <div className="flex flex-wrap gap-1">
                            {a.triggers.map(t => (
                              <span key={t} className="inline-flex items-center px-1.5 py-0.5 rounded text-xs bg-slate-700 text-slate-300">{t}</span>
                            ))}
                          </div>
                        </div>
                        <div className="flex items-center gap-1.5 shrink-0">
                          {lastResponse && (
                            <span className={`text-xs ${lastResponse.success ? 'text-green-400' : 'text-red-400'}`}>
                              {lastResponse.success ? 'OK' : `Err ${lastResponse.status}`}
                            </span>
                          )}
                          {perms['can-update'] && (
                            <>
                              <button onClick={() => handleToggleNotif(nc)} className="text-xs text-brand-400 hover:text-brand-300 px-1">
                                {a.enabled ? 'Disable' : 'Enable'}
                              </button>
                              <button onClick={() => handleVerifyNotif(nc.id)} disabled={verifyingId === nc.id}
                                className="text-xs text-brand-400 hover:text-brand-300 px-1">
                                {verifyingId === nc.id ? 'Sending...' : 'Verify'}
                              </button>
                            </>
                          )}
                          {responses.length > 0 && (
                            <button onClick={() => setExpandedNotifId(isExpanded ? null : nc.id)}
                              className="text-xs text-slate-400 hover:text-slate-200 px-1">
                              {isExpanded ? 'Hide' : 'History'}
                            </button>
                          )}
                          {perms['can-update'] && (
                            deleteNotifId === nc.id ? (
                              <>
                                <button onClick={() => setDeleteNotifId(null)} className="text-xs text-slate-400 hover:text-slate-200 px-1">Cancel</button>
                                <button onClick={() => handleDeleteNotif(nc.id)} className="text-xs text-red-400 hover:text-red-300 px-1">Confirm</button>
                              </>
                            ) : (
                              <button onClick={() => setDeleteNotifId(nc.id)} className="text-xs text-red-400 hover:text-red-300 px-1">Delete</button>
                            )
                          )}
                        </div>
                      </div>
                      {isExpanded && responses.length > 0 && (
                        <div className="border-t border-slate-700/50 px-4 py-2">
                          <h4 className="text-xs font-medium text-slate-400 mb-2">Delivery History</h4>
                          <div className="space-y-1">
                            {[...responses].reverse().map((r, i) => (
                              <div key={i} className="flex items-center gap-3 text-xs">
                                <span className={r.success ? 'text-green-400' : 'text-red-400'}>
                                  {r.success ? 'OK' : 'FAIL'}
                                </span>
                                <span className="text-slate-400">HTTP {r.status}</span>
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
                  {showAddRunTask ? 'Cancel' : 'Add Run Task'}
                </button>
              </div>
            )}

            {showAddRunTask && (
              <form onSubmit={handleAddRunTask} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="rt-name" className="block text-sm font-medium text-slate-300 mb-1">Name</label>
                    <input id="rt-name" type="text" value={rtName} onChange={(e) => setRtName(e.target.value)} required placeholder="OPA Policy Check"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="rt-url" className="block text-sm font-medium text-slate-300 mb-1">Webhook URL</label>
                    <input id="rt-url" type="url" value={rtUrl} onChange={(e) => setRtUrl(e.target.value)} required
                      placeholder="https://opa.example.com/check"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="rt-stage" className="block text-sm font-medium text-slate-300 mb-1">Stage</label>
                    <select id="rt-stage" value={rtStage} onChange={(e) => setRtStage(e.target.value)}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      {ALL_STAGES.map(s => (
                        <option key={s} value={s}>{s.replace('_', ' ')}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label htmlFor="rt-enforcement" className="block text-sm font-medium text-slate-300 mb-1">Enforcement Level</label>
                    <select id="rt-enforcement" value={rtEnforcement} onChange={(e) => setRtEnforcement(e.target.value)}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      {ALL_ENFORCEMENT_LEVELS.map(l => (
                        <option key={l} value={l}>{l}</option>
                      ))}
                    </select>
                  </div>
                  <div className="sm:col-span-2">
                    <label htmlFor="rt-hmac" className="block text-sm font-medium text-slate-300 mb-1">HMAC Key (optional)</label>
                    <input id="rt-hmac" type="password" value={rtHmacKey} onChange={(e) => setRtHmacKey(e.target.value)}
                      placeholder="Signing secret for webhook verification"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                </div>
                <button type="submit" disabled={addingRunTask} className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {addingRunTask ? 'Creating...' : 'Create Run Task'}
                </button>
              </form>
            )}

            {runTasksLoading ? (
              <LoadingSpinner />
            ) : runTasks.length === 0 ? (
              <EmptyState message="No run tasks configured for this workspace." />
            ) : (
              <div className="space-y-3">
                {runTasks.map((rt) => {
                  const a = rt.attributes
                  return (
                    <div key={rt.id} className="bg-slate-800/50 rounded-lg border border-slate-700/50 px-4 py-3 flex items-center gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          <span className="text-sm font-medium text-slate-200 truncate">{a.name}</span>
                          <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${stageBadge(a.stage)}`}>
                            {a.stage.replace('_', ' ')}
                          </span>
                          <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${enforcementBadge(a['enforcement-level'])}`}>
                            {a['enforcement-level']}
                          </span>
                          <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                            a.enabled ? 'bg-green-900/50 text-green-300' : 'bg-slate-700 text-slate-400'
                          }`}>
                            {a.enabled ? 'Enabled' : 'Disabled'}
                          </span>
                        </div>
                        <div className="text-xs text-slate-500 truncate">{a.url}</div>
                      </div>
                      {perms['can-update'] && (
                        <div className="flex items-center gap-1.5 shrink-0">
                          <button onClick={() => handleToggleRunTask(rt)} className="text-xs text-brand-400 hover:text-brand-300 px-1">
                            {a.enabled ? 'Disable' : 'Enable'}
                          </button>
                          {deleteRtId === rt.id ? (
                            <>
                              <button onClick={() => setDeleteRtId(null)} className="text-xs text-slate-400 hover:text-slate-200 px-1">Cancel</button>
                              <button onClick={() => handleDeleteRunTask(rt.id)} className="text-xs text-red-400 hover:text-red-300 px-1">Confirm</button>
                            </>
                          ) : (
                            <button onClick={() => setDeleteRtId(rt.id)} className="text-xs text-red-400 hover:text-red-300 px-1">Delete</button>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )}

        {/* Sharing Tab — cross-workspace remote-state allowlist (#344, #349) */}
        {activeTab === 'sharing' && (
          <div>
            <div className="flex items-baseline justify-between mb-1">
              <h3 className="text-lg font-semibold text-slate-200">Remote State Sharing</h3>
              {rscLoading && <span className="text-xs text-slate-500">loading…</span>}
            </div>
            <p className="text-xs text-slate-500 mb-6">
              Producer-controlled allowlist for cross-workspace{' '}
              <code className="text-slate-400">terraform_remote_state</code>. State data is secret-bearing —
              grant deliberately.{' '}
              <a href="https://github.com/mattrobinsonsre/terrapod/blob/main/docs/remote-state.md" className="text-brand-400 hover:text-brand-300 underline" target="_blank" rel="noopener noreferrer">
                Docs
              </a>
            </p>

            {/* Outbound — workspaces I share my state to */}
            <div className="mb-8">
              <h4 className="text-sm font-medium text-slate-300 mb-2">Workspaces authorized to read this workspace&apos;s state</h4>
              {rscOutbound.length === 0 ? (
                <p className="text-sm text-slate-500 italic">This workspace&apos;s state is not shared with any other workspace.</p>
              ) : (
                <ul className="space-y-1">
                  {rscOutbound.map((e) => (
                    <li key={e.id} className="flex items-center justify-between gap-3 rounded bg-slate-800/40 px-3 py-2 text-sm">
                      <div>
                        <a href={`/workspaces/${e.consumerId}`} className="text-brand-400 hover:text-brand-300 font-medium">
                          {e.consumerName || e.consumerId}
                        </a>
                        {e.createdBy && (
                          <span className="ml-2 text-xs text-slate-500">granted by {e.createdBy}</span>
                        )}
                      </div>
                      {perms['can-update'] && (
                        <button
                          type="button"
                          onClick={() => revokeRemoteStateConsumer(e.id)}
                          className="rounded px-2 py-1 text-xs font-medium bg-red-900/40 text-red-200 hover:bg-red-900/60"
                        >
                          Revoke
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              )}

              {perms['can-update'] && (
                <form
                  onSubmit={(e) => { e.preventDefault(); addRemoteStateConsumer() }}
                  className="mt-3 flex items-center gap-2"
                >
                  <input
                    type="text"
                    value={rscAddName}
                    onChange={(e) => setRscAddName(e.target.value)}
                    placeholder="Authorize consumer workspace by name"
                    className="flex-1 rounded border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-200 placeholder-slate-500"
                    disabled={rscAdding}
                  />
                  <button
                    type="submit"
                    disabled={rscAdding || !rscAddName.trim()}
                    className="rounded bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-500 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {rscAdding ? 'Authorizing…' : 'Authorize'}
                  </button>
                </form>
              )}
            </div>

            {/* Inbound — workspaces I read state from */}
            <div>
              <h4 className="text-sm font-medium text-slate-300 mb-2">Workspaces this workspace is authorized to read state from</h4>
              {rscInbound.length === 0 ? (
                <p className="text-sm text-slate-500 italic">This workspace is not authorized to read state from any other workspace via terraform_remote_state.</p>
              ) : (
                <ul className="space-y-1">
                  {rscInbound.map((e) => (
                    <li key={e.id} className="rounded bg-slate-800/40 px-3 py-2 text-sm">
                      <a href={`/workspaces/${e.producerId}`} className="text-brand-400 hover:text-brand-300 font-medium">
                        {e.producerName || e.producerId}
                      </a>
                      <span className="ml-2 text-xs text-slate-500">(producer; revoke from there)</span>
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
