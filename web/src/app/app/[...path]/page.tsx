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
 *   /app/default/{workspace-name}/runs/{runId}
 *
 * Terrapod uses workspace IDs in its routes:
 *   /workspaces/{wsId}/runs/{runId}
 *
 * This page resolves the workspace name to an ID and redirects. The
 * organization segment from the CLI URL is always "default" in Terrapod
 * (single-organization); we ignore whatever value lands in segments[0]
 * and look the workspace up under the literal "default" org.
 */
export default function TFERedirectPage() {
  const router = useRouter()
  const params = useParams()
  const segments = params.path as string[]

  useEffect(() => {
    async function resolve() {
      // Expected: [_org, workspace-name, "runs", runId]. The org
      // segment is ignored — Terrapod is single-org and the only
      // valid value is "default".
      if (segments.length >= 4 && segments[2] === 'runs') {
        const wsName = segments[1]
        const runId = segments[3]

        try {
          const res = await apiFetch(`/api/v2/organizations/default/workspaces/${wsName}`)
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
