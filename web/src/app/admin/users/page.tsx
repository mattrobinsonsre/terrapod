'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { zxcvbnAsync, zxcvbnOptions } from '@zxcvbn-ts/core'
import * as zxcvbnCommonPackage from '@zxcvbn-ts/language-common'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { useSortable } from '@/lib/use-sortable'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { usePollingInterval } from '@/lib/use-polling-interval'

zxcvbnOptions.setOptions({
  dictionary: { ...zxcvbnCommonPackage.dictionary },
  graphs: zxcvbnCommonPackage.adjacencyGraphs,
})

const SCORE_LABELS = ['Very weak', 'Weak', 'Fair', 'Good', 'Strong'] as const
const SCORE_COLORS = ['bg-red-500', 'bg-red-500', 'bg-yellow-500', 'bg-brand-500', 'bg-green-500']
const SCORE_TEXT = ['text-red-400', 'text-red-400', 'text-yellow-400', 'text-brand-400', 'text-green-400']
const MIN_SCORE = 3

interface UserRecord {
  id: string
  attributes: {
    email: string
    'display-name': string | null
    'is-active': boolean
    'has-password': boolean
    'last-login-at': string | null
    'created-at': string | null
    'updated-at': string | null
  }
}

const emptyResult = { score: 0, warning: '', suggestions: [] as string[] }

function usePasswordStrength(password: string) {
  const [result, setResult] = useState(emptyResult)

  useEffect(() => {
    if (!password) return
    let cancelled = false
    zxcvbnAsync(password).then((r) => {
      if (!cancelled) {
        setResult({
          score: r.score,
          warning: r.feedback.warning || '',
          suggestions: r.feedback.suggestions || [],
        })
      }
    })
    return () => { cancelled = true }
  }, [password])

  if (!password) return emptyResult

  return result
}

function usePasswordValid(password: string): boolean {
  const { score } = usePasswordStrength(password)
  return !!password && score >= MIN_SCORE
}

