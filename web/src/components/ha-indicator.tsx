'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { CircleCheck, CircleAlert, CircleDot, CircleX } from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { getAuthState } from '@/lib/auth'

/**
 * The always-visible HA indicator (#1165).
 *
 * Two questions an operator should never have to navigate to answer: **which
 * node am I talking to, and is it healthy?** Someone with two tabs open on two
 * nodes, mid-failover, is exactly who this is for — and they are also exactly
 * who cannot afford to go and look.
 *
 * Three deliberate choices:
 *
 *  - **Everyone sees it.** Hiding "you are talking to a follower" from the
 *    person whose next apply is about to be refused is the opposite of useful.
 *  - **Clicking it is how you reach the HA page.** Node disposition is context,
 *    not an administrative task, so it does not live in the Admin menu.
 *  - **Colour is never the only signal.** Each state has its own icon and its
 *    own word, so it survives colour-blindness and a monochrome screenshot.
 *
 * It renders as a bordered chip, not a bare dot: a lone icon, in a bar where
 * every other control is icon-plus-text, reads as decoration and gets missed
 * entirely — which is exactly what happened to the first version of this.
 *
 * It carries **no visible text in any state** — colour and symbol only. The
 * words live in the tooltip and the accessible name, which is where they can be
 * as precise as they like without competing with the nav for width. Nothing is
 * lost to a screen reader, and nothing depends on colour alone, because the
 * symbol differs per state too.
 */

type Tone = 'ok' | 'warn' | 'passive' | 'down'

interface Snapshot {
  role: string
  peerConfigured: boolean
  replicationEnabled: boolean
  inSync: boolean
  backfilling: number
}

/**
 * One fetch shared by every mount, on one interval.
 *
 * The indicator renders on every page in every session, so a per-mount poll
 * would multiply straight into API load for a number that changes on the order
 * of minutes. A module-level store keeps it to a single request per interval no
 * matter how many components subscribe.
 */
const POLL_MS = 30_000

let snapshot: Snapshot | null = null
let unavailable = false
let timer: ReturnType<typeof setInterval> | null = null
const subscribers = new Set<() => void>()

function publish() {
  subscribers.forEach((fn) => fn())
}

async function refresh() {
  // No token means the login page, where there is nothing to indicate.
  if (!getAuthState()?.token) return
  try {
    const resp = await apiFetch('/api/terrapod/v1/ha/status')
    if (!resp.ok) throw new Error(String(resp.status))
    const attrs = (await resp.json()).data.attributes
    snapshot = {
      role: attrs.role,
      peerConfigured: Boolean(attrs['peer-configured']),
      replicationEnabled: Boolean(attrs['replication-enabled']),
      inSync: Boolean(attrs['in-sync']),
      backfilling: (attrs['backfilling-classes'] || []).length,
    }
    unavailable = false
  } catch {
    // A failed read is not a failed node — say nothing rather than show a red
    // dot for what is probably a transient blip in the page's own request.
    unavailable = true
  }
  publish()
}

function subscribe(fn: () => void) {
  subscribers.add(fn)
  if (timer === null) {
    void refresh()
    timer = setInterval(() => void refresh(), POLL_MS)
  }
  return () => {
    subscribers.delete(fn)
    if (subscribers.size === 0 && timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }
}

function toneOf(s: Snapshot): Tone {
  // A follower is not broken — it is doing its job. But it originates nothing,
  // so anyone reading it needs to know before they try to change something.
  if (s.role === 'follower') return 'passive'
  if (!s.peerConfigured) return 'ok' // A single node is a legitimate healthy state.
  if (!s.replicationEnabled) return 'warn'
  return s.inSync ? 'ok' : 'warn'
}

const ICONS = {
  ok: CircleCheck,
  warn: CircleAlert,
  passive: CircleDot,
  down: CircleX,
} as const

const TONES = {
  ok: 'border-emerald-800/60 bg-emerald-950/40 text-emerald-300 hover:bg-emerald-950/70',
  warn: 'border-amber-700/60 bg-amber-950/50 text-amber-200 hover:bg-amber-950/80',
  passive: 'border-sky-800/60 bg-sky-950/40 text-sky-300 hover:bg-sky-950/70',
  down: 'border-red-800/60 bg-red-950/50 text-red-200 hover:bg-red-950/80',
} as const

export function HAIndicator() {
  const t = useTranslations('haIndicator')
  const [, force] = useState(0)

  useEffect(() => subscribe(() => force((n) => n + 1)), [])

  // Nothing to say yet, or the read failed. Either way an empty slot beats a
  // grey dot that an operator would have to interpret.
  if (!snapshot || unavailable) return null

  const tone = toneOf(snapshot)
  const Icon = ICONS[tone]
  const role = tone === 'passive' ? t('follower') : t('leader')
  const detail = !snapshot.peerConfigured
    ? t('singleNode')
    : snapshot.backfilling > 0
      ? t('backfilling')
      : tone === 'ok'
        ? t('inSync')
        : t('behind')
  return (
    <Link
      href="/ha"
      title={`${role} — ${detail}`}
      aria-label={t('aria', { role, state: detail })}
      className={`flex items-center rounded-lg border p-1.5 transition-colors ${TONES[tone]}`}
    >
      <Icon size={17} aria-hidden="true" />
    </Link>
  )
}
