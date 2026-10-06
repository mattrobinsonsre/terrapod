'use client'

import { useEffect, useState } from 'react'

import { apiFetch } from '@/lib/api'
import type { OidcAudienceDefaults } from '@/lib/oidc-audiences'

/**
 * The deployment's audience catalogue, for telling an inherited entry from an
 * override (#1901).
 *
 * `GET /api/terrapod/v1/oidc/audience-defaults` — any authenticated user, and
 * note the prefix: this line serves `/api/terrapod/v1`, so `/api/v1` would be
 * a silent 404 rather than a build error. Checked against
 * `services/tests/api/api_route_contract.json`.
 *
 * **A failed probe is not a broken form.** It resolves to an empty catalogue
 * with the issuer assumed ON, which degrades to the pre-provenance behaviour:
 * every entry reads as workspace-owned, so a save still sends what the
 * operator can see rather than silently dropping something as inherited. The
 * opposite default — assuming the issuer is off — would grey out an editor
 * that works, and assuming a catalogue we could not read would mark real
 * overrides as inherited and drop them.
 */
export function useOidcAudienceDefaults(): OidcAudienceDefaults & { loaded: boolean } {
  const [state, setState] = useState<OidcAudienceDefaults & { loaded: boolean }>({
    audiences: {},
    issuerEnabled: true,
    loaded: false,
  })

  useEffect(() => {
    let live = true
    ;(async () => {
      try {
        const res = await apiFetch('/api/terrapod/v1/oidc/audience-defaults')
        if (!res.ok) {
          if (live) setState((s) => ({ ...s, loaded: true }))
          return
        }
        const attrs = (await res.json()).data?.attributes ?? {}
        if (!live) return
        setState({
          audiences: attrs.audiences ?? {},
          // Absent reads as enabled, matching the fail-soft above.
          issuerEnabled: attrs['issuer-enabled'] !== false,
          loaded: true,
        })
      } catch {
        if (live) setState((s) => ({ ...s, loaded: true }))
      }
    })()
    return () => {
      live = false
    }
  }, [])

  return state
}
