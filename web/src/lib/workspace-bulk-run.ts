// Runs a bulk action by calling the existing per-run endpoints once per selected
// workspace. No new API surface: the server stays authoritative, so a workspace
// the caller may not act on, or whose run has moved on, comes back refused with
// the server's own reason, and the others carry on.

import type { BulkActionId, BulkWorkspace } from './workspace-bulk-actions'

export type Fetcher = (path: string, init?: RequestInit) => Promise<Response>

export interface Endpoint {
  path: string
  body?: unknown
}

export type BulkResult = { id: string; name: string; ok: true } | { id: string; name: string; ok: false; reason: string }

// The endpoint for one workspace, or null when the workspace lacks the run the action needs.
export function endpointFor(action: BulkActionId, w: BulkWorkspace): Endpoint | null {
  if (action === 'plan') {
    return {
      path: '/api/v1/runs',
      body: {
        data: {
          type: 'runs',
          attributes: { 'plan-only': true },
          relationships: { workspace: { data: { type: 'workspaces', id: w.id } } },
        },
      },
    }
  }
  if (!w.latestRun) return null
  return { path: `/api/v1/runs/${w.latestRun.id}/actions/${action}` }
}

// The server's reason, from the JSON:API errors array or the legacy `detail`.
async function reasonFrom(res: Response): Promise<string> {
  try {
    const body = await res.json()
    if (Array.isArray(body?.errors) && body.errors[0]?.detail) return String(body.errors[0].detail)
    if (typeof body?.detail === 'string') return body.detail
  } catch {
    /* not JSON */
  }
  return `Request failed (${res.status})`
}

// Sequential, not parallel: a batch can be dozens of workspaces, and the server
// serialises per workspace anyway, so a burst buys nothing and risks rate limits.
export async function runBulkAction(
  action: BulkActionId,
  selection: readonly BulkWorkspace[],
  fetcher: Fetcher,
): Promise<BulkResult[]> {
  const results: BulkResult[] = []
  for (const w of selection) {
    const endpoint = endpointFor(action, w)
    if (!endpoint) {
      results.push({ id: w.id, name: w.name, ok: false, reason: 'No run to act on' })
      continue
    }
    try {
      const res = await fetcher(endpoint.path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: endpoint.body === undefined ? undefined : JSON.stringify(endpoint.body),
      })
      results.push(res.ok ? { id: w.id, name: w.name, ok: true } : { id: w.id, name: w.name, ok: false, reason: await reasonFrom(res) })
    } catch (err) {
      results.push({ id: w.id, name: w.name, ok: false, reason: err instanceof Error ? err.message : String(err) })
    }
  }
  return results
}
