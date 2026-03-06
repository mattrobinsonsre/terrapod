'use client'

import { useEffect } from 'react'
import { useRouter, useParams } from 'next/navigation'
import { apiFetch } from '@/lib/api'
import { LoadingSpinner } from '@/components/loading-spinner'
import NavBar from '@/components/nav-bar'

/**
 * TFE-compatible URL redirect.
 *
 * The terraform/tofu CLI prints URLs like:
 *   /app/{org}/{workspace-name}/runs/{runId}
 *
 * Terrapod uses workspace IDs in its routes:
 *   /workspaces/{wsId}/runs/{runId}
 *
 * This page resolves the workspace name to an ID and redirects.
 */
export default function TFERedirectPage() {
  const router = useRouter()
  const params = useParams()
  const segments = params.path as string[]

  useEffect(() => {
    async function resolve() {
      // Expected: [org, workspace-name, "runs", runId]
      if (segments.length >= 4 && segments[2] === 'runs') {
        const org = segments[0]
        const wsName = segments[1]
        const runId = segments[3]

        try {
          const res = await apiFetch(`/api/v2/organizations/${org}/workspaces/${wsName}`)
          if (res.ok) {
            const data = await res.json()
            const wsId = data.data.id
            router.replace(`/workspaces/${wsId}/runs/${runId}`)
            return
          }
        } catch {
          // Fall through to workspace list
        }
      }

      // Fallback: redirect to workspaces list
      router.replace('/workspaces')
    }

    resolve()
  }, [segments, router])

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <LoadingSpinner />
      </main>
    </>
  )
}
