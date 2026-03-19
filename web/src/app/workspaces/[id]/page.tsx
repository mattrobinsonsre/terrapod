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
  'vcs-repo-url': string
  'vcs-branch': string
  'vcs-connection-id': string | null
  'vcs-connection-name': string | null
  'drift-detection-enabled': boolean
  'drift-detection-interval-seconds': number
  'drift-last-checked-at': string
  'drift-status': string
  'state-diverged': boolean
  'created-at': string
  'updated-at': string
  permissions: WorkspacePermissions
}

interface AgentPool {
  id: string
  attributes: { name: string }
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
    'created-at': string
    'plan-started-at': string | null
    'apply-finished-at': string | null
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
  }
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

type Tab = 'overview' | 'variables' | 'runs' | 'state' | 'notifications' | 'run-tasks'

const VALID_TABS: Set<string> = new Set(['overview', 'variables', 'runs', 'state', 'notifications', 'run-tasks'])

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
  const [editVcsConnectionId, setEditVcsConnectionId] = useState<string | null>(null)
  const [editVcsRepoUrl, setEditVcsRepoUrl] = useState('')
  const [editVcsBranch, setEditVcsBranch] = useState('')
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

  // Delete confirmation
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [deleting, setDeleting] = useState(false)

  // Label lockout warning
  const [lockoutWarning, setLockoutWarning] = useState('')

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
  type RunSortKey = 'id' | 'status' | 'type' | 'source' | 'created-at'
  const { sortedItems: sortedRuns, sortState: runSortState, toggleSort: toggleRunSort } = useSortable<RunItem, RunSortKey>(
    runs, 'created-at', 'desc',
    useCallback((item: RunItem, key: RunSortKey) => {
      switch (key) {
        case 'id': return item.id
        case 'status': return item.attributes.status
        case 'type': return item.attributes['plan-only'] ? 'plan only' : 'plan + apply'
        case 'source': return item.attributes.source
        case 'created-at': return item.attributes['created-at']
      }
    }, []),
  )

  // Sorting for state tab
  type StateSortKey = 'serial' | 'lineage' | 'size' | 'created-at'
  const { sortedItems: sortedState, sortState: stateSortState, toggleSort: toggleStateSort } = useSortable<StateVersionItem, StateSortKey>(
    stateVersions, 'serial', 'desc',
    useCallback((item: StateVersionItem, key: StateSortKey) => {
      switch (key) {
        case 'serial': return item.attributes.serial
        case 'lineage': return item.attributes.lineage
        case 'size': return item.attributes.size
        case 'created-at': return item.attributes['created-at']
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
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/vcs-refs`)
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

  async function loadNotifications() {
    setNotifLoading(true)
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/notification-configurations`)
      if (!res.ok) throw new Error('Failed to load notifications')
      const data = await res.json()
      setNotifications(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load notifications')
    } finally {
      setNotifLoading(false)
    }
  }

  async function loadRunTasks() {
    setRunTasksLoading(true)
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/run-tasks`)
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

      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/run-tasks`, {
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
      const res = await apiFetch(`/api/v2/run-tasks/${rt.id}`, {
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
      const res = await apiFetch(`/api/v2/run-tasks/${rtId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete')
      setDeleteRtId(null)
      await loadRunTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete run task')
    }
  }

  function startEditing() {
    if (!workspace) return
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
    setEditVcsConnectionId(workspace.attributes['vcs-connection-id'] || null)
    setEditVcsRepoUrl(workspace.attributes['vcs-repo-url'] || '')
    setEditVcsBranch(workspace.attributes['vcs-branch'] || '')
    setEditing(true)
    if (!poolsLoaded) {
      apiFetch('/api/v2/organizations/default/agent-pools').then(res => res.ok ? res.json() : { data: [] }).then(data => {
        setAgentPools(data.data || [])
        setPoolsLoaded(true)
      }).catch(() => {})
    }
    if (!vcsConnectionsLoaded) {
      apiFetch('/api/v2/organizations/default/vcs-connections').then(res => res.ok ? res.json() : { data: [] }).then(data => {
        setVcsConnections(data.data || [])
        setVcsConnectionsLoaded(true)
      }).catch(() => {})
    }
    const backend = workspace.attributes['execution-backend'] || 'tofu'
    if (versionsBackend !== backend) {
      apiFetch(`/api/v2/binary-cache/versions?tool=${backend}`)
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
              'resource-cpu': editCpu,
              'resource-memory': editMemory,
              'auto-apply': editAutoApply,
              'execution-mode': editExecMode,
              'execution-backend': editBackend,
              'terraform-version': editVersion,
              'agent-pool-id': editPoolId,
              'var-files': editVarFiles,
              'vcs-repo-url': editVcsRepoUrl,
              'vcs-branch': editVcsBranch,
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
      setWorkspace(data.data)
      setEditing(false)
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

  async function handleDelete() {
    setDeleting(true)
    try {
      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}`, { method: 'DELETE' })
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

      const res = await apiFetch(`/api/v2/workspaces/${workspaceId}/notification-configurations`, {
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
      const res = await apiFetch(`/api/v2/notification-configurations/${nc.id}`, {
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
      const res = await apiFetch(`/api/v2/notification-configurations/${ncId}`, { method: 'DELETE' })
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
      const res = await apiFetch(`/api/v2/notification-configurations/${ncId}/actions/verify`, { method: 'POST' })
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
    { key: 'notifications', label: 'Notifications' },
    { key: 'run-tasks', label: 'Run Tasks' },
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
            {attrs['state-diverged'] && (
              <div className="rounded-lg border border-red-500/50 bg-red-500/10 p-4 flex items-start gap-3">
                <svg className="w-5 h-5 text-red-400 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z" /></svg>
                <div>
                  <p className="text-sm font-medium text-red-300">State may be diverged</p>
                  <p className="text-xs text-red-400/80 mt-1">The last apply completed but the state upload failed. The actual infrastructure may not match the stored state. Upload the correct state or run a new apply to resolve this.</p>
                </div>
              </div>
            )}
            {attrs['execution-mode'] === 'remote' && !attrs['agent-pool-id'] && (
              <div className="rounded-lg border border-amber-500/50 bg-amber-500/10 p-4 flex items-start gap-3">
                <svg className="w-5 h-5 text-amber-400 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z" /></svg>
                <div>
                  <p className="text-sm font-medium text-amber-300">No agent pool assigned</p>
                  <p className="text-xs text-amber-400/80 mt-1">This workspace is in remote execution mode but has no agent pool. Runs will be queued indefinitely because no runner can claim them. Assign an agent pool in the settings below.</p>
                </div>
              </div>
            )}
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
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <dt className="text-xs text-slate-500">Execution Mode</dt>
                  {editing ? (
                    <select value={editExecMode} onChange={(e) => setEditExecMode(e.target.value)} className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500">
                      <option value="local">Local</option>
                      <option value="remote">Remote</option>
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
                    <input type="text" value={editCpu} onChange={(e) => setEditCpu(e.target.value)} className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                  ) : (
                    <dd className="mt-1 text-sm text-slate-200">{attrs['resource-cpu']}</dd>
                  )}
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Memory Request</dt>
                  {editing ? (
                    <input type="text" value={editMemory} onChange={(e) => setEditMemory(e.target.value)} className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
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
                      <input type="text" list="edit-version-suggestions" value={editVersion} onChange={(e) => setEditVersion(e.target.value)} placeholder="e.g. 1.9 or 1.9.8" className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
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
                  <dd className="mt-1 text-sm text-slate-200">{attrs['working-directory'] || '/'}</dd>
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
                    <input type="text" value={editVcsRepoUrl} onChange={(e) => setEditVcsRepoUrl(e.target.value)} placeholder="https://github.com/org/repo" className="mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
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
                <div className="sm:col-span-2">
                  <dt className="text-xs text-slate-500 mb-1">Labels</dt>
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
                  <dd className="mt-1">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${driftStatusBadge(attrs['drift-status']).cls}`}>
                      {driftStatusBadge(attrs['drift-status']).label}
                    </span>
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
                      <SortableHeader label="Source" sortKey="source" sortState={runSortState} onSort={toggleRunSort} className="hidden sm:table-cell" />
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
                          <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${statusColor(run.attributes.status)}`}>
                            {run.attributes.status}
                          </span>
                        </td>
                        <td className="px-4 py-3 hidden sm:table-cell">
                          {run.attributes['plan-only'] ? (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-cyan-900/50 text-cyan-300">
                              plan only
                            </span>
                          ) : (
                            <span className="text-xs text-slate-500">plan + apply</span>
                          )}
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-400 hidden sm:table-cell">
                          {run.attributes.source === 'module-test' ? (
                            <span className="text-purple-400">module test</span>
                          ) : run.attributes.source === 'module-publish' ? (
                            <span className="text-purple-400">module publish</span>
                          ) : run.attributes.source}
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
                      <SortableHeader label="Lineage" sortKey="lineage" sortState={stateSortState} onSort={toggleStateSort} className="hidden sm:table-cell" />
                      <SortableHeader label="Size" sortKey="size" sortState={stateSortState} onSort={toggleStateSort} className="hidden md:table-cell" />
                      <SortableHeader label="Created" sortKey="created-at" sortState={stateSortState} onSort={toggleStateSort} className="hidden lg:table-cell" />
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {sortedState.map((sv) => (
                      <tr key={sv.id} className="hover:bg-slate-700/20 transition-colors">
                        <td className="px-4 py-3 text-sm text-slate-200 font-mono">#{sv.attributes.serial}</td>
                        <td className="px-4 py-3 text-xs text-slate-400 font-mono hidden sm:table-cell">{sv.attributes.lineage || '-'}</td>
                        <td className="px-4 py-3 text-xs text-slate-400 hidden md:table-cell">
                          {sv.attributes.size > 0 ? `${(sv.attributes.size / 1024).toFixed(1)} KB` : '-'}
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-500 hidden lg:table-cell">
                          {sv.attributes['created-at'] ? new Date(sv.attributes['created-at']).toLocaleString() : ''}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <a
                            href={`/api/v2/state-versions/${sv.id}/download`}
                            className="text-xs text-brand-400 hover:text-brand-300"
                          >
                            Download
                          </a>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
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
      </main>
    </>
  )
}
