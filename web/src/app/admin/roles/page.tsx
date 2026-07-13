'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { useSortable } from '@/lib/use-sortable'
import { useConfirm } from '@/lib/use-confirm'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { useFormat } from '@/lib/format'
import { usePollingInterval } from '@/lib/use-polling-interval'

interface Role {
  name: string
  type: string
  attributes: {
    description: string
    'built-in': boolean
    'workspace-permission': string | null
    'pool-permission': string | null
    'registry-permission': string | null
    'catalog-permission': string | null
    capabilities?: string[]
    'allow-labels': Record<string, string>
    'allow-names': string[]
    'deny-labels': Record<string, string>
    'deny-names': string[]
  }
}

// ── Capability catalog (#585) ────────────────────────────────────────────────
// The grantable capability tokens, grouped by resource (the part before ':').
// Mirrors the server catalog in services/terrapod/auth/capabilities.py /
// docs/rbac-capabilities.md — the four label-scoped axes (workspace/pool/
// registry/catalog). platform:* tokens are NOT grantable and are excluded.
// Ordering within a group follows the read→plan→write→admin tiering for
// readability; the actual grant is an arbitrary subset of these.
const CAPABILITY_GROUPS: { resource: string; label: string; caps: string[] }[] = [
  {
    resource: 'workspace',
    label: 'Workspace',
    caps: ['workspace:read', 'workspace:lock', 'workspace:settings', 'workspace:force-unlock', 'workspace:delete'],
  },
  {
    resource: 'run',
    label: 'Run',
    caps: ['run:read', 'run:plan', 'run:cancel', 'run:apply', 'run:apply-destroy'],
  },
  {
    resource: 'state',
    label: 'State',
    caps: ['state:read-metadata', 'state:read', 'state:write', 'state:delete'],
  },
  { resource: 'var', label: 'Variables', caps: ['var:read', 'var:write'] },
  { resource: 'config', label: 'Config', caps: ['config:read', 'config:upload'] },
  { resource: 'drift', label: 'Drift', caps: ['drift:dismiss'] },
  { resource: 'run-task', label: 'Run tasks', caps: ['run-task:read', 'run-task:manage'] },
  { resource: 'notification', label: 'Notifications', caps: ['notification:read', 'notification:manage'] },
  { resource: 'run-trigger', label: 'Run triggers', caps: ['run-trigger:read', 'run-trigger:manage'] },
  { resource: 'pool', label: 'Agent pools', caps: ['pool:read', 'pool:assign', 'pool:manage'] },
  { resource: 'registry', label: 'Registry', caps: ['registry:read', 'registry:write', 'registry:admin'] },
  { resource: 'catalog', label: 'Service catalog', caps: ['catalog:read', 'catalog:use', 'catalog:admin'] },
]

// Every grantable token, flat — used to filter unknown/platform tokens out of a
// role's effective set before rendering the matrix, and to detect "extra"
// tokens the UI catalog doesn't know about (forward-compat).
const ALL_CAPABILITIES: string[] = CAPABILITY_GROUPS.flatMap((g) => g.caps)
const ALL_CAPABILITIES_SET = new Set(ALL_CAPABILITIES)

// ── Preset → capability expansion ────────────────────────────────────────────
// Mirrors _WORKSPACE_LEVELS / _POOL_LEVELS / _REGISTRY_LEVELS / _CATALOG_LEVELS
// (cumulative). Selecting the preset dropdowns pre-checks exactly these boxes;
// this is the same expansion the server applies when a role is level-authored.
const WORKSPACE_LEVELS: Record<string, string[]> = {
  read: ['workspace:read', 'run:read', 'state:read-metadata', 'var:read', 'config:read', 'run-task:read', 'notification:read', 'run-trigger:read'],
}
WORKSPACE_LEVELS.plan = [...WORKSPACE_LEVELS.read, 'run:plan', 'run:cancel', 'workspace:lock', 'state:read', 'drift:dismiss']
WORKSPACE_LEVELS.write = [...WORKSPACE_LEVELS.plan, 'run:apply', 'run:apply-destroy', 'var:write', 'state:write', 'config:upload']
WORKSPACE_LEVELS.admin = [...WORKSPACE_LEVELS.write, 'workspace:settings', 'workspace:force-unlock', 'workspace:delete', 'state:delete', 'notification:manage', 'run-task:manage', 'run-trigger:manage']

const POOL_LEVELS: Record<string, string[]> = {
  read: ['pool:read'],
  write: ['pool:read', 'pool:assign'],
  admin: ['pool:read', 'pool:assign', 'pool:manage'],
}
const REGISTRY_LEVELS: Record<string, string[]> = {
  read: ['registry:read'],
  write: ['registry:read', 'registry:write'],
  admin: ['registry:read', 'registry:write', 'registry:admin'],
}
const CATALOG_LEVELS: Record<string, string[]> = {
  none: [],
  read: ['catalog:read'],
  use: ['catalog:read', 'catalog:use'],
  admin: ['catalog:read', 'catalog:use', 'catalog:admin'],
}

