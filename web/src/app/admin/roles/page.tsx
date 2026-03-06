'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { useSortable } from '@/lib/use-sortable'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

interface Role {
  name: string
  type: string
  attributes: {
    description: string
    'built-in': boolean
    'workspace-permission': string | null
    'allow-labels': Record<string, string>
    'allow-names': string[]
    'deny-labels': Record<string, string>
    'deny-names': string[]
  }
}

interface RoleAssignment {
  id: string
  attributes: {
    provider: string
    email: string
    'role-name': string
    'created-at': string
  }
}

type Tab = 'roles' | 'assignments'

export default function RolesPage() {
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
  const [roleDenyNames, setRoleDenyNames] = useState('')
  const [creatingRole, setCreatingRole] = useState(false)

  // Edit role
  const [editingRole, setEditingRole] = useState<string | null>(null)
  const [editRoleDesc, setEditRoleDesc] = useState('')
  const [editRolePermission, setEditRolePermission] = useState('read')
  const [editRoleAllowLabels, setEditRoleAllowLabels] = useState('')
  const [editRoleAllowNames, setEditRoleAllowNames] = useState('')
  const [editRoleDenyLabels, setEditRoleDenyLabels] = useState('')
  const [editRoleDenyNames, setEditRoleDenyNames] = useState('')
  const [savingRole, setSavingRole] = useState(false)

  // Delete role
  const [deleteRoleName, setDeleteRoleName] = useState<string | null>(null)

  // Assignments
  const [assignments, setAssignments] = useState<RoleAssignment[]>([])
  const [assignmentsLoading, setAssignmentsLoading] = useState(false)

  // Create assignment form
  const [showCreateAssignment, setShowCreateAssignment] = useState(false)
  const [assignProvider, setAssignProvider] = useState('local')
  const [assignEmail, setAssignEmail] = useState('')
  const [assignRoles, setAssignRoles] = useState<string[]>([])
  const [creatingAssignment, setCreatingAssignment] = useState(false)

  type AssignmentSortKey = 'provider' | 'email' | 'role' | 'created'
  const assignmentAccessor = useCallback((item: RoleAssignment, key: AssignmentSortKey) => {
    switch (key) {
      case 'provider': return item.attributes.provider
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

  useEffect(() => {
    if (activeTab === 'assignments') loadAssignments()
  }, [activeTab])

  async function loadRoles() {
    setRolesLoading(true)
    try {
      const res = await apiFetch('/api/v2/roles')
      if (!res.ok) throw new Error('Failed to load roles')
      const data = await res.json()
      setRoles(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load roles')
    } finally {
      setRolesLoading(false)
    }
  }

  async function loadAssignments() {
    setAssignmentsLoading(true)
    try {
      const res = await apiFetch('/api/v2/role-assignments')
      if (!res.ok) throw new Error('Failed to load assignments')
      const data = await res.json()
      setAssignments(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load assignments')
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

  async function handleCreateRole(e: React.FormEvent) {
    e.preventDefault()
    setCreatingRole(true)
    setError('')
    setSuccess('')
    try {
      const attrs: Record<string, unknown> = {
        name: roleName,
        description: roleDesc,
        'workspace-permission': rolePermission,
      }
      if (roleAllowLabels.trim()) attrs['allow-labels'] = parseLabels(roleAllowLabels)
      if (roleAllowNames.trim()) attrs['allow-names'] = roleAllowNames.split(',').map((s) => s.trim()).filter(Boolean)
      if (roleDenyLabels.trim()) attrs['deny-labels'] = parseLabels(roleDenyLabels)
      if (roleDenyNames.trim()) attrs['deny-names'] = roleDenyNames.split(',').map((s) => s.trim()).filter(Boolean)

      const res = await apiFetch('/api/v2/roles', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'roles', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to create role (${res.status})`)
      }
      setSuccess(`Role "${roleName}" created`)
      setRoleName('')
      setRoleDesc('')
      setRolePermission('read')
      setRoleAllowLabels('')
      setRoleAllowNames('')
      setRoleDenyLabels('')
      setRoleDenyNames('')
      setShowCreateRole(false)
      await loadRoles()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create role')
    } finally {
      setCreatingRole(false)
    }
  }

  function startEditRole(role: Role) {
    setEditingRole(role.name)
    setEditRoleDesc(role.attributes.description || '')
    setEditRolePermission(role.attributes['workspace-permission'] || 'read')
    setEditRoleAllowLabels(formatLabels(role.attributes['allow-labels'] || {}))
    setEditRoleAllowNames((role.attributes['allow-names'] || []).join(', '))
    setEditRoleDenyLabels(formatLabels(role.attributes['deny-labels'] || {}))
    setEditRoleDenyNames((role.attributes['deny-names'] || []).join(', '))
  }

  async function handleSaveRole() {
    if (!editingRole) return
    setSavingRole(true)
    setError('')
    setSuccess('')
    try {
      const attrs: Record<string, unknown> = {
        description: editRoleDesc,
        'workspace-permission': editRolePermission,
        'allow-labels': parseLabels(editRoleAllowLabels),
        'allow-names': editRoleAllowNames.split(',').map((s) => s.trim()).filter(Boolean),
        'deny-labels': parseLabels(editRoleDenyLabels),
        'deny-names': editRoleDenyNames.split(',').map((s) => s.trim()).filter(Boolean),
      }
      const res = await apiFetch(`/api/v2/roles/${editingRole}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'roles', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Failed to update role')
      }
      setSuccess(`Role "${editingRole}" updated`)
      setEditingRole(null)
      await loadRoles()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update role')
    } finally {
      setSavingRole(false)
    }
  }

  async function handleDeleteRole(name: string) {
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/v2/roles/${name}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete role')
      setDeleteRoleName(null)
      setSuccess(`Role "${name}" deleted`)
      await loadRoles()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete role')
    }
  }

  async function handleCreateAssignment(e: React.FormEvent) {
    e.preventDefault()
    setCreatingAssignment(true)
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch('/api/v2/role-assignments', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'role-assignments',
            attributes: {
              provider: assignProvider,
              email: assignEmail,
              roles: assignRoles,
            },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to set assignment (${res.status})`)
      }
      setSuccess(`Roles assigned to ${assignEmail}`)
      setAssignEmail('')
      setAssignRoles([])
      setShowCreateAssignment(false)
      await loadAssignments()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to set assignment')
    } finally {
      setCreatingAssignment(false)
    }
  }

  async function handleDeleteAssignment(provider: string, email: string, roleName: string) {
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/v2/role-assignments/${encodeURIComponent(provider)}/${encodeURIComponent(email)}/${encodeURIComponent(roleName)}`, {
        method: 'DELETE',
      })
      if (!res.ok) throw new Error('Failed to delete assignment')
      setSuccess('Assignment removed')
      await loadAssignments()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete assignment')
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
      case 'plan': return 'bg-blue-900/50 text-blue-300'
      case 'read': return 'bg-green-900/50 text-green-300'
      default: return 'bg-slate-700 text-slate-400'
    }
  }

  const tabs: { key: Tab; label: string }[] = [
    { key: 'roles', label: 'Roles' },
    { key: 'assignments', label: 'Assignments' },
  ]

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="Roles & Assignments"
          description="Manage RBAC roles and user role assignments"
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
                {showCreateRole ? 'Cancel' : 'Create Role'}
              </button>
            </div>

            {showCreateRole && (
              <form onSubmit={handleCreateRole} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <div>
                    <label htmlFor="r-name" className="block text-sm font-medium text-slate-300 mb-1">Name</label>
                    <input id="r-name" type="text" value={roleName} onChange={(e) => setRoleName(e.target.value)} required placeholder="developer"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="r-perm" className="block text-sm font-medium text-slate-300 mb-1">Workspace Permission</label>
                    <select id="r-perm" value={rolePermission} onChange={(e) => setRolePermission(e.target.value)}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                      <option value="read">read</option>
                      <option value="plan">plan</option>
                      <option value="write">write</option>
                      <option value="admin">admin</option>
                    </select>
                  </div>
                  <div>
                    <label htmlFor="r-desc" className="block text-sm font-medium text-slate-300 mb-1">Description</label>
                    <input id="r-desc" type="text" value={roleDesc} onChange={(e) => setRoleDesc(e.target.value)} placeholder="Developer access"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="r-allow-labels" className="block text-sm font-medium text-slate-300 mb-1">Allow Labels (key=value, comma-separated)</label>
                    <input id="r-allow-labels" type="text" value={roleAllowLabels} onChange={(e) => setRoleAllowLabels(e.target.value)} placeholder="env=dev, team=platform"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="r-allow-names" className="block text-sm font-medium text-slate-300 mb-1">Allow Names (comma-separated)</label>
                    <input id="r-allow-names" type="text" value={roleAllowNames} onChange={(e) => setRoleAllowNames(e.target.value)} placeholder="ws-prod, ws-staging"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="r-deny-labels" className="block text-sm font-medium text-slate-300 mb-1">Deny Labels (key=value, comma-separated)</label>
                    <input id="r-deny-labels" type="text" value={roleDenyLabels} onChange={(e) => setRoleDenyLabels(e.target.value)} placeholder="env=prod"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="r-deny-names" className="block text-sm font-medium text-slate-300 mb-1">Deny Names (comma-separated)</label>
                    <input id="r-deny-names" type="text" value={roleDenyNames} onChange={(e) => setRoleDenyNames(e.target.value)} placeholder="ws-critical"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                </div>
                <button type="submit" disabled={creatingRole}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
                  {creatingRole ? 'Creating...' : 'Create Role'}
                </button>
              </form>
            )}

            {rolesLoading ? (
              <LoadingSpinner />
            ) : roles.length === 0 ? (
              <EmptyState message="No roles defined." />
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
                              <button onClick={() => setEditingRole(null)} className="text-xs text-slate-400 hover:text-slate-200">Cancel</button>
                              <button onClick={handleSaveRole} disabled={savingRole} className="text-xs text-brand-400 hover:text-brand-300">
                                {savingRole ? 'Saving...' : 'Save'}
                              </button>
                            </div>
                          </div>
                          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                            <div>
                              <label className="block text-xs text-slate-500 mb-1">Description</label>
                              <input type="text" value={editRoleDesc} onChange={(e) => setEditRoleDesc(e.target.value)}
                                className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                            </div>
                            <div>
                              <label className="block text-xs text-slate-500 mb-1">Permission</label>
                              <select value={editRolePermission} onChange={(e) => setEditRolePermission(e.target.value)}
                                className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500">
                                <option value="read">read</option>
                                <option value="plan">plan</option>
                                <option value="write">write</option>
                                <option value="admin">admin</option>
                              </select>
                            </div>
                            <div>
                              <label className="block text-xs text-slate-500 mb-1">Allow Labels</label>
                              <input type="text" value={editRoleAllowLabels} onChange={(e) => setEditRoleAllowLabels(e.target.value)}
                                className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                            </div>
                            <div>
                              <label className="block text-xs text-slate-500 mb-1">Allow Names</label>
                              <input type="text" value={editRoleAllowNames} onChange={(e) => setEditRoleAllowNames(e.target.value)}
                                className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                            </div>
                            <div>
                              <label className="block text-xs text-slate-500 mb-1">Deny Labels</label>
                              <input type="text" value={editRoleDenyLabels} onChange={(e) => setEditRoleDenyLabels(e.target.value)}
                                className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                            </div>
                            <div>
                              <label className="block text-xs text-slate-500 mb-1">Deny Names</label>
                              <input type="text" value={editRoleDenyNames} onChange={(e) => setEditRoleDenyNames(e.target.value)}
                                className="w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                            </div>
                          </div>
                        </div>
                      ) : (
                        <div className="flex items-start justify-between">
                          <div>
                            <div className="flex items-center gap-2 mb-1">
                              <h3 className="text-sm font-medium text-slate-200">{role.name}</h3>
                              {a['built-in'] && (
                                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-700 text-slate-400">built-in</span>
                              )}
                              {a['workspace-permission'] && (
                                <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${permissionBadge(a['workspace-permission'])}`}>
                                  {a['workspace-permission']}
                                </span>
                              )}
                            </div>
                            {a.description && <p className="text-xs text-slate-400 mb-2">{a.description}</p>}
                            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
                              {Object.keys(a['allow-labels'] || {}).length > 0 && (
                                <span>Allow: {formatLabels(a['allow-labels'])}</span>
                              )}
                              {(a['allow-names'] || []).length > 0 && (
                                <span>Allow names: {a['allow-names'].join(', ')}</span>
                              )}
                              {Object.keys(a['deny-labels'] || {}).length > 0 && (
                                <span>Deny: {formatLabels(a['deny-labels'])}</span>
                              )}
                              {(a['deny-names'] || []).length > 0 && (
                                <span>Deny names: {a['deny-names'].join(', ')}</span>
                              )}
                            </div>
                          </div>
                          {!a['built-in'] && (
                            <div className="flex gap-2 flex-shrink-0">
                              <button onClick={() => startEditRole(role)} className="text-xs text-brand-400 hover:text-brand-300">Edit</button>
                              {deleteRoleName === role.name ? (
                                <>
                                  <button onClick={() => setDeleteRoleName(null)} className="text-xs text-slate-400 hover:text-slate-200">Cancel</button>
                                  <button onClick={() => handleDeleteRole(role.name)} className="text-xs text-red-400 hover:text-red-300">Confirm</button>
                                </>
                              ) : (
                                <button onClick={() => setDeleteRoleName(role.name)} className="text-xs text-red-400 hover:text-red-300">Delete</button>
                              )}
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
                {showCreateAssignment ? 'Cancel' : 'Add Assignment'}
              </button>
            </div>

            {showCreateAssignment && (
              <form onSubmit={handleCreateAssignment} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="a-provider" className="block text-sm font-medium text-slate-300 mb-1">Provider</label>
                    <input id="a-provider" type="text" value={assignProvider} onChange={(e) => setAssignProvider(e.target.value)} required
                      placeholder="local"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                  <div>
                    <label htmlFor="a-email" className="block text-sm font-medium text-slate-300 mb-1">Email</label>
                    <input id="a-email" type="email" value={assignEmail} onChange={(e) => setAssignEmail(e.target.value)} required
                      placeholder="user@example.com"
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
                  </div>
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-2">Roles</label>
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
                  {creatingAssignment ? 'Saving...' : 'Set Roles'}
                </button>
              </form>
            )}

            {assignmentsLoading ? (
              <LoadingSpinner />
            ) : assignments.length === 0 ? (
              <EmptyState message="No role assignments." />
            ) : (
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <SortableHeader label="Provider" sortKey="provider" sortState={assignmentSortState} onSort={toggleAssignmentSort} />
                      <SortableHeader label="Email" sortKey="email" sortState={assignmentSortState} onSort={toggleAssignmentSort} />
                      <SortableHeader label="Role" sortKey="role" sortState={assignmentSortState} onSort={toggleAssignmentSort} />
                      <SortableHeader label="Created" sortKey="created" sortState={assignmentSortState} onSort={toggleAssignmentSort} className="hidden sm:table-cell" />
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {sortedAssignments.map((a) => (
                      <tr key={a.id} className="hover:bg-slate-700/20 transition-colors">
                        <td className="px-4 py-3 text-sm text-slate-400">{a.attributes.provider}</td>
                        <td className="px-4 py-3 text-sm text-slate-200">{a.attributes.email}</td>
                        <td className="px-4 py-3">
                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-brand-900/50 text-brand-300">
                            {a.attributes['role-name']}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-xs text-slate-500 hidden sm:table-cell">
                          {a.attributes['created-at'] ? new Date(a.attributes['created-at']).toLocaleDateString() : ''}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <button
                            onClick={() => handleDeleteAssignment(a.attributes.provider, a.attributes.email, a.attributes['role-name'])}
                            className="text-xs text-red-400 hover:text-red-300"
                          >
                            Remove
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
