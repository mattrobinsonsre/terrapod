'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import { Package, Blocks, Key } from 'lucide-react'
import NavBar from '@/components/nav-bar'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { getAuthState } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

interface AccountDetails {
  username: string
  email: string
  roles: string[]
}

export default function DashboardPage() {
  const router = useRouter()
  const [account, setAccount] = useState<AccountDetails | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    const auth = getAuthState()
    if (!auth) {
      router.push('/login')
      return
    }

    apiFetch('/api/v2/account/details')
      .then(async (res) => {
        if (!res.ok) throw new Error('Failed to load account details')
        const data = await res.json()
        const attrs = data.data?.attributes || {}
        setAccount({
          username: attrs.username || auth.email.split('@')[0],
          email: attrs.email || auth.email,
          roles: auth.roles,
        })
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [router])

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        {error && <ErrorBanner message={error} />}
        {loading ? (
          <LoadingSpinner />
        ) : account ? (
          <>
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6 mb-8">
              <h1 className="text-2xl font-bold text-slate-100">
                Welcome, {account.username}
              </h1>
              <p className="text-slate-400 mt-1">{account.email}</p>
              {account.roles.length > 0 && (
                <div className="flex gap-2 mt-3">
                  {account.roles.map((role) => (
                    <span
                      key={role}
                      className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-brand-900/50 text-brand-300 border border-brand-700/50"
                    >
                      {role}
                    </span>
                  ))}
                </div>
              )}
            </div>

            <h2 className="text-lg font-semibold text-slate-200 mb-4">Quick Links</h2>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <Link
                href="/registry/modules"
                className="bg-slate-800/50 rounded-lg border border-slate-700/50 hover:border-brand-600/30 p-5 transition-colors group"
              >
                <Package size={24} className="text-brand-400 mb-3" />
                <h3 className="font-semibold text-slate-200 group-hover:text-brand-300 transition-colors">
                  Modules
                </h3>
                <p className="text-sm text-slate-500 mt-1">
                  Browse and publish private Terraform modules
                </p>
              </Link>
              <Link
                href="/registry/providers"
                className="bg-slate-800/50 rounded-lg border border-slate-700/50 hover:border-brand-600/30 p-5 transition-colors group"
              >
                <Blocks size={24} className="text-brand-400 mb-3" />
                <h3 className="font-semibold text-slate-200 group-hover:text-brand-300 transition-colors">
                  Providers
                </h3>
                <p className="text-sm text-slate-500 mt-1">
                  Manage private Terraform providers
                </p>
              </Link>
              <Link
                href="/settings/tokens"
                className="bg-slate-800/50 rounded-lg border border-slate-700/50 hover:border-brand-600/30 p-5 transition-colors group"
              >
                <Key size={24} className="text-brand-400 mb-3" />
                <h3 className="font-semibold text-slate-200 group-hover:text-brand-300 transition-colors">
                  API Tokens
                </h3>
                <p className="text-sm text-slate-500 mt-1">
                  Create and manage API authentication tokens
                </p>
              </Link>
            </div>
          </>
        ) : null}
      </main>
    </>
  )
}