// Expand the four preset dropdowns into the set of capabilities they grant
// (union across axes), matching the server's expand_preset().
function expandPresets(ws: string, pool: string, registry: string, catalog: string): Set<string> {
  return new Set<string>([
    ...(WORKSPACE_LEVELS[ws] || []),
    ...(POOL_LEVELS[pool] || []),
    ...(REGISTRY_LEVELS[registry] || []),
    ...(CATALOG_LEVELS[catalog] || []),
  ])
}

function setsEqual(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) return false
  for (const x of a) if (!b.has(x)) return false
  return true
}

// A grouped checkbox matrix over the grantable capability catalog. `selected`
// is the current grant; `onToggle` flips one token. `custom` surfaces whether
// the selection has diverged from the preset dropdowns. Any selected token not
// in the known catalog (forward-compat: a newer server capability) is shown as
// a read-only chip so it isn't silently dropped on save.
function CapabilityMatrix({
  selected,
  onToggle,
  custom,
  idPrefix,
}: {
  selected: Set<string>
  onToggle: (cap: string) => void
  custom: boolean
  idPrefix: string
}) {
  const t = useTranslations('adminRoles')
  const unknown = Array.from(selected).filter((c) => !ALL_CAPABILITIES_SET.has(c)).sort()
  return (
    <div className="mt-2 rounded-lg border border-slate-700/50 bg-slate-900/40 p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs text-slate-400">
          {t('caps.selected', { count: selected.size })}
        </span>
        {custom ? (
          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-purple-900/50 text-purple-300">{t('caps.custom')}</span>
        ) : (
          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-700 text-slate-400">{t('caps.matchesPresets')}</span>
        )}
      </div>
      <p className="text-xs text-slate-500 mb-3">
        {t('caps.presetHelp.before')}
        <span className="text-purple-300"> {t('caps.custom')}</span> {t('caps.presetHelp.after')}
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3">
        {CAPABILITY_GROUPS.map((group) => (
          <div key={group.resource}>
            <div className="text-xs font-medium text-slate-300 mb-1">{t(`caps.groups.${group.resource}`)}</div>
            <div className="space-y-1">
              {group.caps.map((cap) => {
                const verb = cap.split(':')[1]
                const id = `${idPrefix}-cap-${cap.replace(/[:]/g, '-')}`
                return (
                  <label key={cap} htmlFor={id} className="flex items-center gap-1.5 cursor-pointer">
                    <input
                      id={id}
                      type="checkbox"
                      checked={selected.has(cap)}
                      onChange={() => onToggle(cap)}
                      className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
                    />
                    <span className="text-xs text-slate-300 font-mono">{verb}</span>
                  </label>
                )
              })}
            </div>
          </div>
        ))}
      </div>
      {unknown.length > 0 && (
        <div className="mt-3 pt-3 border-t border-slate-700/50">
          <div className="text-xs text-slate-500 mb-1">{t('caps.otherPreserved')}</div>
          <div className="flex flex-wrap gap-1">
            {unknown.map((c) => (
              <span key={c} className="inline-flex items-center px-2 py-0.5 rounded text-xs font-mono bg-slate-700 text-slate-300">{c}</span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

interface RoleAssignment {
  id: string
  attributes: {
    'provider-name': string
    email: string
    'role-name': string
    'created-at': string
  }
}

interface Identity {
  'provider-name': string
  email: string
  'display-name': string | null
  roles: string[]
}

type Tab = 'roles' | 'assignments'

export default function RolesPage() {
  const t = useTranslations('adminRoles')
  const fmt = useFormat()
  const { confirmDelete } = useConfirm()
  const router = useRouter()
  const [activeTab, setActiveTab] = useState<Tab>('roles')

  // Roles
  const [roles, setRoles] = useState<Role[]>([])
  const [rolesLoading, setRolesLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  // Create role form
  const [showCreateRole, setShowCreateRole] = useState(false)
  const [roleName, setRoleName] = useState('')
  const [roleDesc, setRoleDesc] = useState('')
  const [rolePermission, setRolePermission] = useState('read')
  const [roleAllowLabels, setRoleAllowLabels] = useState('')
  const [roleAllowNames, setRoleAllowNames] = useState('')
  const [roleDenyLabels, setRoleDenyLabels] = useState('')
  const [rolePoolPermission, setRolePoolPermission] = useState('read')
  const [roleRegistryPermission, setRoleRegistryPermission] = useState('read')
  const [roleCatalogPermission, setRoleCatalogPermission] = useState('none')
  const [roleDenyNames, setRoleDenyNames] = useState('')
  const [creatingRole, setCreatingRole] = useState(false)
  // Capability authoring (create). `roleCaps` is the working matrix selection;
  // `roleCapsCustom` flips true once the user diverges from the preset expansion.
  const [showCreateCaps, setShowCreateCaps] = useState(false)
  const [roleCaps, setRoleCaps] = useState<Set<string>>(() => expandPresets('read', 'read', 'read', 'none'))
  const [roleCapsCustom, setRoleCapsCustom] = useState(false)

  // Edit role
  const [editingRole, setEditingRole] = useState<string | null>(null)
  const [editRoleDesc, setEditRoleDesc] = useState('')
  const [editRolePermission, setEditRolePermission] = useState('read')
  const [editRoleAllowLabels, setEditRoleAllowLabels] = useState('')
  const [editRoleAllowNames, setEditRoleAllowNames] = useState('')
  const [editRoleDenyLabels, setEditRoleDenyLabels] = useState('')
  const [editRolePoolPermission, setEditRolePoolPermission] = useState('read')
  const [editRoleRegistryPermission, setEditRoleRegistryPermission] = useState('read')
  const [editRoleCatalogPermission, setEditRoleCatalogPermission] = useState('none')
  const [editRoleDenyNames, setEditRoleDenyNames] = useState('')
  const [savingRole, setSavingRole] = useState(false)
  // Capability authoring (edit).
  const [showEditCaps, setShowEditCaps] = useState(false)
  const [editRoleCaps, setEditRoleCaps] = useState<Set<string>>(new Set())
  const [editRoleCapsCustom, setEditRoleCapsCustom] = useState(false)

  // Delete role

  // Display: which role rows have their capability list expanded
  const [expandedCaps, setExpandedCaps] = useState<Set<string>>(new Set())

  // Assignments
  const [assignments, setAssignments] = useState<RoleAssignment[]>([])
  const [assignmentsLoading, setAssignmentsLoading] = useState(false)
  const [identities, setIdentities] = useState<Identity[]>([])

  // Create assignment form
  const [showCreateAssignment, setShowCreateAssignment] = useState(false)
  const [assignProvider, setAssignProvider] = useState('local')
  const [assignEmail, setAssignEmail] = useState('')
  const [assignRoles, setAssignRoles] = useState<string[]>([])
  const [creatingAssignment, setCreatingAssignment] = useState(false)

  type AssignmentSortKey = 'provider' | 'email' | 'role' | 'created'
  const assignmentAccessor = useCallback((item: RoleAssignment, key: AssignmentSortKey) => {
    switch (key) {
      case 'provider': return item.attributes['provider-name']
      case 'email': return item.attributes.email
      case 'role': return item.attributes['role-name']
      case 'created': return item.attributes['created-at']
    }
  }, [])
  const { sortedItems: sortedAssignments, sortState: assignmentSortState, toggleSort: toggleAssignmentSort } = useSortable<RoleAssignment, AssignmentSortKey>(
    assignments, 'email', 'asc', assignmentAccessor,
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadRoles()
  }, [router])

  usePollingInterval(!rolesLoading, 60_000, loadRoles)

  useEffect(() => {
    if (activeTab === 'assignments') loadAssignments()
  }, [activeTab])

  async function loadRoles() {
    try {
      const res = await apiFetch('/api/terrapod/v1/roles')
      if (!res.ok) throw new Error(t('errors.loadRoles'))
      const data = await res.json()
      setRoles(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadRoles'))
    } finally {
      setRolesLoading(false)
    }
  }

  async function loadAssignments() {
    setAssignmentsLoading(true)
    try {
      const [assignRes, identRes] = await Promise.all([
        apiFetch('/api/terrapod/v1/role-assignments'),
        apiFetch('/api/terrapod/v1/role-assignments/identities'),
      ])
      if (!assignRes.ok) throw new Error(t('errors.loadAssignments'))
      const assignData = await assignRes.json()
      setAssignments(assignData.data || [])
      if (identRes.ok) {
        const identData = await identRes.json()
        setIdentities(identData.data || [])
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.loadAssignments'))
    } finally {
      setAssignmentsLoading(false)
    }
  }

  function parseLabels(s: string): Record<string, string> {
    const result: Record<string, string> = {}
    if (!s.trim()) return result
    s.split(',').forEach((pair) => {
      const [k, v] = pair.split('=').map((x) => x.trim())
      if (k) result[k] = v || ''
    })
    return result
  }

  function formatLabels(labels: Record<string, string>): string {
    return Object.entries(labels).map(([k, v]) => v ? `${k}=${v}` : k).join(', ')
  }

  // ── Create-form capability matrix helpers ─────────────────────────────────
  // Changing a preset dropdown re-derives the matrix from all four presets and
  // clears the "custom" flag (the presets are once again the source of truth).
  function setCreatePreset(axis: 'w' | 'p' | 'r' | 'c', value: string) {
    const w = axis === 'w' ? value : rolePermission
    const p = axis === 'p' ? value : rolePoolPermission
    const r = axis === 'r' ? value : roleRegistryPermission
    const c = axis === 'c' ? value : roleCatalogPermission
    if (axis === 'w') setRolePermission(value)
    if (axis === 'p') setRolePoolPermission(value)
    if (axis === 'r') setRoleRegistryPermission(value)
    if (axis === 'c') setRoleCatalogPermission(value)
    setRoleCaps(expandPresets(w, p, r, c))
    setRoleCapsCustom(false)
  }

  // Ticking/unticking a box updates the set and flips to "custom" if it no
  // longer matches the current preset expansion.
  function toggleCreateCap(cap: string) {
    const next = new Set(roleCaps)
    if (next.has(cap)) next.delete(cap)
    else next.add(cap)
    setRoleCaps(next)
    setRoleCapsCustom(!setsEqual(next, expandPresets(rolePermission, rolePoolPermission, roleRegistryPermission, roleCatalogPermission)))
  }

  // ── Edit-form capability matrix helpers ───────────────────────────────────
  function setEditPreset(axis: 'w' | 'p' | 'r' | 'c', value: string) {
    const w = axis === 'w' ? value : editRolePermission
    const p = axis === 'p' ? value : editRolePoolPermission
    const r = axis === 'r' ? value : editRoleRegistryPermission
    const c = axis === 'c' ? value : editRoleCatalogPermission
    if (axis === 'w') setEditRolePermission(value)
    if (axis === 'p') setEditRolePoolPermission(value)
    if (axis === 'r') setEditRoleRegistryPermission(value)
    if (axis === 'c') setEditRoleCatalogPermission(value)
    setEditRoleCaps(expandPresets(w, p, r, c))
    setEditRoleCapsCustom(false)
  }

  function toggleEditCap(cap: string) {
    const next = new Set(editRoleCaps)
    if (next.has(cap)) next.delete(cap)
    else next.add(cap)
    setEditRoleCaps(next)
    setEditRoleCapsCustom(!setsEqual(next, expandPresets(editRolePermission, editRolePoolPermission, editRoleRegistryPermission, editRoleCatalogPermission)))
  }

  function toggleExpandCaps(name: string) {
    setExpandedCaps((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  async function handleCreateRole(e: React.FormEvent) {
    e.preventDefault()
    setCreatingRole(true)
    setError('')
    setSuccess('')
    try {
      const attrs: Record<string, unknown> = {
        name: roleName,
        description: roleDesc,
      }
      // If the user customised the capability matrix, author the role by its
      // explicit capability set; otherwise send the four preset level fields.
      if (roleCapsCustom) {
        attrs['capabilities'] = Array.from(roleCaps).sort()
      } else {
        attrs['workspace-permission'] = rolePermission
        attrs['pool-permission'] = rolePoolPermission
        attrs['registry-permission'] = roleRegistryPermission
        attrs['catalog-permission'] = roleCatalogPermission
      }
      if (roleAllowLabels.trim()) attrs['allow-labels'] = parseLabels(roleAllowLabels)
      if (roleAllowNames.trim()) attrs['allow-names'] = roleAllowNames.split(',').map((s) => s.trim()).filter(Boolean)
      if (roleDenyLabels.trim()) attrs['deny-labels'] = parseLabels(roleDenyLabels)
      if (roleDenyNames.trim()) attrs['deny-names'] = roleDenyNames.split(',').map((s) => s.trim()).filter(Boolean)

      const res = await apiFetch('/api/terrapod/v1/roles', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'roles', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.createRoleStatus', { status: res.status }))
      }
      setSuccess(t('success.roleCreated', { name: roleName }))
      setRoleName('')
      setRoleDesc('')
      setRolePermission('read')
      setRolePoolPermission('read')
      setRoleRegistryPermission('read')
      setRoleCatalogPermission('none')
      setRoleAllowLabels('')
      setRoleAllowNames('')
      setRoleDenyLabels('')
      setRoleDenyNames('')
      setRoleCaps(expandPresets('read', 'read', 'read', 'none'))
      setRoleCapsCustom(false)
      setShowCreateCaps(false)
      setShowCreateRole(false)
      await loadRoles()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.createRole'))
    } finally {
      setCreatingRole(false)
    }
  }

  function startEditRole(role: Role) {
    const a = role.attributes
    setEditingRole(role.name)
    setEditRoleDesc(a.description || '')
    // The server may report a derived level of "custom" per axis when the role's
    // stored capabilities don't match any preset — coerce those to the floor
    // preset for the dropdown so it stays a valid <option>. The capability matrix
    // (seeded below) carries the true grant.
    const coerce = (v: string | null, floor: string) => (v && v !== 'custom' ? v : floor)
    const ws = coerce(a['workspace-permission'], 'read')
    const pool = coerce(a['pool-permission'], 'read')
    const registry = coerce(a['registry-permission'], 'read')
    const catalog = coerce(a['catalog-permission'], 'none')
    setEditRolePermission(ws)
    setEditRolePoolPermission(pool)
    setEditRoleRegistryPermission(registry)
    setEditRoleCatalogPermission(catalog)
    // Seed the matrix from the role's effective capabilities (restricted to the
    // known grantable catalog). If that set matches the preset expansion, the
    // role is preset-authored; otherwise it's custom (and editing keeps it so).
    const effective = new Set((a.capabilities || []).filter((c) => ALL_CAPABILITIES_SET.has(c)))
    const presetCaps = expandPresets(ws, pool, registry, catalog)
    const isCustom = a.capabilities !== undefined && !setsEqual(effective, presetCaps)
    setEditRoleCaps(isCustom ? effective : presetCaps)
    setEditRoleCapsCustom(isCustom)
    setShowEditCaps(isCustom)
    setEditRoleAllowLabels(formatLabels(a['allow-labels'] || {}))
    setEditRoleAllowNames((a['allow-names'] || []).join(', '))
    setEditRoleDenyLabels(formatLabels(a['deny-labels'] || {}))
    setEditRoleDenyNames((a['deny-names'] || []).join(', '))
  }

  async function handleSaveRole() {
    if (!editingRole) return
    setSavingRole(true)
    setError('')
    setSuccess('')
    try {
      const attrs: Record<string, unknown> = {
        description: editRoleDesc,
        'allow-labels': parseLabels(editRoleAllowLabels),
        'allow-names': editRoleAllowNames.split(',').map((s) => s.trim()).filter(Boolean),
        'deny-labels': parseLabels(editRoleDenyLabels),
        'deny-names': editRoleDenyNames.split(',').map((s) => s.trim()).filter(Boolean),
      }
      // Custom matrix → author by explicit capabilities (server derives the level
      // summary). Otherwise send the preset levels (server expands them). Sending
      // level fields on a previously capability-authored role reverts it to
      // preset-authoring, which is the intended behaviour of using the dropdowns.
      if (editRoleCapsCustom) {
        attrs['capabilities'] = Array.from(editRoleCaps).sort()
      } else {
        attrs['workspace-permission'] = editRolePermission
        attrs['pool-permission'] = editRolePoolPermission
        attrs['registry-permission'] = editRoleRegistryPermission
        attrs['catalog-permission'] = editRoleCatalogPermission
      }
      const res = await apiFetch(`/api/terrapod/v1/roles/${editingRole}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'roles', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.updateRole'))
      }
      setSuccess(t('success.roleUpdated', { name: editingRole }))
      setEditingRole(null)
      await loadRoles()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.updateRole'))
    } finally {
      setSavingRole(false)
    }
  }

  async function handleDeleteRole(name: string) {
    if (!confirmDelete(t('confirm.deleteRole', { name }))) return
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/roles/${name}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(t('errors.deleteRole'))
      setSuccess(t('success.roleDeleted', { name }))
      await loadRoles()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.deleteRole'))
    }
  }

  async function handleCreateAssignment(e: React.FormEvent) {
    e.preventDefault()
    setCreatingAssignment(true)
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch('/api/terrapod/v1/role-assignments', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'role-assignments',
            attributes: {
              'provider-name': assignProvider,
              email: assignEmail,
              roles: assignRoles,
            },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || t('errors.setAssignmentStatus', { status: res.status }))
      }
      setSuccess(t('success.rolesAssigned', { email: assignEmail }))
      setAssignEmail('')
      setAssignRoles([])
      setShowCreateAssignment(false)
      await loadAssignments()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.setAssignment'))
    } finally {
      setCreatingAssignment(false)
    }
  }

  async function handleDeleteAssignment(provider: string, email: string, roleName: string) {
    if (!confirmDelete(t('confirm.removeAssignment', { roleName, email }))) return
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/role-assignments/${encodeURIComponent(provider)}/${encodeURIComponent(email)}/${encodeURIComponent(roleName)}`, {
        method: 'DELETE',
      })
      if (!res.ok) throw new Error(t('errors.deleteAssignment'))
      setSuccess(t('success.assignmentRemoved'))
      await loadAssignments()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.deleteAssignment'))
    }
  }

  function toggleAssignRole(name: string) {
    setAssignRoles((prev) =>
      prev.includes(name) ? prev.filter((r) => r !== name) : [...prev, name]
    )
  }

  function permissionBadge(p: string | null) {
    switch (p) {
      case 'admin': return 'bg-red-900/50 text-red-300'
      case 'write': return 'bg-orange-900/50 text-orange-300'
      case 'use': return 'bg-orange-900/50 text-orange-300'
      case 'plan': return 'bg-blue-900/50 text-blue-300'
      case 'read': return 'bg-green-900/50 text-green-300'
      case 'custom': return 'bg-purple-900/50 text-purple-300'
      default: return 'bg-slate-700 text-slate-400'
    }
  }

  const tabs: { key: Tab; label: string }[] = [
    { key: 'roles', label: t('tabs.roles') },
    { key: 'assignments', label: t('tabs.assignments') },
  ]

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={t('title')}
          description={t('description')}
        />

        {error && <ErrorBanner message={error} />}
        {success && (
          <div className="mb-4 p-3 bg-green-900/30 text-green-400 rounded-lg text-sm border border-green-800/50">{success}</div>
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

        {/* Roles Tab */}
        {activeTab === 'roles' && (
          <div>
            <div className="flex justify-end mb-4">
              <button
                onClick={() => setShowCreateRole(!showCreateRole)}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors"
              >
                {showCreateRole ? t('actions.cancel') : t('actions.createRole')}
              </button>
            </div>

            {showCreateRole && (
              <form onSubmit={handleCreateRole} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
                  <div>
                    <label htmlFor="r-name" className="block text-sm font-medium text-slate-300 mb-1">{t('form.name')}</label>
                    <input id="r-name" type="text" value={roleName} onChange={(e) => setRoleName(e.target.value)} required placeholder="developer"
                      pattern="[a-zA-Z0-9][a-zA-Z0-9_\-]*"
                      title={t('form.namePattern')}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="r-perm" className="block text-sm font-medium text-slate-300 mb-1">{t('form.workspacePermission')}</label>
                    <select id="r-perm" value={rolePermission} onChange={(e) => setCreatePreset('w', e.target.value)}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      <option value="read">read</option>
                      <option value="plan">plan</option>
                      <option value="write">write</option>
                      <option value="admin">admin</option>
                    </select>
                  </div>
                  <div>
                    <label htmlFor="r-pool-perm" className="block text-sm font-medium text-slate-300 mb-1">{t('form.poolPermission')}</label>
                    <select id="r-pool-perm" value={rolePoolPermission} onChange={(e) => setCreatePreset('p', e.target.value)}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      <option value="read">read</option>
                      <option value="write">write</option>
                      <option value="admin">admin</option>
                    </select>
                  </div>
                  <div>
                    <label htmlFor="r-registry-perm" className="block text-sm font-medium text-slate-300 mb-1">{t('form.registryPermission')}</label>
                    <select id="r-registry-perm" value={roleRegistryPermission} onChange={(e) => setCreatePreset('r', e.target.value)}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      <option value="read">read</option>
                      <option value="write">write</option>
                      <option value="admin">admin</option>
                    </select>
                  </div>
                  <div>
                    <label htmlFor="r-catalog-perm" className="block text-sm font-medium text-slate-300 mb-1">{t('form.catalogPermission')}</label>
                    <select id="r-catalog-perm" value={roleCatalogPermission} onChange={(e) => setCreatePreset('c', e.target.value)}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      <option value="none">none</option>
                      <option value="read">read</option>
                      <option value="use">use</option>
                      <option value="admin">admin</option>
                    </select>
                  </div>
                </div>
                <div>
                  <button type="button" onClick={() => setShowCreateCaps(!showCreateCaps)}
                    className="text-xs text-brand-400 hover:text-brand-300">
                    {showCreateCaps ? '▾ ' : '▸ '}{t('caps.advanced')}
                    {roleCapsCustom && <span className="ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-purple-900/50 text-purple-300">{t('caps.custom')}</span>}
                  </button>
                  {showCreateCaps && (
                    <CapabilityMatrix selected={roleCaps} onToggle={toggleCreateCap} custom={roleCapsCustom} idPrefix="create" />
                  )}
                </div>
                <div>
                  <label htmlFor="r-desc" className="block text-sm font-medium text-slate-300 mb-1">{t('form.descriptionLabel')}</label>
                  <input id="r-desc" type="text" value={roleDesc} onChange={(e) => setRoleDesc(e.target.value)} placeholder={t('form.descriptionPlaceholder')}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="r-allow-labels" className="block text-sm font-medium text-slate-300 mb-1">{t('form.allowLabels')}</label>
                    <input id="r-allow-labels" type="text" value={roleAllowLabels} onChange={(e) => setRoleAllowLabels(e.target.value)} placeholder="env=dev, team=platform"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="r-allow-names" className="block text-sm font-medium text-slate-300 mb-1">{t('form.allowNames')}</label>
                    <input id="r-allow-names" type="text" value={roleAllowNames} onChange={(e) => setRoleAllowNames(e.target.value)} placeholder="ws-prod, ws-staging"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="r-deny-labels" className="block text-sm font-medium text-slate-300 mb-1">{t('form.denyLabels')}</label>
                    <input id="r-deny-labels" type="text" value={roleDenyLabels} onChange={(e) => setRoleDenyLabels(e.target.value)} placeholder="env=prod"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="r-deny-names" className="block text-sm font-medium text-slate-300 mb-1">{t('form.denyNames')}</label>
                    <input id="r-deny-names" type="text" value={roleDenyNames} onChange={(e) => setRoleDenyNames(e.target.value)} placeholder="ws-critical"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                </div>
                <button type="submit" disabled={creatingRole}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {creatingRole ? t('actions.creating') : t('actions.createRole')}
                </button>
              </form>
            )}

            {rolesLoading ? (
              <LoadingSpinner />
            ) : roles.length === 0 ? (
              <EmptyState message={t('emptyRoles')} />
            ) : (
              <div className="space-y-3">
                {roles.map((role) => {
                  const a = role.attributes
                  const isEditing = editingRole === role.name
                  return (
                    <div key={role.name} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4">
                      {isEditing ? (
                        <div className="space-y-3">
                          <div className="flex items-center justify-between">
                            <h3 className="text-sm font-medium text-slate-200">{role.name}</h3>
                            <div className="flex gap-2">
                              <button onClick={() => setEditingRole(null)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200">{t('actions.cancel')}</button>
                              <button onClick={handleSaveRole} disabled={savingRole} className="px-2.5 py-1 rounded-md text-xs font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white">
                                {savingRole ? t('actions.saving') : t('actions.save')}
                              </button>
                            </div>
                          </div>
                          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                            <div>
                              <label className="block text-xs text-slate-500 mb-1">{t('form.descriptionLabel')}</label>
                              <input type="text" value={editRoleDesc} onChange={(e) => setEditRoleDesc(e.target.value)}
                                className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                            </div>
                            <div>
                              <label className="block text-xs text-slate-500 mb-1">{t('form.workspacePermission')}</label>
                              <select value={editRolePermission} onChange={(e) => setEditPreset('w', e.target.value)}
                                className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500">
                                <option value="read">read</option>
                                <option value="plan">plan</option>
                                <option value="write">write</option>
                                <option value="admin">admin</option>
                              </select>
                            </div>
                            <div>
                              <label className="block text-xs text-slate-500 mb-1">{t('form.poolPermission')}</label>
                              <select value={editRolePoolPermission} onChange={(e) => setEditPreset('p', e.target.value)}
                                className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500">
                                <option value="read">read</option>
                                <option value="write">write</option>
                                <option value="admin">admin</option>
                              </select>
                            </div>
                            <div>
                              <label className="block text-xs text-slate-500 mb-1">{t('form.registryPermission')}</label>
                              <select value={editRoleRegistryPermission} onChange={(e) => setEditPreset('r', e.target.value)}
                                className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500">
                                <option value="read">read</option>
                                <option value="write">write</option>
                                <option value="admin">admin</option>
                              </select>
                            </div>
                            <div>
                              <label className="block text-xs text-slate-500 mb-1">{t('form.catalogPermission')}</label>
                              <select value={editRoleCatalogPermission} onChange={(e) => setEditPreset('c', e.target.value)}
                                className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500">
                                <option value="none">none</option>
                                <option value="read">read</option>
                                <option value="use">use</option>
                                <option value="admin">admin</option>
                              </select>
                            </div>
                            <div>
                              <label className="block text-xs text-slate-500 mb-1">{t('form.allowLabelsShort')}</label>
                              <input type="text" value={editRoleAllowLabels} onChange={(e) => setEditRoleAllowLabels(e.target.value)}
                                className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                            </div>
                            <div>
                              <label className="block text-xs text-slate-500 mb-1">{t('form.allowNamesShort')}</label>
                              <input type="text" value={editRoleAllowNames} onChange={(e) => setEditRoleAllowNames(e.target.value)}
                                className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                            </div>
                            <div>
                              <label className="block text-xs text-slate-500 mb-1">{t('form.denyLabelsShort')}</label>
                              <input type="text" value={editRoleDenyLabels} onChange={(e) => setEditRoleDenyLabels(e.target.value)}
                                className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                            </div>
                            <div>
                              <label className="block text-xs text-slate-500 mb-1">{t('form.denyNamesShort')}</label>
                              <input type="text" value={editRoleDenyNames} onChange={(e) => setEditRoleDenyNames(e.target.value)}
                                className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                            </div>
                          </div>
                          <div>
                            <button type="button" onClick={() => setShowEditCaps(!showEditCaps)}
                              className="text-xs text-brand-400 hover:text-brand-300">
                              {showEditCaps ? '▾ ' : '▸ '}{t('caps.advanced')}
                              {editRoleCapsCustom && <span className="ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-purple-900/50 text-purple-300">{t('caps.custom')}</span>}
                            </button>
                            {showEditCaps && (
                              <CapabilityMatrix selected={editRoleCaps} onToggle={toggleEditCap} custom={editRoleCapsCustom} idPrefix={`edit-${role.name}`} />
                            )}
                          </div>
                        </div>
                      ) : (
                        <div className="flex items-start justify-between">
                          <div>
                            <div className="flex items-center gap-2 mb-1">
                              <h3 className="text-sm font-medium text-slate-200">{role.name}</h3>
                              {a['built-in'] && (
                                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-700 text-slate-400">{t('builtIn')}</span>
                              )}
                              {a['workspace-permission'] && (
                                <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${permissionBadge(a['workspace-permission'])}`}>
                                  ws: {a['workspace-permission']}
                                </span>
                              )}
                              {a['pool-permission'] && a['pool-permission'] !== 'read' && (
                                <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${permissionBadge(a['pool-permission'])}`}>
                                  pool: {a['pool-permission']}
                                </span>
                              )}
                              {a['registry-permission'] && a['registry-permission'] !== 'read' && (
                                <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${permissionBadge(a['registry-permission'])}`}>
                                  registry: {a['registry-permission']}
                                </span>
                              )}
                              {a['catalog-permission'] && a['catalog-permission'] !== 'none' && (
                                <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${permissionBadge(a['catalog-permission'])}`}>
                                  catalog: {a['catalog-permission']}
                                </span>
                              )}
                            </div>
                            {a.description && <p className="text-xs text-slate-400 mb-2">{a.description}</p>}
                            {(a.capabilities || []).length > 0 && (
                              <div className="mb-2">
                                <button
                                  type="button"
                                  onClick={() => toggleExpandCaps(role.name)}
                                  className="text-xs text-slate-400 hover:text-slate-200"
                                >
                                  {expandedCaps.has(role.name) ? '▾ ' : '▸ '}
                                  {t('caps.count', { count: a.capabilities!.length })}
                                </button>
                                {expandedCaps.has(role.name) && (
                                  <div className="mt-1.5 flex flex-wrap gap-1">
                                    {[...a.capabilities!].sort().map((c) => (
                                      <span key={c} className="inline-flex items-center px-2 py-0.5 rounded text-xs font-mono bg-slate-700/70 text-slate-300">{c}</span>
                                    ))}
                                  </div>
                                )}
                              </div>
                            )}
                            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
                              {Object.keys(a['allow-labels'] || {}).length > 0 && (
                                <span>{t('display.allow', { value: formatLabels(a['allow-labels']) })}</span>
                              )}
                              {(a['allow-names'] || []).length > 0 && (
                                <span>{t('display.allowNames', { value: a['allow-names'].join(', ') })}</span>
                              )}
                              {Object.keys(a['deny-labels'] || {}).length > 0 && (
                                <span>{t('display.deny', { value: formatLabels(a['deny-labels']) })}</span>
                              )}
                              {(a['deny-names'] || []).length > 0 && (
                                <span>{t('display.denyNames', { value: a['deny-names'].join(', ') })}</span>
                              )}
                            </div>
                          </div>
                          {!a['built-in'] && (
                            <div className="flex gap-2 flex-shrink-0">
                              <button onClick={() => startEditRole(role)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200">{t('actions.edit')}</button>
                              <button onClick={() => handleDeleteRole(role.name)} className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300">{t('actions.delete')}</button>
                            </div>
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

        {/* Assignments Tab */}
        {activeTab === 'assignments' && (
          <div>
            <div className="flex justify-end mb-4">
              <button
                onClick={() => setShowCreateAssignment(!showCreateAssignment)}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors"
              >
                {showCreateAssignment ? t('actions.cancel') : t('actions.addAssignment')}
              </button>
            </div>

            {showCreateAssignment && (
              <form onSubmit={handleCreateAssignment} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="a-provider" className="block text-sm font-medium text-slate-300 mb-1">{t('assignForm.provider')}</label>
                    <select id="a-provider" value={assignProvider} onChange={(e) => { setAssignProvider(e.target.value); setAssignEmail('') }} required
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      {Array.from(new Set(identities.map((i) => i['provider-name']))).sort((a, b) => a === 'local' ? -1 : b === 'local' ? 1 : a.localeCompare(b)).map((p) => (
                        <option key={p} value={p}>{p}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label htmlFor="a-email" className="block text-sm font-medium text-slate-300 mb-1">{t('assignForm.email')}</label>
                    <input id="a-email" type="email" list="email-suggestions" value={assignEmail} onChange={(e) => setAssignEmail(e.target.value)} required
                      placeholder="user@example.com"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                    <datalist id="email-suggestions">
                      {identities.filter((i) => i['provider-name'] === assignProvider).map((i) => (
                        <option key={i.email} value={i.email}>{i['display-name'] ? `${i['display-name']} (${i.email})` : i.email}</option>
                      ))}
                    </datalist>
                  </div>
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-2">{t('assignForm.roles')}</label>
                  <div className="flex flex-wrap gap-2">
                    {roles.map((role) => (
                      <label key={role.name} className="flex items-center gap-1.5 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={assignRoles.includes(role.name)}
                          onChange={() => toggleAssignRole(role.name)}
                          className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
                        />
                        <span className="text-sm text-slate-300">{role.name}</span>
                      </label>
                    ))}
                  </div>
                </div>
                <button type="submit" disabled={creatingAssignment || assignRoles.length === 0}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {creatingAssignment ? t('actions.saving') : t('actions.setRoles')}
                </button>
              </form>
            )}

            {assignmentsLoading ? (
              <LoadingSpinner />
            ) : assignments.length === 0 ? (
              <EmptyState message={t('emptyAssignments')} />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <SortableHeader label={t('columns.provider')} sortKey="provider" sortState={assignmentSortState} onSort={toggleAssignmentSort} />
                      <SortableHeader label={t('columns.email')} sortKey="email" sortState={assignmentSortState} onSort={toggleAssignmentSort} />
                      <SortableHeader label={t('columns.role')} sortKey="role" sortState={assignmentSortState} onSort={toggleAssignmentSort} />
                      <SortableHeader label={t('columns.created')} sortKey="created" sortState={assignmentSortState} onSort={toggleAssignmentSort} className="hidden sm:table-cell" />
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">{t('columns.actions')}</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {sortedAssignments.map((a) => (
                      <tr key={`${a.attributes['provider-name']}:${a.attributes.email}:${a.attributes['role-name']}`} className="hover:bg-slate-700/20 transition-colors">
                        <td className="px-4 py-3 text-sm text-slate-400">{a.attributes['provider-name']}</td>
                        <td className="px-4 py-3 text-sm text-slate-200">{a.attributes.email}</td>
                        <td className="px-4 py-3">
                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-brand-900/50 text-brand-300">
                            {a.attributes['role-name']}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-500 hidden sm:table-cell">
                          {a.attributes['created-at'] ? fmt.date(a.attributes['created-at']) : ''}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <button
                            onClick={() => handleDeleteAssignment(a.attributes['provider-name'], a.attributes.email, a.attributes['role-name'])}
                            className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-900/40 hover:bg-red-900/60 text-red-300"
                          >
                            {t('actions.remove')}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </main>
    </>
  )
}