function PasswordStrength({ password }: { password: string }) {
  const { score, warning, suggestions } = usePasswordStrength(password)
  if (!password) return null

  const feedback = [warning, ...suggestions].filter(Boolean)

  return (
    <div className="mt-2 space-y-2">
      <div className="flex items-center gap-2">
        <div className="flex gap-1 flex-1">
          {[0, 1, 2, 3, 4].map((i) => (
            <div
              key={i}
              className={`h-1 flex-1 rounded-full transition-colors ${
                i <= score ? SCORE_COLORS[score] : 'bg-slate-700'
              }`}
            />
          ))}
        </div>
        <span className={`text-xs font-medium ${SCORE_TEXT[score]}`}>{SCORE_LABELS[score]}</span>
      </div>
      {feedback.length > 0 && (
        <ul className="space-y-0.5">
          {feedback.map((msg, i) => (
            <li key={i} className="text-xs text-slate-400">{msg}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

function UserForm({
  onSubmit,
  onCancel,
  submitting,
}: {
  onSubmit: (data: { email: string; password?: string; displayName?: string }) => void
  onCancel: () => void
  submitting: boolean
}) {
  const [email, setEmail] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const passwordValid = usePasswordValid(password)

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    onSubmit({
      email,
      displayName: displayName || undefined,
      password: password || undefined,
    })
  }

  return (
    <form onSubmit={handleSubmit} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-4">
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div>
          <label htmlFor="u-email" className="block text-sm font-medium text-slate-300 mb-1">Email</label>
          <input
            id="u-email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            placeholder="user@example.com"
            className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
          />
        </div>
        <div>
          <label htmlFor="u-name" className="block text-sm font-medium text-slate-300 mb-1">Display Name</label>
          <input
            id="u-name"
            type="text"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="Jane Doe"
            className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
          />
        </div>
        <div>
          <label htmlFor="u-pw" className="block text-sm font-medium text-slate-300 mb-1">Password (optional)</label>
          <input
            id="u-pw"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Enter a strong password"
            autoComplete="new-password"
            className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
          />
          <PasswordStrength password={password} />
        </div>
      </div>
      <div className="flex gap-2 justify-end">
        <button
          type="button"
          onClick={onCancel}
          className="px-4 py-2 text-sm text-slate-400 hover:text-slate-200 transition-colors"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={submitting || (!!password && !passwordValid)}
          className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
        >
          {submitting ? 'Creating...' : 'Create User'}
        </button>
      </div>
    </form>
  )
}

function PasswordResetForm({
  email,
  onSubmit,
  onCancel,
  submitting,
}: {
  email: string
  onSubmit: (password: string) => void
  onCancel: () => void
  submitting: boolean
}) {
  const [password, setPassword] = useState('')
  const passwordValid = usePasswordValid(password)

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    onSubmit(password)
  }

  return (
    <form onSubmit={handleSubmit} className="mt-3 p-3 bg-slate-700/50 rounded-lg border border-slate-600/50 space-y-3">
      <div className="flex items-center gap-3">
        <span className="text-sm font-medium text-slate-200">Reset password for {email}</span>
      </div>
      <div>
        <label htmlFor={`pw-${email}`} className="block text-sm font-medium text-slate-300 mb-1">New Password</label>
        <input
          id={`pw-${email}`}
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          placeholder="Enter a strong password"
          autoComplete="new-password"
          className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
        />
        <PasswordStrength password={password} />
      </div>
      <div className="flex gap-2 justify-end">
        <button
          type="button"
          onClick={onCancel}
          className="px-4 py-2 text-sm text-slate-400 hover:text-slate-200 transition-colors"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={submitting || !passwordValid}
          className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
        >
          {submitting ? 'Saving...' : 'Reset Password'}
        </button>
      </div>
    </form>
  )
}

export default function UsersPage() {
  const router = useRouter()
  const [users, setUsers] = useState<UserRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  // Create form
  const [showCreate, setShowCreate] = useState(false)
  const [creating, setCreating] = useState(false)

  // Inline edit
  const [editingEmail, setEditingEmail] = useState<string | null>(null)
  const [editDisplayName, setEditDisplayName] = useState('')
  const [savingEdit, setSavingEdit] = useState(false)

  // Password reset
  const [resetEmail, setResetEmail] = useState<string | null>(null)
  const [resettingPassword, setResettingPassword] = useState(false)

  // Delete
  const [deleteEmail, setDeleteEmail] = useState<string | null>(null)

  type SortKey = 'email' | 'displayName' | 'active' | 'lastLogin' | 'created'
  const accessor = useCallback((item: UserRecord, key: SortKey) => {
    switch (key) {
      case 'email': return item.attributes.email
      case 'displayName': return item.attributes['display-name'] || ''
      case 'active': return item.attributes['is-active'] ? 'active' : 'inactive'
      case 'lastLogin': return item.attributes['last-login-at'] || ''
      case 'created': return item.attributes['created-at'] || ''
    }
  }, [])
  const { sortedItems, sortState, toggleSort } = useSortable<UserRecord, SortKey>(
    users, 'email', 'asc', accessor,
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadUsers()
  }, [router])

  usePollingInterval(!loading, 60_000, loadUsers)

  async function loadUsers() {
    setLoading(true)
    try {
      const res = await apiFetch('/api/v2/organizations/default/users?page[size]=100')
      if (!res.ok) throw new Error('Failed to load users')
      const data = await res.json()
      setUsers(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load users')
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(data: { email: string; password?: string; displayName?: string }) {
    setCreating(true)
    setError('')
    setSuccess('')
    try {
      const attrs: Record<string, unknown> = { email: data.email }
      if (data.password) attrs.password = data.password
      if (data.displayName) attrs['display-name'] = data.displayName

      const res = await apiFetch('/api/v2/organizations/default/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'users', attributes: attrs } }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || `Failed to create user (${res.status})`)
      }
      setSuccess(`User "${data.email}" created`)
      setShowCreate(false)
      await loadUsers()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create user')
    } finally {
      setCreating(false)
    }
  }

  function startEdit(u: UserRecord) {
    setEditingEmail(u.attributes.email)
    setEditDisplayName(u.attributes['display-name'] || '')
    setResetEmail(null)
  }

  async function handleSaveEdit() {
    if (!editingEmail) return
    setSavingEdit(true)
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/v2/users/${encodeURIComponent(editingEmail)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'users', attributes: { 'display-name': editDisplayName } },
        }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || 'Failed to update user')
      }
      setSuccess(`User "${editingEmail}" updated`)
      setEditingEmail(null)
      await loadUsers()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update user')
    } finally {
      setSavingEdit(false)
    }
  }

  async function handleToggleActive(email: string, currentlyActive: boolean) {
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/v2/users/${encodeURIComponent(email)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'users', attributes: { 'is-active': !currentlyActive } },
        }),
      })
      if (!res.ok) throw new Error('Failed to update user')
      setSuccess(`User "${email}" ${currentlyActive ? 'deactivated' : 'activated'}`)
      await loadUsers()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update user')
    }
  }

  async function handleResetPassword(email: string, password: string) {
    setResettingPassword(true)
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/v2/users/${encodeURIComponent(email)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'users', attributes: { password } },
        }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || 'Failed to reset password')
      }
      setSuccess(`Password reset for "${email}"`)
      setResetEmail(null)
      await loadUsers()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reset password')
    } finally {
      setResettingPassword(false)
    }
  }

  async function handleDelete(email: string) {
    setError('')
    setSuccess('')
    try {
      const res = await apiFetch(`/api/v2/users/${encodeURIComponent(email)}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete user')
      setDeleteEmail(null)
      if (resetEmail === email) setResetEmail(null)
      if (editingEmail === email) setEditingEmail(null)
      setSuccess(`User "${email}" deleted`)
      await loadUsers()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete user')
    }
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="Users"
          description="Manage local user accounts"
        />

        {error && <ErrorBanner message={error} />}
        {success && (
          <div className="mb-4 p-3 bg-green-900/30 text-green-400 rounded-lg text-sm border border-green-800/50">{success}</div>
        )}

        <div className="flex justify-end mb-4">
          <button
            onClick={() => { setShowCreate(!showCreate); setError(''); setSuccess('') }}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors"
          >
            {showCreate ? 'Cancel' : 'Create User'}
          </button>
        </div>

        {showCreate && (
          <UserForm
            onSubmit={handleCreate}
            onCancel={() => setShowCreate(false)}
            submitting={creating}
          />
        )}

        {loading ? (
          <LoadingSpinner />
        ) : users.length === 0 ? (
          <EmptyState message="No users found." />
        ) : (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <SortableHeader label="Email" sortKey="email" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label="Display Name" sortKey="displayName" sortState={sortState} onSort={toggleSort} className="hidden sm:table-cell" />
                  <SortableHeader label="Status" sortKey="active" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label="Last Login" sortKey="lastLogin" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
                  <SortableHeader label="Created" sortKey="created" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                  <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase tracking-wider">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700/30">
                {sortedItems.map((u) => {
                  const a = u.attributes
                  const isEditing = editingEmail === a.email
                  const isResetting = resetEmail === a.email
                  return (
                    <tr key={a.email} className="hover:bg-slate-700/20 transition-colors group">
                      <td className="px-4 py-3 text-sm text-slate-200">{a.email}</td>
                      <td className="px-4 py-3 text-sm text-slate-400 hidden sm:table-cell">
                        {isEditing ? (
                          <div className="flex items-center gap-2">
                            <input
                              type="text"
                              value={editDisplayName}
                              onChange={(e) => setEditDisplayName(e.target.value)}
                              className="px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500 w-40"
                            />
                            <button onClick={handleSaveEdit} disabled={savingEdit} className="text-xs text-brand-400 hover:text-brand-300">
                              {savingEdit ? 'Saving...' : 'Save'}
                            </button>
                            <button onClick={() => setEditingEmail(null)} className="text-xs text-slate-400 hover:text-slate-200">
                              Cancel
                            </button>
                          </div>
                        ) : (
                          a['display-name'] || <span className="text-slate-600">-</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <button
                          onClick={() => handleToggleActive(a.email, a['is-active'])}
                          className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium cursor-pointer transition-colors ${
                            a['is-active']
                              ? 'bg-green-900/50 text-green-300 hover:bg-green-900/80'
                              : 'bg-red-900/50 text-red-300 hover:bg-red-900/80'
                          }`}
                        >
                          {a['is-active'] ? 'Active' : 'Inactive'}
                        </button>
                      </td>
                      <td className="px-4 py-3 text-xs text-slate-500 hidden md:table-cell">
                        {a['last-login-at'] ? new Date(a['last-login-at']).toLocaleString() : 'Never'}
                      </td>
                      <td className="px-4 py-3 text-xs text-slate-500 hidden lg:table-cell">
                        {a['created-at'] ? new Date(a['created-at']).toLocaleDateString() : ''}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <div className="flex gap-2 justify-end">
                          {!isEditing && (
                            <button onClick={() => startEdit(u)} className="text-xs text-brand-400 hover:text-brand-300">Edit</button>
                          )}
                          {!isResetting ? (
                            <button onClick={() => { setResetEmail(a.email); setEditingEmail(null) }} className="text-xs text-yellow-400 hover:text-yellow-300">
                              {a['has-password'] ? 'Reset PW' : 'Set PW'}
                            </button>
                          ) : (
                            <button onClick={() => setResetEmail(null)} className="text-xs text-slate-400 hover:text-slate-200">Cancel PW</button>
                          )}
                          {deleteEmail === a.email ? (
                            <>
                              <button onClick={() => setDeleteEmail(null)} className="text-xs text-slate-400 hover:text-slate-200">Cancel</button>
                              <button onClick={() => handleDelete(a.email)} className="text-xs text-red-400 hover:text-red-300">Confirm</button>
                            </>
                          ) : (
                            <button onClick={() => setDeleteEmail(a.email)} className="text-xs text-red-400 hover:text-red-300">Delete</button>
                          )}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            {/* Password reset forms render below the table row */}
            {resetEmail && (
              <div className="px-4 pb-4">
                <PasswordResetForm
                  email={resetEmail}
                  onSubmit={(pw) => handleResetPassword(resetEmail, pw)}
                  onCancel={() => setResetEmail(null)}
                  submitting={resettingPassword}
                />
              </div>
            )}
          </div>
        )}
      </main>
    </>
  )
}
