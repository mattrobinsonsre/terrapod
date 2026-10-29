'use client'

import { Fragment, Suspense, useEffect, useRef, useState, useCallback, useMemo, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { useTranslations } from 'next-intl'
import { engineWord } from '@/lib/phase-vocabulary'
import Link from 'next/link'
import { useFormat } from '@/lib/format'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { ConnectionStatus } from '@/components/connection-status'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { WorkspaceStatusBadges } from '@/components/workspace-status-badges'
import { StatChip } from '@/components/stat-chip'
import { getAuthState } from '@/lib/auth'
import { apiFetch, fetchAllPages } from '@/lib/api'
import { useSortable } from '@/lib/use-sortable'
import { useWorkspaceListEvents } from '@/lib/use-workspace-list-events'
import {
  type ParsedFilter,
  hasLabelTerm,
  hasStatusTerm,
  HEALTH_ISSUE_STATUS,
  LOCKED_STATUS,
  matchWorkspace,
  parseFilterQuery,
  removeTerm,
  serializeFilter,
  toggleLabelTerm,
  toggleStatusTerm,
} from '@/lib/workspace-filter'
import { WORKSPACE_STATUSES, resolveStatus } from '@/lib/workspace-status'
import { defaultEngineVersion, versionToolFor } from '@/lib/engine-version'
import {
  CREATE_INPUT,
  FIELD_BY_ATTR,
  GROUPS,
  LABEL_KEY,
  dependencyMet,
  groupFields,
  toAttributes,
  type GroupId,
} from '@/components/workspace-fields'
import { Field, Group, type GroupCtx } from '@/components/workspace-field-groups'
import { LabelsEditor } from '@/components/labels-editor'
import {
  OidcAudiencesEditor,
  StringListEditor,
  sanitizeOidcAudiences,
  type OidcAudiences,
} from '@/components/template-editors'
import { useOidcAudienceDefaults } from '@/lib/use-oidc-audience-defaults'
import {
  formatCollectionLines,
  parseCollectionLines,
  type AnsibleCollection,
} from '@/lib/ansible-collections'
import {
  type GroupMode,
  type WorkspaceGroup,
  LOCAL_GROUP_KEY,
  buildWorkspaceTree,
  countWorkspaces,
  parseGroupParam,
  serializeGroupParam,
} from '@/lib/workspace-grouping'

interface LatestRun {
  id: string
  status: string
  'plan-only': boolean
  'created-at': string
}

interface HealthCondition {
  code: string
  severity: string
  title: string
  detail: string
}

interface Workspace {
  id: string
  attributes: {
    name: string
    'execution-mode': string
    'auto-apply': boolean
    'terraform-version': string
    // Which engine, not which binary — the status badge resolves its phase
    // vocabulary from this (#1407 §3). Optional: an older API does not send it,
    // and the helper falls back to Terraform's words rather than a raw key.
    engine?: string
    locked: boolean
    'resource-cpu': string
    'resource-memory': string
    'agent-pool-name': string | null
    'drift-detection-enabled': boolean
    'drift-status': string
    'drift-latest-run-id': string | null
    'state-diverged': boolean
    'vcs-last-error': string | null
    'health-conditions': HealthCondition[]
    'lifecycle-state': 'active' | 'pending_deletion' | 'archived'
    'lifecycle-reason': string
    'latest-run': LatestRun | null
    'working-directory'?: string
    'vcs-repo-url'?: string
    'created-at': string
    labels?: Record<string, string> | null
  }
}

function WorkspaceGroupRows({
  groups,
  collapsedGroups,
  toggleGroup,
  pathPrefix,
  parsedFilter: filter,
  setFilterInput,
  depth = 0,
}: {
  groups: WorkspaceGroup[]
  collapsedGroups: Set<string>
  toggleGroup: (path: string) => void
  pathPrefix: string
  parsedFilter: ParsedFilter
  setFilterInput: (v: string) => void
  depth?: number
}) {
  const t = useTranslations('workspaces')
  return (
    <>
      {groups.map(group => {
        const fullPath = pathPrefix ? `${pathPrefix}/${group.key}` : group.key
        const isCollapsed = collapsedGroups.has(fullPath)
        const count = countWorkspaces(group)

        return (
          <Fragment key={fullPath}>
            <tr className="hover:bg-slate-700/20 transition-colors">
              <td className="px-4 py-2" colSpan={6}>
                <button
                  type="button"
                  aria-expanded={!isCollapsed}
                  onClick={() => toggleGroup(fullPath)}
                  onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleGroup(fullPath) } }}
                  className="flex items-center gap-2 w-full"
                  style={{ paddingLeft: `${depth * 16}px` }}
                >
                  <svg
                    className={`w-3 h-3 text-slate-500 transition-transform ${isCollapsed ? '' : 'rotate-90'}`}
                    fill="none" stroke="currentColor" viewBox="0 0 24 24"
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                  </svg>
                  <span className="text-sm text-slate-400">{group.key === LOCAL_GROUP_KEY ? t('group.local') : group.label}/</span>
                  <span className="text-xs text-slate-600 bg-slate-800 px-1.5 py-0.5 rounded-full">{count}</span>
                </button>
              </td>
            </tr>

            {!isCollapsed && group.workspaces.map(item => {
              const ws = item.workspace as Workspace
              const { def, runId } = resolveStatus(ws)
              return (
                <tr key={item.id} className="hover:bg-slate-700/20 transition-colors border-t border-slate-700/30">
                  <td className="px-4 py-3">
                    <div className="flex flex-col gap-1.5" style={{ paddingLeft: `${(depth + 1) * 16}px` }}>
                      <Link
                        href={`/workspaces/${item.id}`}
                        className="text-sm font-medium text-brand-400 hover:text-brand-300"
                      >
                        {item.name}
                        <EngineBadge engine={ws.attributes.engine} />
                      </Link>
                      {ws.attributes.labels && Object.keys(ws.attributes.labels).length > 0 && (
                        <div className="flex flex-wrap gap-1">
                          {Object.entries(ws.attributes.labels as Record<string, string>).map(([k, v]) => {
                            const active = hasLabelTerm(filter, k, v)
                            return (
                              <button
                                key={`${k}=${v}`}
                                type="button"
                                onClick={() => setFilterInput(serializeFilter(toggleLabelTerm(filter, k, v)))}
                                title={active ? t('row.removeLabelFilter') : t('row.addLabelFilter')}
                                className={
                                  'inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-mono transition-colors ' +
                                  (active
                                    ? 'bg-brand-700/60 text-brand-100 hover:bg-brand-700'
                                    : 'bg-slate-700/40 text-slate-400 hover:bg-slate-700/80 hover:text-slate-200')
                                }
                              >
                                <span className="text-slate-500">{k}:</span>
                                <span className="ml-0.5">{v}</span>
                              </button>
                            )
                          })}
                        </div>
                      )}
                      <div className="lg:hidden" data-testid="ws-row-status-mobile">
                        <WorkspaceStatusBadges
                          workspaceId={ws.id}
                          engine={ws.attributes.engine}
                          def={def}
                          runId={runId}
                          lifecycleState={ws.attributes['lifecycle-state']}
                        />
                      </div>
                    </div>
                  </td>
                  <td className="px-4 py-3 hidden sm:table-cell">
                    <span className="text-xs text-slate-400">{ws.attributes['execution-mode']}</span>
                  </td>
                  <td className="px-4 py-3 hidden md:table-cell">
                    <span className="text-xs text-slate-400">{ws.attributes['agent-pool-name'] || '—'}</span>
                  </td>
                  <td className="px-4 py-3 hidden lg:table-cell">
                    <span className="text-xs text-slate-400">{t('table.resourcesValue', { cpu: ws.attributes['resource-cpu'], memory: ws.attributes['resource-memory'] })}</span>
                  </td>
                  <td className="px-4 py-3 hidden lg:table-cell">
                    <WorkspaceStatusBadges
                      workspaceId={ws.id}
                      engine={ws.attributes.engine}
                      def={def}
                      runId={runId}
                      lifecycleState={ws.attributes['lifecycle-state']}
                    />
                  </td>
                  <td className="px-4 py-3 hidden xl:table-cell">
                    <span className="text-xs text-slate-500">
                      {ws.attributes['created-at'] ? new Date(ws.attributes['created-at']).toLocaleDateString() : ''}
                    </span>
                  </td>
                </tr>
              )
            })}

            {!isCollapsed && group.children.length > 0 && (
              <WorkspaceGroupRows
                groups={group.children}
                collapsedGroups={collapsedGroups}
                toggleGroup={toggleGroup}
                pathPrefix={fullPath}
                parsedFilter={filter}
                setFilterInput={setFilterInput}
                depth={depth + 1}
              />
            )}
          </Fragment>
        )
      })}
    </>
  )
}

/**
 * The create form's initial values, keyed as the API names each attribute.
 *
 * ONE object rather than ~50 `useState` hooks, because the form now renders the
 * shared field set (#2253) and the groups hand back `(attr, value)` pairs. It
 * is also the shape `toAttributes` consumes, so nothing translates between a
 * control and the wire.
 *
 * WHAT IS SEEDED, and why it is not simply "everything empty": an absent value
 * renders a checkbox unchecked and a `<select>` showing its first option, so a
 * field whose column default is anything other than false/empty would have the
 * form STATE THE OPPOSITE of what it is about to create. The values below are
 * each read off `Workspace.__table__` — `plan-apply-enabled` defaults true, and
 * every select here has a real default — so the form shows what the API will
 * store. Fields whose default is false or empty are deliberately absent:
 * `toAttributes` drops `undefined`, so an untouched field is not sent at all
 * and the API applies its own default, which keeps the payload to what the
 * operator actually saw.
 *
 * `engine` is a parameter because switching engine re-seeds the version: a
 * Pulumi workspace pins the 3.x Pulumi CLI where Terraform's are 1.x, so
 * carrying one across would submit a version that does not exist (#1559).
 */
function createDefaults(engine: string): Record<string, unknown> {
  return {
    // identity
    name: '',
    engine,
    // execution
    'execution-mode': 'local',
    'execution-backend': 'tofu',
    'engine-version': defaultEngineVersion(engine),
    'working-directory': '',
    'auto-apply-mode': 'never',
    parallelism: 10,
    'resource-cpu': '1',
    'resource-memory': '2Gi',
    // operations — the one boolean pair whose defaults differ
    'plan-apply-enabled': true,
    // pools
    'agent-pool-ids': [],
    // vcs
    'vcs-connection-id': '',
    'vcs-repo-url': '',
    'vcs-branch': '',
    'vcs-workflow': 'merge_then_apply',
    'auto-merge-strategy': 'merge',
    // governance
    'drift-detection-interval-seconds': 86400,
    // ai
    'ai-summary-mode': 'default',
    'ai-policy-mode': 'default',
    // security scanning
    'security-scan-engine': 'checkov',
    'security-scan-enforcement': 'advisory',
    'security-scan-severity-threshold': 'high',
    // metadata / cloud identity — the two map-valued attributes, whose editors
    // want a real object rather than undefined
    labels: {},
    'oidc-audiences': {},
  }
}

/**
 * The attributes the create form does not offer, with the reason.
 *
 * `owner-email` is accepted on PATCH and NOT by the create path, which sets
 * `owner_email=user.email` — the creator is the owner. Rendering it would take
 * an operator's input and silently ignore it, which is worse than its absence,
 * and ownership grants workspace admin so it is a platform-admin change made
 * deliberately afterwards rather than a field on a create form.
 */
const CREATE_OMITS: ReadonlySet<string> = new Set(['owner-email'])

/**
 * Whether a group collapses into a `<details>` in the create form.
 *
 * DERIVED from the shared table rather than listed here: a group whose every
 * rendered field is `advanced` holds nothing needed to create a workspace, so
 * it collapses — and `Group` puts what it holds in the summary, so a collapsed
 * group still cannot conceal a value. Deriving it means a field changing its
 * `advanced` flag, or a group gaining a plain field, moves this with it instead
 * of leaving a stale list behind.
 */
function groupIsAdvanced(group: GroupId, engine: string): boolean {
  const rendered = groupFields(group, engine).filter((f) => !CREATE_OMITS.has(f.attr))
  return rendered.length > 0 && rendered.every((f) => f.advanced)
}

// Wrapped in <Suspense> at the bottom — `useSearchParams()` triggers Next.js's
// CSR bailout, and without a boundary the whole page fails to build statically.
function WorkspacesPageInner() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const t = useTranslations('workspaces')
  const ts = useTranslations('status')
  const tMode = useTranslations('common.autoApplyMode')
  // Root namespace, for the per-engine phase vocabulary (#1911). The create
  // form carries an engine selector, so the engine is in view before the
  // workspace exists — and the auto-apply hint sits directly beneath it.
  const tPhase = useTranslations()
  // The create form's field labels and group headings come from the detail
  // page's namespace, which already carries them translated in every catalogue
  // — so the form inherits ~50 labels rather than needing them translated 33
  // times. `workspace-field-groups.tsx` records why the namespace keeps the
  // detail page's name.
  const tDetail = useTranslations('workspaceDetail')
  // The deployment's audience catalogue, so an inherited entry reads as
  // inherited in the create form too. Fail-soft: a failed probe degrades to an
  // empty catalogue rather than a broken editor.
  const oidcDefaults = useOidcAudienceDefaults()
  const fmt = useFormat()
  // Translate a filter-suggestion's category hint (its stable English key stays
  // the internal value; only the displayed label is localized).
  const hintLabel = (hint: string) => {
    switch (hint) {
      case 'status': return t('filter.hintStatus')
      case 'health': return t('filter.hintHealth')
      case 'label': return t('filter.hintLabel')
      case 'label key': return t('filter.hintLabelKey')
      default: return t('filter.hintName')
    }
  }
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Filter input. Tokens are space-separated; bare words match the workspace
  // name (case-insensitive substring), `key:value` / `key=value` match a
  // label exactly, and `key:` / `key=` matches any workspace that has the
  // label key set. Mirrored to the URL via ?q=… so refresh + share work.
  const [filterInput, setFilterInput] = useState(searchParams.get('q') || '')
  const parsedFilter = useMemo(() => parseFilterQuery(filterInput), [filterInput])
  // Engine filter (#1555): a dropdown rather than an `engine:` term, which would
  // collide with a label of that name. Offered only when more than one engine is on.
  const [engineFilter, setEngineFilter] = useState(searchParams.get('engine') || '')

  // Grouping mode — URL param takes priority, localStorage as fallback.
  const [groupMode, setGroupModeState] = useState<GroupMode>(() => {
    const fromUrl = searchParams.get('group')
    if (fromUrl) return parseGroupParam(fromUrl)
    if (typeof window !== 'undefined') {
      return parseGroupParam(localStorage.getItem('terrapod:workspace-group'))
    }
    return 'flat'
  })

  // Collapsed groups state — persisted in localStorage.
  const [collapsedGroups, setCollapsedGroupsRaw] = useState<Set<string>>(() => {
    if (typeof window !== 'undefined') {
      const stored = localStorage.getItem('terrapod:workspace-collapsed')
      if (stored) {
        try { return new Set(JSON.parse(stored)) } catch { /* ignore */ }
      }
    }
    return new Set()
  })
  const setCollapsedGroups = useCallback((update: Set<string> | ((prev: Set<string>) => Set<string>)) => {
    setCollapsedGroupsRaw(prev => {
      const next = typeof update === 'function' ? update(prev) : update
      if (typeof window !== 'undefined') {
        localStorage.setItem('terrapod:workspace-collapsed', JSON.stringify([...next]))
      }
      return next
    })
  }, [])

  const toggleGroup = useCallback((path: string) => {
    setCollapsedGroups(prev => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }, [setCollapsedGroups])

  const setGroupMode = useCallback((mode: GroupMode) => {
    setGroupModeState(mode)
    setCollapsedGroups(new Set())
    const serialized = serializeGroupParam(mode)
    if (typeof window !== 'undefined') {
      if (serialized) localStorage.setItem('terrapod:workspace-group', serialized)
      else localStorage.removeItem('terrapod:workspace-group')
    }
    const q = serializeFilter(parsedFilter)
    const params = new URLSearchParams()
    if (q) params.set('q', q)
    if (serialized) params.set('group', serialized)
    const qs = params.toString()
    router.replace(qs ? `/workspaces?${qs}` : '/workspaces', { scroll: false })
  }, [parsedFilter, router, setCollapsedGroups])

  // Group by dropdown — same outside-click + Escape pattern as Status/Label.
  const [groupMenuOpen, setGroupMenuOpen] = useState(false)
  const groupMenuRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!groupMenuOpen) return
    const onClick = (e: MouseEvent) => {
      if (groupMenuRef.current && !groupMenuRef.current.contains(e.target as Node)) {
        setGroupMenuOpen(false)
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setGroupMenuOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [groupMenuOpen])

  // Status dropdown state. Closes on outside-click and Escape — same pattern
  // used for the run-actions menu so the page feels consistent.
  const [statusMenuOpen, setStatusMenuOpen] = useState(false)
  const statusMenuRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!statusMenuOpen) return
    const onClick = (e: MouseEvent) => {
      if (statusMenuRef.current && !statusMenuRef.current.contains(e.target as Node)) {
        setStatusMenuOpen(false)
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setStatusMenuOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [statusMenuOpen])

  // Label dropdown — same outside-click + Escape pattern as Status. The
  // dropdown is a two-level picker: first pick a label key, then a value.
  const [labelMenuOpen, setLabelMenuOpen] = useState(false)
  const [labelMenuKey, setLabelMenuKey] = useState<string | null>(null)
  const labelMenuRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!labelMenuOpen) return
    const onClick = (e: MouseEvent) => {
      if (labelMenuRef.current && !labelMenuRef.current.contains(e.target as Node)) {
        setLabelMenuOpen(false)
        setLabelMenuKey(null)
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (labelMenuKey !== null) setLabelMenuKey(null)
        else setLabelMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', onClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [labelMenuOpen, labelMenuKey])

  // Inline filter typeahead — AWS-style faceted suggestions popped under the
  // free-text input as you type, while still letting you type anything. The
  // dropdown suggests `key:value` labels, `status:…`, label keys (to drill),
  // and matching workspace names; clicking one inserts the chip.
  const [suggestOpen, setSuggestOpen] = useState(false)
  // -1 = nothing highlighted. A suggestion is only applied on an explicit
  // pick (click, or arrow-key to highlight + Enter); a bare Enter keeps the
  // free text the user typed.
  const [suggestIndex, setSuggestIndex] = useState(-1)
  const filterInputRef = useRef<HTMLInputElement>(null)
  const suggestWrapRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!suggestOpen) return
    const onClick = (e: MouseEvent) => {
      if (suggestWrapRef.current && !suggestWrapRef.current.contains(e.target as Node)) {
        setSuggestOpen(false)
      }
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [suggestOpen])

  // Sync the input to the URL whenever the parsed filter changes — debounced.
  //
  // Each `router.replace` triggers a Next.js RSC prefetch for the new URL.
  // Firing per-keystroke (e.g. typing "repo:data-pipelines") cascades
  // ~20 prefetches in a few hundred ms, overwhelms Next's dev server with
  // 503s, and can leave the address bar stuck on an early keystroke when
  // a later prefetch wins the race. We debounce to a single replace once
  // typing settles.
  //
  // Dedup via `lastSyncedQueryRef` — we track the last query string we
  // wrote and skip redundant `router.replace` calls. We deliberately do
  // NOT compare against `searchParams.get('q')`: `useSearchParams()` is
  // updated by Next.js asynchronously after `router.replace`, so its
  // value in the effect closure can be one render behind the URL we
  // just wrote.
  //
  // Initialised to the URL's current `q` so that mounting on a page
  // already loaded with `?q=...` (refresh, shared link) doesn't fire a
  // redundant `router.replace` over the unchanged URL on first render.
  const lastSyncedQueryRef = useRef<string | null>(`${searchParams.get('q') || ''}|${searchParams.get('engine') || ''}`)
  useEffect(() => {
    const serialized = serializeFilter(parsedFilter)
    const syncKey = `${serialized}|${engineFilter}`
    if (lastSyncedQueryRef.current === syncKey) return
    const timer = setTimeout(() => {
      lastSyncedQueryRef.current = syncKey
      const params = new URLSearchParams()
      if (serialized) params.set('q', serialized)
      if (engineFilter) params.set('engine', engineFilter)
      const groupSerialized = serializeGroupParam(groupMode)
      if (groupSerialized) params.set('group', groupSerialized)
      const qs = params.toString()
      router.replace(qs ? `/workspaces?${qs}` : '/workspaces', { scroll: false })
    }, 250)
    return () => clearTimeout(timer)
  }, [parsedFilter, engineFilter, groupMode, router])

  const filteredWorkspaces = useMemo(() => {
    const byEngine = engineFilter
      ? workspaces.filter(ws => (ws.attributes.engine || 'terraform') === engineFilter)
      : workspaces
    if (parsedFilter.terms.length === 0) return byEngine
    return byEngine.filter(ws => {
      // Status terms need the resolved status; pass it conditionally so
      // workspaces without a status (— display) don't accidentally match
      // an empty `status:` predicate.
      return matchWorkspace(ws, parsedFilter, resolveStatus(ws).def?.filter ?? undefined)
    })
  }, [workspaces, parsedFilter, engineFilter])


  // Create form
  const [showCreate, setShowCreate] = useState(false)
  // ONE values object, keyed by API attribute, in place of the thirteen `new*`
  // hooks this form used to carry (#2253). The thirteen were the whole problem:
  // a hook per control meant adding a field cost a hook, a reset line and a
  // payload line, so the form stopped at 13 of the ~50 attributes create
  // accepts and the rest were reachable only by creating a workspace and then
  // editing it.
  const [values, setValues] = useState<Record<string, unknown>>(() =>
    createDefaults('terraform'),
  )
  const setValue = useCallback((attr: string, value: unknown) => {
    setValues((prev) => ({ ...prev, [attr]: value }))
  }, [])
  // Which engine, not which binary — `execution-backend` picks tofu vs
  // terraform *within* the Terraform engine. Workspaces are created here, with
  // the provider, or via the API; never by an engine's own CLI (#1535).
  const engine = String(values.engine ?? 'terraform')
  // The hint under the auto-apply selector describes a phase, so it follows the
  // engine the form is set to (#1911). The engine has no unset state — it is
  // seeded to Terraform, the <select> is controlled with no placeholder option,
  // and on a single-engine deployment the selector is not rendered at all — so
  // a Terraform-only install and an untouched form both read Terraform's
  // wording without needing a special case. The four MODE values beside it stay
  // put: they render the literal wire values (`never`, `always`, `create`,
  // `create/update`), which are identifiers, not prose.
  const phaseWord = (name: string) => engineWord(tPhase, engine, name)
  const [creating, setCreating] = useState(false)

  // Version suggestions. Keyed on the *tool* the version pins, which the engine
  // decides — not on the execution backend, which only means anything within the
  // Terraform engine (#1559).
  const [versionSuggestions, setVersionSuggestions] = useState<string[]>([])
  const [versionsTool, setVersionsTool] = useState('')

  // VCS connections
  const [vcsConnections, setVcsConnections] = useState<{ id: string; attributes: { name: string; provider: string } }[]>([])
  const [vcsConnectionsLoaded, setVcsConnectionsLoaded] = useState(false)

  // Agent pools
  const [agentPools, setAgentPools] = useState<{ id: string; attributes: { name: string } }[]>([])
  const [agentPoolsLoaded, setAgentPoolsLoaded] = useState(false)

  // Engines this build can run (#1555). The picker appears only when there is a
  // choice to make, which is still the right condition — but note it is now keyed
  // on what the BUILD contains rather than on what an operator enabled, because
  // the engine on/off switch is gone (#1986). So a Terraform-only deployment does
  // see the picker, defaulted to Terraform. There is deliberately nothing
  // deployment-specific left to key on: keying it on "has a Pulumi workspace
  // already" would make the first one impossible to create.
  const [engines, setEngines] = useState<string[]>(['terraform'])
  useEffect(() => {
    apiFetch('/api/v1/engines')
      .then(res => (res.ok ? res.json() : { data: [] }))
      .then((d: { data?: { id: string }[] }) => {
        const ids = (d.data || []).map(e => e.id)
        if (ids.length > 0) setEngines(ids)
      })
      .catch(() => {})
  }, [])
  const engineName = (e: string) =>
    e === 'pulumi' ? t('form.enginePulumi') : e === 'terraform' ? t('form.engineTerraform') : e

  type WsSortKey = 'name' | 'mode' | 'pool' | 'resources' | 'status' | 'created'

  // resolveStatus + WORKSPACE_STATUSES are imported from workspace-status.ts
  // — single source of truth for both the row pill and the filter dropdown.

  // Per-status counts on the unfiltered workspace list, memoised so the
  // dropdown render is a constant lookup. Recomputes only when the
  // workspace data changes.
  const statusCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const ws of workspaces) {
      const f = resolveStatus(ws).def?.filter
      if (f) counts[f] = (counts[f] || 0) + 1
    }
    return counts
  }, [workspaces])

  // "Health issues" is ONE control rendered two ways (#719): the desktop
  // stat-grid card and the compact chip on the mobile filter row. Compute the
  // count + toggle once so both forms stay in sync.
  const withConditions = useMemo(
    () => workspaces.filter(ws => (ws.attributes['health-conditions'] || []).length > 0).length,
    [workspaces],
  )
  const healthFilterActive = hasStatusTerm(parsedFilter, HEALTH_ISSUE_STATUS)
  const toggleHealth = () =>
    setFilterInput(serializeFilter(toggleStatusTerm(parsedFilter, HEALTH_ISSUE_STATUS)))
  const lockedFilterActive = hasStatusTerm(parsedFilter, LOCKED_STATUS)
  const toggleLocked = () =>
    setFilterInput(serializeFilter(toggleStatusTerm(parsedFilter, LOCKED_STATUS)))

  // Distinct labels across the unfiltered list: key → sorted unique values
  // with workspace counts. Drives the two-level "+ Label" picker. Computed
  // once per workspace-list change so the menu render stays cheap.
  const labelIndex = useMemo(() => {
    const idx = new Map<string, Map<string, number>>()
    for (const ws of workspaces) {
      const labels = ws.attributes.labels || {}
      for (const [k, v] of Object.entries(labels)) {
        let values = idx.get(k)
        if (!values) {
          values = new Map<string, number>()
          idx.set(k, values)
        }
        values.set(v, (values.get(v) || 0) + 1)
      }
    }
    return idx
  }, [workspaces])
  const sortedLabelKeys = useMemo(
    () => Array.from(labelIndex.keys()).sort(),
    [labelIndex],
  )

  // The token being typed = text after the last space (filter tokens are
  // space-separated). Suggestions are computed against just that token, so
  // earlier chips in the box are left untouched.
  const currentToken = useMemo(() => {
    const parts = filterInput.split(' ')
    return parts[parts.length - 1]
  }, [filterInput])

  interface FilterSuggestion {
    kind: 'label' | 'label-key' | 'status' | 'name'
    insert: string
    display: string
    hint: string
    count?: number
    dot?: string
  }

  const suggestions = useMemo<FilterSuggestion[]>(() => {
    const tok = currentToken.trim()
    if (!tok) return []
    const lower = tok.toLowerCase()
    const out: FilterSuggestion[] = []
    // Aggregate health-issues count for the `status:unhealthy` suggestion.
    const unhealthyCount = workspaces.filter(
      ws => (ws.attributes['health-conditions'] || []).length > 0,
    ).length
    const sepIdx = tok.search(/[:=]/)
    if (sepIdx >= 0) {
      // `key:partial` → suggest values for that key.
      const key = tok.slice(0, sepIdx)
      const partial = tok.slice(sepIdx + 1).toLowerCase()
      if (key === 'status') {
        for (const s of WORKSPACE_STATUSES) {
          if (!partial || s.filter.toLowerCase().includes(partial) || s.label.toLowerCase().includes(partial))
            out.push({ kind: 'status', insert: `status:${s.filter}`, display: `status:${s.filter}`, hint: 'status', dot: s.dot, count: statusCounts[s.filter] || 0 })
        }
        if (unhealthyCount > 0 && (!partial || HEALTH_ISSUE_STATUS.includes(partial) || 'health issues'.includes(partial)))
          out.push({ kind: 'status', insert: `status:${HEALTH_ISSUE_STATUS}`, display: `status:${HEALTH_ISSUE_STATUS}`, hint: 'health', dot: 'bg-red-400', count: unhealthyCount })
      } else {
        const values = labelIndex.get(key)
        if (values) {
          for (const [v, c] of values) {
            if (!partial || v.toLowerCase().includes(partial))
              out.push({ kind: 'label', insert: `${key}:${v}`, display: `${key}:${v}`, hint: 'label', count: c })
          }
        }
      }
    } else {
      // Bare token → match label keys (drill), key:value pairs, statuses, names.
      for (const k of sortedLabelKeys) {
        if (k.toLowerCase().includes(lower))
          out.push({ kind: 'label-key', insert: `${k}:`, display: `${k}:`, hint: 'label key' })
      }
      for (const k of sortedLabelKeys) {
        for (const [v, c] of labelIndex.get(k)!) {
          if (v.toLowerCase().includes(lower) || k.toLowerCase().includes(lower))
            out.push({ kind: 'label', insert: `${k}:${v}`, display: `${k}:${v}`, hint: 'label', count: c })
        }
      }
      for (const s of WORKSPACE_STATUSES) {
        if (s.filter.toLowerCase().includes(lower) || s.label.toLowerCase().includes(lower))
          out.push({ kind: 'status', insert: `status:${s.filter}`, display: `status:${s.filter}`, hint: 'status', dot: s.dot, count: statusCounts[s.filter] || 0 })
      }
      if (unhealthyCount > 0 && (HEALTH_ISSUE_STATUS.includes(lower) || 'health'.includes(lower) || 'issues'.includes(lower)))
        out.push({ kind: 'status', insert: `status:${HEALTH_ISSUE_STATUS}`, display: `status:${HEALTH_ISSUE_STATUS}`, hint: 'health', dot: 'bg-red-400', count: unhealthyCount })
      for (const ws of workspaces) {
        const n = ws.attributes.name
        if (n.toLowerCase().includes(lower))
          out.push({ kind: 'name', insert: n, display: n, hint: 'name' })
      }
    }
    // Dedup by inserted text; cap so the menu stays usable.
    const seen = new Set<string>()
    return out.filter(s => (seen.has(s.insert) ? false : (seen.add(s.insert), true))).slice(0, 12)
  }, [currentToken, sortedLabelKeys, labelIndex, statusCounts, workspaces])

  function applyFilterSuggestion(s: FilterSuggestion) {
    const parts = filterInput.split(' ')
    parts[parts.length - 1] = s.insert
    // A label-key suggestion (`env:`) leaves the cursor mid-token so you keep
    // typing/picking the value; everything else completes the chip + a space.
    const drill = s.kind === 'label-key'
    setFilterInput(parts.join(' ') + (drill ? '' : ' '))
    setSuggestIndex(-1)
    setSuggestOpen(drill)
    filterInputRef.current?.focus()
  }

  function onFilterKeyDown(e: ReactKeyboardEvent<HTMLInputElement>) {
    if (e.key === 'ArrowDown' && (!suggestOpen || suggestions.length === 0)) {
      if (currentToken.trim()) { setSuggestOpen(true); setSuggestIndex(0) }
      return
    }
    if (!suggestOpen || suggestions.length === 0) return
    if (e.key === 'ArrowDown') { e.preventDefault(); setSuggestIndex(i => (i + 1) % suggestions.length) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSuggestIndex(i => (i <= 0 ? suggestions.length - 1 : i - 1)) }
    else if (e.key === 'Enter') {
      // Apply ONLY a suggestion the user explicitly highlighted with the
      // arrow keys. A bare Enter (nothing highlighted) keeps the typed text.
      if (suggestIndex >= 0) { e.preventDefault(); applyFilterSuggestion(suggestions[suggestIndex]); }
      else setSuggestOpen(false)
    }
    else if (e.key === 'Escape') { e.preventDefault(); setSuggestOpen(false) }
  }

  const { sortedItems: sortedWorkspaces, sortState, toggleSort } = useSortable<Workspace, WsSortKey>(
    filteredWorkspaces, 'name', 'asc',
    useCallback((item: Workspace, key: WsSortKey) => {
      switch (key) {
        case 'name': return item.attributes.name
        case 'mode': return item.attributes['execution-mode']
        case 'pool': return item.attributes['agent-pool-name'] || ''
        case 'resources': return item.attributes['resource-cpu']
        case 'status': return resolveStatus(item).def?.label ?? '\u2014'
        case 'created': return item.attributes['created-at']
      }
    }, []),
  )

  const workspaceTree = useMemo(
    () => buildWorkspaceTree(sortedWorkspaces, groupMode),
    [sortedWorkspaces, groupMode]
  )

  const allGroupPaths = useMemo(() => {
    const paths = new Set<string>()
    const collect = (groups: WorkspaceGroup[], prefix: string) => {
      for (const g of groups) {
        const p = prefix ? `${prefix}/${g.key}` : g.key
        paths.add(p)
        collect(g.children, p)
      }
    }
    collect(workspaceTree, '')
    return paths
  }, [workspaceTree])

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadWorkspaces()
  // eslint-disable-next-line react-hooks/exhaustive-deps -- initial mount load; the loader is a hoisted function declaration recreated each render, so depending on it would re-fetch on every render
  }, [router])

  // Real-time workspace list updates via SSE. Always-on (not gated on
  // workspaces.length) so the FIRST workspace created from another tab / the
  // CLI / a VCS push live-appears on an empty org instead of requiring a
  // manual refresh — the empty state is exactly where live-update matters most.
  const { connected: sseConnected } = useWorkspaceListEvents(true, useCallback(() => {
    loadWorkspaces()
  // eslint-disable-next-line react-hooks/exhaustive-deps -- the callback is deliberately referentially stable — adding the loader would re-create it every render and churn the SSE subscription
  }, []))

  // Load VCS connections and agent pools when form opens
  useEffect(() => {
    if (!showCreate) return
    if (!vcsConnectionsLoaded) {
      fetchAllPages<{ id: string; attributes: { name: string; provider: string } }>('/api/terrapod/v1/vcs-connections')
        .then(conns => { setVcsConnections(conns); setVcsConnectionsLoaded(true) })
        .catch(() => {})
    }
    if (!agentPoolsLoaded) {
      fetchAllPages<{ id: string; attributes: { name: string } }>('/api/terrapod/v1/agent-pools')
        .then(pools => { setAgentPools(pools); setAgentPoolsLoaded(true) })
        .catch(() => {})
    }
  }, [showCreate, vcsConnectionsLoaded, agentPoolsLoaded])

  // Fetch version suggestions when the tool the version pins changes and the
  // form is open. A Pulumi workspace pins the Pulumi CLI; every other engine
  // pins whichever Terraform-family binary its execution backend names.
  const versionTool = versionToolFor(engine, String(values['execution-backend'] ?? 'tofu'))
  useEffect(() => {
    if (!showCreate || versionTool === versionsTool) return
    apiFetch(`/api/terrapod/v1/binary-cache/versions?tool=${versionTool}`)
      .then(res => res.ok ? res.json() : { data: [] })
      .then(data => {
        setVersionSuggestions(data.data || [])
        setVersionsTool(versionTool)
      })
      .catch(() => {})
  }, [showCreate, versionTool, versionsTool])

  // Switching engine invalidates whatever version is in the box: Pulumi's
  // releases are 3.x and Terraform's are 1.x, so carrying one across would
  // submit a version that does not exist on the engine now selected.
  //
  // Returns `prev` untouched when the version already matches, so the effect's
  // run on mount — where it would otherwise write the value it just seeded — is
  // not a second render.
  useEffect(() => {
    const seeded = defaultEngineVersion(engine)
    setValues((prev) =>
      prev['engine-version'] === seeded ? prev : { ...prev, 'engine-version': seeded },
    )
  }, [engine])

  async function loadWorkspaces() {
    try {
      // Fetch the whole list (paging through internally) and filter/sort
      // client-side — the workspace list is deliberately not page-through UI.
      const items = await fetchAllPages<Workspace>('/api/v1/workspaces')
      setWorkspaces(items)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('loadFailed'))
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    try {
      // ONE payload builder, shared with the Configuration tab (#2253). It is
      // what keeps the two rules below out of this function, each of which is a
      // 422 waiting to happen and neither visible from a field's declaration:
      // never send an alias beside its canonical key (the API derives
      // `auto-apply` from `auto-apply-mode`, and refuses `engine-version`
      // alongside a disagreeing `terraform-version`), and `vcs-connection` is a
      // RELATIONSHIP rather than an attribute, so it comes back under its own
      // key to be placed below instead of being dropped into `attributes` where
      // the API would ignore it.
      //
      // `create: true` is what emits `engine`: the native create route reads it
      // off the body and no PATCH path accepts it, so omitting it here would
      // make every workspace Terraform and leave Pulumi uncreatable from the UI
      // — the #1535 defect over again.
      //
      // `include` sends only what the form actually SHOWED, on the same two
      // conditions the groups render on: the field applies to this engine, and
      // its gating boolean is on. So turning configure on, typing a playbook
      // and turning it back off does not quietly ship the playbook, and a
      // Pulumi workspace is not sent a Terraform-family execution backend it
      // has no use for. Each omitted column takes its own default, which is
      // what the operator saw in place of the field.
      const { attributes, vcsConnectionId } = toAttributes(values, {
        create: true,
        include: (attr) => {
          const spec = FIELD_BY_ATTR[attr]
          if (!spec) return true
          if (spec.engines && !spec.engines.includes(engine)) return false
          return dependencyMet(spec, values)
        },
      })
      // The native surface, because it is the only one that can carry an engine
      // — the TFE-compatible route is Terraform-only by design. The web UI ships
      // in the same Helm release as the API, so there is no version skew to keep
      // it on the older path for.
      const res = await apiFetch('/api/v1/workspaces', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'workspaces',
            attributes,
            ...(vcsConnectionId ? {
              relationships: {
                'vcs-connection': {
                  data: { id: vcsConnectionId, type: 'vcs-connections' },
                },
              },
            } : {}),
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `${t('createFailed')} (${res.status})`)
      }
      // One reset, where there were thirteen. A field added to the shared table
      // is cleared by this line without being named here — which is the other
      // half of why the old form stopped at thirteen controls.
      setValues(createDefaults('terraform'))
      setShowCreate(false)
      await loadWorkspaces()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('createFailed'))
    } finally {
      setCreating(false)
    }
  }

  // ── the create form's shared field set (#2253) ──────────────────────────
  //
  // What the groups need to render themselves. `onChange` is the page's
  // decision, which is the whole reason a group takes it: this form collects
  // everything for one POST, where the Configuration tab batches behind an
  // Edit/Save toggle and the AI and security cards PATCH on change.
  const createCtx: GroupCtx = {
    mode: 'create',
    values,
    onChange: setValue,
    engine,
    label: (attr) => tDetail(LABEL_KEY[attr]),
    hint: (attr) => createHint(attr),
    busy: creating,
  }

  const hintText = (node: ReactNode) => (
    <p className="mt-1 text-xs text-slate-500">{node}</p>
  )

  /** The explanatory line under a field, where one exists already translated. */
  function createHint(attr: string): ReactNode {
    switch (attr) {
      // The composed `project::stack` name is the one an operator must get
      // right before the browser will let the form submit at all, so its hint
      // is shown rather than left to the input's `title`.
      case 'name':
        return engine === 'pulumi' ? (
          <p className="mt-1 text-xs text-slate-400" data-testid="ws-pulumi-name-hint">
            {t.rich('form.pulumiNameHint', {
              code: (c) => <code className="bg-slate-700 px-1 rounded">{c}</code>,
            })}
          </p>
        ) : undefined
      case 'engine':
        return <p className="mt-1 text-xs text-slate-400">{t('form.engineHelp')}</p>
      case 'auto-apply-mode':
        return hintText(phaseWord('formAutoApplyModeHint'))
      case 'resource-cpu':
        return hintText(t('form.cpuHint'))
      case 'resource-memory':
        return hintText(t('form.memoryHint'))
      case 'working-directory':
        return hintText(t('form.workingDirectoryHint'))
      case 'pulumi-bind-plan':
        // t.rich: carries a <docs> link. The SECOND of the two rich hints,
        // and the one the browser did not reveal -- bind-plan renders for
        // Pulumi only and the form defaults to Terraform, so it would have
        // thrown for whoever first selected Pulumi. Found by the gate, not by
        // the demo, which is the argument for having both.
        return (
          <p className="mt-1 text-xs text-slate-400">
            {tDetail.rich('fields.bindPlanHelp', {
              docs: (chunks) => (
                <a
                  href="https://github.com/mattrobinsonsre/terrapod/blob/main/docs/pulumi-cli-surface.md"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-brand-400 hover:text-brand-300 underline"
                >
                  {chunks}
                </a>
              ),
            })}
          </p>
        )
      case 'plan-apply-enabled':
        return hintText(tDetail('fields.planApplyEnabledHint'))
      case 'configure-enabled':
        return hintText(tDetail('fields.configureEnabledHint'))
      case 'auto-configure-enabled':
        return hintText(tDetail('fields.autoConfigureEnabledHint'))
      case 'configure-after-apply':
        return hintText(tDetail('fields.configureAfterApplyHint'))
      case 'configure-all-hosts':
        return hintText(tDetail('fields.configureAllHostsHint'))
      case 'config-directory':
        return hintText(tDetail('fields.configDirectoryRootHint'))
      case 'inventory-paths':
        return hintText(tDetail('fields.inventoryPathsHint'))
      case 'dynamic-inventory-enabled':
        return hintText(tDetail('fields.dynamicInventoryEnabledHint'))
      case 'inventory-scripts-enabled':
        return hintText(tDetail('fields.inventoryScriptsEnabledHint'))
      case 'ansible-collections':
        return hintText(tDetail('fields.ansibleCollectionsHint'))
      case 'agent-pool-ids':
        return hintText(tDetail('fields.agentPoolsHint'))
      case 'allow-fork-pr-plans':
        return hintText(tDetail('allowForkPrPlans.hint'))
      case 'trigger-prefixes':
        return hintText(tDetail('fields.triggerPrefixesHint'))
      case 'drift-ignore-rules':
        // t.rich, NOT t: this hint is the ONE field hint carrying rich-text
        // tags (<star>, <dot>, <n>, <starIdx>, <docs>), and plain `t` throws
        // FORMATTING_ERROR per missing tag -- ten console errors and a
        // fallback string, which is how it shipped until the form was driven
        // in a browser. Components match the edit tab's rendering so the two
        // forms read identically.
        return (
          <p className="mt-1 text-xs text-slate-400">
            {tDetail.rich('fields.driftIgnoreRulesHint', {
              star: () => <code className="text-[10px] bg-slate-800 px-1">*</code>,
              dot: () => <code className="text-[10px] bg-slate-800 px-1">.</code>,
              n: () => <code className="text-[10px] bg-slate-800 px-1">[N]</code>,
              starIdx: () => <code className="text-[10px] bg-slate-800 px-1">[*]</code>,
              docs: (chunks) => (
                <a
                  href="https://github.com/mattrobinsonsre/terrapod/blob/main/docs/drift-ignore-rules.md"
                  target="_blank"
                  rel="noreferrer"
                  className="text-brand-400 hover:underline"
                >
                  {chunks}
                </a>
              ),
            })}
          </p>
        )
      case 'ai-summary-context':
        return hintText(tDetail('aiSummary.contextHint'))
      case 'ai-policy-mode':
        return hintText(tDetail('aiSummary.policyModeNote'))
      case 'oidc-audiences':
        return hintText(tDetail('fields.oidcAudiencesHint'))
      case 'debug-mode':
        return hintText(tDetail('debugMode.hint'))
      case 'slack-channel':
        return hintText(tDetail('slack.hint'))
      default:
        return undefined
    }
  }

  // Option sets. Every label is a `t(...)` call or a wire identifier read out
  // of an array, never a literal in the JSX below — a raw literal would be a
  // new entry in the hardcoded-string allowlist, which is the gate that keeps
  // untranslated copy out of the UI.
  const execModeOptions = [
    { value: 'local', label: tDetail('fields.execModeLocal') },
    { value: 'agent', label: tDetail('fields.execModeAgent') },
  ]
  // The two Terraform-family binaries. Product names, so they are not
  // translated in any catalogue; read from here rather than written into the
  // JSX so they are not new hardcoded literals either.
  const backendOptions = [
    { value: 'tofu', label: 'OpenTofu' },
    { value: 'terraform', label: 'Terraform' },
  ]
  // The four MODE values are wire identifiers, which is why they read the same
  // in every language — `common.autoApplyMode` renders them verbatim.
  const autoApplyOptions = [
    { value: 'never', label: tMode('never') },
    { value: 'always', label: tMode('always') },
    { value: 'create', label: tMode('create') },
    { value: 'create_update', label: tMode('createUpdate') },
  ]
  const vcsWorkflowOptions = [
    { value: 'merge_then_apply', label: tDetail('fields.vcsWorkflowMergeThenApply') },
    { value: 'apply_then_merge', label: tDetail('fields.vcsWorkflowApplyThenMerge') },
  ]
  // Git's own merge-method names, as the API spells them.
  const mergeStrategyOptions = ['merge', 'squash', 'rebase'].map((s) => ({
    value: s,
    label: s,
  }))
  const driftIntervalOptions = [
    { value: '3600', label: tDetail('intervals.1hour') },
    { value: '14400', label: tDetail('intervals.4hours') },
    { value: '43200', label: tDetail('intervals.12hours') },
    { value: '86400', label: tDetail('intervals.24hours') },
    { value: '172800', label: tDetail('intervals.48hours') },
    { value: '259200', label: tDetail('intervals.3days') },
    { value: '604800', label: tDetail('intervals.7days') },
  ]
  const aiModeOptions = [
    { value: 'default', label: tDetail('aiSummary.modeDefault') },
    { value: 'enabled', label: tDetail('aiSummary.modeEnabled') },
    { value: 'disabled', label: tDetail('aiSummary.modeDisabled') },
  ]
  const scanEnforcementOptions = [
    { value: 'off', label: tDetail('securityScan.enforcementOff') },
    { value: 'advisory', label: tDetail('securityScan.enforcementAdvisory') },
    { value: 'enforced', label: tDetail('securityScan.enforcementEnforced') },
  ]
  const scanEngineOptions = [
    { value: 'checkov', label: tDetail('securityScan.engineCheckov') },
    { value: 'trivy', label: tDetail('securityScan.engineTrivy') },
    { value: 'both', label: tDetail('securityScan.engineBoth') },
  ]
  const scanThresholdOptions = [
    { value: 'critical', label: tDetail('securityScan.thresholdCritical') },
    { value: 'high', label: tDetail('securityScan.thresholdHigh') },
    { value: 'medium', label: tDetail('securityScan.thresholdMedium') },
    { value: 'low', label: tDetail('securityScan.thresholdLow') },
  ]

  /** A list-valued attribute's current value, for the row editors. */
  const listOf = (attr: string): string[] => {
    const v = values[attr]
    return Array.isArray(v) ? (v as unknown[]).map(String) : []
  }

  /**
   * The groups' contents, keyed by group so the render below can walk `GROUPS`
   * and keep both forms in the table's order.
   *
   * Each field names its own control kind, which is deliberate: there is no
   * form library here and a schema-driven renderer would make every future
   * field a fight with an abstraction. `control` is the escape hatch for the
   * inputs that carry their own validation or their own editor.
   */
  const groupBody: Record<GroupId, ReactNode> = {
    identity: (
      <>
        <Field
          attr="name"
          ctx={createCtx}
          kind="text"
          control={
            /* The pattern must widen for Pulumi: a stack is named
               `project::stack`, and without this the browser blocks the only
               valid name before the form can submit, so the engine would be
               uncreatable from the UI entirely (#1535). */
            <input
              id="ws-name"
              data-testid="ws-name"
              type="text"
              value={String(values.name ?? '')}
              onChange={(e) => setValue('name', e.target.value)}
              required
              pattern={engine === 'pulumi'
                ? '[a-zA-Z0-9][a-zA-Z0-9_\\-]*::[a-zA-Z0-9][a-zA-Z0-9_\\-]*'
                : '[a-zA-Z0-9][a-zA-Z0-9_\\-]*'}
              title={engine === 'pulumi' ? t('form.namePatternPulumi') : t('form.namePattern')}
              placeholder={engine === 'pulumi' ? t('form.namePlaceholderPulumi') : t('form.namePlaceholder')}
              className={CREATE_INPUT}
            />
          }
        />
        {/* Only when there is a choice to make. Keyed on what the BUILD
            contains rather than on what an operator enabled, because the engine
            on/off switch is gone (#1986) — so a Terraform-only deployment sees
            no picker and creates Terraform workspaces, which is what the seeded
            value already says. */}
        {engines.length > 1 && (
          <Field
            attr="engine"
            ctx={createCtx}
            kind="select"
            control={
              <select
                id="ws-engine"
                data-testid="ws-engine-select"
                value={engine}
                onChange={(e) => setValue('engine', e.target.value)}
                className={CREATE_INPUT}
              >
                {engines.map((e) => (
                  <option key={e} value={e}>{engineName(e)}</option>
                ))}
              </select>
            }
          />
        )}
      </>
    ),

    execution: (
      <>
        <Field attr="execution-mode" ctx={createCtx} kind="select" options={execModeOptions} />
        {/* Terraform-only, and the table says so rather than this form — the
            engine restriction lives on the field spec so both forms inherit it.
            The version is NOT restricted that way: every engine pins one, and
            since #1559 a Pulumi workspace's is the Pulumi CLI it runs. */}
        <Field attr="execution-backend" ctx={createCtx} kind="select" options={backendOptions} />
        <Field
          attr="engine-version"
          ctx={createCtx}
          kind="text"
          control={
            <>
              <input
                id="ws-version"
                data-testid="ws-version"
                type="text"
                list="version-suggestions"
                value={String(values['engine-version'] ?? '')}
                onChange={(e) => setValue('engine-version', e.target.value)}
                pattern="[0-9]+\.[0-9]+(\.[0-9]+)?"
                title={t('form.versionTitle')}
                placeholder={t('form.versionPlaceholder')}
                className={CREATE_INPUT}
              />
              <datalist id="version-suggestions">
                {versionSuggestions.map((v) => (
                  <option key={v} value={v} />
                ))}
              </datalist>
            </>
          }
        />
        <Field
          attr="working-directory"
          ctx={createCtx}
          kind="text"
          placeholder={t('form.workingDirectoryPlaceholder')}
        />
        <Field attr="auto-apply-mode" ctx={createCtx} kind="select" options={autoApplyOptions} />
        <Field attr="terragrunt-enabled" ctx={createCtx} kind="checkbox" />
        <Field
          attr="terragrunt-version"
          ctx={createCtx}
          kind="text"
          placeholder={tDetail('fields.terragruntVersionPlaceholder')}
        />
        <Field attr="pulumi-bind-plan" ctx={createCtx} kind="checkbox" />
        <Field attr="parallelism" ctx={createCtx} kind="number" min={1} max={256} />
        <Field
          attr="resource-cpu"
          ctx={createCtx}
          kind="text"
          control={
            <input
              id="ws-cpu"
              data-testid="ws-cpu"
              type="text"
              value={String(values['resource-cpu'] ?? '')}
              onChange={(e) => setValue('resource-cpu', e.target.value)}
              pattern="[0-9]+m|[0-9]+(\.[0-9]+)?"
              title={t('form.cpuTitle')}
              placeholder="1"
              className={CREATE_INPUT}
            />
          }
        />
        <Field
          attr="resource-memory"
          ctx={createCtx}
          kind="text"
          control={
            <input
              id="ws-mem"
              data-testid="ws-mem"
              type="text"
              value={String(values['resource-memory'] ?? '')}
              onChange={(e) => setValue('resource-memory', e.target.value)}
              pattern="[0-9]+(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E|m)?"
              title={t('form.memoryTitle')}
              placeholder="2Gi"
              className={CREATE_INPUT}
            />
          }
        />
      </>
    ),

    operations: (
      <>
        <Field attr="plan-apply-enabled" ctx={createCtx} kind="checkbox" />
        <Field attr="configure-enabled" ctx={createCtx} kind="checkbox" />
        <Field attr="auto-configure-enabled" ctx={createCtx} kind="checkbox" />
        <Field attr="configure-after-apply" ctx={createCtx} kind="checkbox" />
        <Field attr="configure-all-hosts" ctx={createCtx} kind="checkbox" />
      </>
    ),

    ansible: (
      <>
        <Field
          attr="config-directory"
          ctx={createCtx}
          kind="text"
          placeholder={tDetail('fields.configDirectoryPlaceholder')}
        />
        <Field
          attr="default-playbook"
          ctx={createCtx}
          kind="text"
          placeholder={tDetail('fields.defaultPlaybookPlaceholder')}
        />
        <Field
          attr="default-limit"
          ctx={createCtx}
          kind="text"
          placeholder={tDetail('fields.defaultLimitPlaceholder')}
        />
        <Field
          attr="inventory-paths"
          ctx={createCtx}
          kind="list"
          control={
            <StringListEditor
              values={listOf('inventory-paths')}
              onChange={(next) => setValue('inventory-paths', next)}
              placeholder={tDetail('fields.inventoryPathsPlaceholder')}
              addLabel={tDetail('fields.inventoryPathsAdd')}
            />
          }
        />
        {/* Which inventory sources ansible may resolve on the runner
            (#2033). Terrapod itself never runs either kind, in either state. */}
        <Field attr="dynamic-inventory-enabled" ctx={createCtx} kind="checkbox" />
        <Field attr="inventory-scripts-enabled" ctx={createCtx} kind="checkbox" />
        <Field
          attr="ansible-version"
          ctx={createCtx}
          kind="text"
          placeholder={tDetail('fields.ansibleVersionPlaceholder')}
        />
        <Field
          attr="ansible-execution-environment"
          ctx={createCtx}
          kind="text"
          placeholder={tDetail('fields.ansibleExecutionEnvironmentPlaceholder')}
        />
        {/* The wire shape is `{name, version?}` objects, so the values stay
            wire-shaped here and the editor formats them as `name:version`
            lines — the same helper pair the autodiscovery rule form uses, so
            the two agree on the format. */}
        <Field
          attr="ansible-collections"
          ctx={createCtx}
          kind="list"
          control={
            <StringListEditor
              values={formatCollectionLines(
                (values['ansible-collections'] as AnsibleCollection[] | undefined) ?? [],
              )}
              onChange={(next) => setValue('ansible-collections', parseCollectionLines(next))}
              placeholder={tDetail('fields.ansibleCollectionsPlaceholder')}
              addLabel={tDetail('fields.ansibleCollectionsAdd')}
            />
          }
        />
        <Field attr="ee-resource-cpu" ctx={createCtx} kind="text" placeholder="1" />
        <Field attr="ee-resource-memory" ctx={createCtx} kind="text" placeholder="2Gi" />
      </>
    ),

    pools: (
      /* A SET, not one pool: runs are offered to every pool at once. The
         checkbox list mirrors the Configuration tab's, append-ordered so the
         stored order is the order the operator picked. `pool:assign` is
         enforced server-side on every pool in the set, so a pool the caller may
         read but not assign is refused with a 422 rather than hidden here. */
      <Field
        attr="agent-pool-ids"
        ctx={createCtx}
        kind="list"
        control={
          agentPools.length === 0 ? (
            <p className="text-sm text-slate-400">{tDetail('common.none')}</p>
          ) : (
            <ul className="space-y-1">
              {agentPools.map((p) => {
                const selected = listOf('agent-pool-ids').includes(p.id)
                return (
                  <li key={p.id}>
                    <label className="flex min-h-11 items-center gap-2 rounded-lg px-3 py-2 text-sm cursor-pointer bg-slate-700/60 text-slate-100 hover:bg-slate-700">
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={() =>
                          setValue(
                            'agent-pool-ids',
                            selected
                              ? listOf('agent-pool-ids').filter((id) => id !== p.id)
                              : [...listOf('agent-pool-ids'), p.id],
                          )
                        }
                        className="h-4 w-4 rounded border-slate-500 bg-slate-800 text-brand-500 focus:ring-1 focus:ring-brand-500"
                      />
                      <span className="min-w-0 break-words">{p.attributes.name}</span>
                    </label>
                  </li>
                )
              })}
            </ul>
          )
        }
      />
    ),

    vcs: (
      <>
        <Field
          attr="vcs-connection-id"
          ctx={createCtx}
          kind="select"
          control={
            <select
              id="ws-vcs-conn"
              data-testid="ws-vcs-conn"
              value={String(values['vcs-connection-id'] ?? '')}
              onChange={(e) => setValue('vcs-connection-id', e.target.value)}
              className={CREATE_INPUT}
            >
              <option value="">{t('form.none')}</option>
              {vcsConnections.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.attributes.name} ({c.attributes.provider})
                </option>
              ))}
            </select>
          }
        />
        <Field
          attr="vcs-repo-url"
          ctx={createCtx}
          kind="text"
          control={
            <input
              id="ws-vcs-repo"
              data-testid="ws-vcs-repo"
              type="text"
              value={String(values['vcs-repo-url'] ?? '')}
              onChange={(e) => setValue('vcs-repo-url', e.target.value)}
              pattern="https?://.+"
              title={t('form.vcsRepoUrlTitle')}
              placeholder={t('form.vcsRepoUrlPlaceholder')}
              className={CREATE_INPUT}
            />
          }
        />
        <Field
          attr="vcs-branch"
          ctx={createCtx}
          kind="text"
          placeholder={t('form.vcsBranchPlaceholder')}
        />
        <Field attr="vcs-workflow" ctx={createCtx} kind="select" options={vcsWorkflowOptions} />
        <Field attr="auto-merge" ctx={createCtx} kind="checkbox" />
        <Field
          attr="auto-merge-strategy"
          ctx={createCtx}
          kind="select"
          options={mergeStrategyOptions}
        />
        <Field attr="allow-fork-pr-plans" ctx={createCtx} kind="checkbox" />
        <Field
          attr="trigger-prefixes"
          ctx={createCtx}
          kind="list"
          control={
            <StringListEditor
              values={listOf('trigger-prefixes')}
              onChange={(next) => setValue('trigger-prefixes', next)}
              placeholder={tDetail('fields.triggerPrefixesPlaceholder')}
            />
          }
        />
      </>
    ),

    governance: (
      <>
        <Field attr="drift-detection-enabled" ctx={createCtx} kind="checkbox" />
        {/* Seconds on the wire, named intervals in the UI — the same set the
            Configuration tab offers. Stored as a number so `toAttributes` sends
            what the column holds. */}
        <Field
          attr="drift-detection-interval-seconds"
          ctx={createCtx}
          kind="select"
          options={driftIntervalOptions}
          control={
            <select
              id="ws-drift-interval"
              data-testid="ws-drift-interval"
              value={String(values['drift-detection-interval-seconds'] ?? 86400)}
              onChange={(e) =>
                setValue('drift-detection-interval-seconds', Number(e.target.value))
              }
              className={CREATE_INPUT}
            >
              {driftIntervalOptions.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          }
        />
        <Field
          attr="drift-ignore-rules"
          ctx={createCtx}
          kind="list"
          control={
            <StringListEditor
              values={listOf('drift-ignore-rules')}
              onChange={(next) => setValue('drift-ignore-rules', next)}
            />
          }
        />
        <Field attr="plan-expiry-seconds" ctx={createCtx} kind="number" min={0} />
      </>
    ),

    ai: (
      <>
        <Field attr="ai-summary-mode" ctx={createCtx} kind="select" options={aiModeOptions} />
        <Field attr="ai-policy-mode" ctx={createCtx} kind="select" options={aiModeOptions} />
        {/* Free prose up to 4000 characters, so a textarea rather than the
            single-line primitive. */}
        <Field
          attr="ai-summary-context"
          ctx={createCtx}
          kind="text"
          control={
            <textarea
              id="ws-ai-summary-context"
              data-testid="ws-ai-summary-context"
              rows={3}
              maxLength={4000}
              value={String(values['ai-summary-context'] ?? '')}
              onChange={(e) => setValue('ai-summary-context', e.target.value)}
              placeholder={tDetail('aiSummary.contextPlaceholder')}
              className={CREATE_INPUT}
            />
          }
        />
      </>
    ),

    security: (
      <>
        <Field
          attr="security-scan-enforcement"
          ctx={createCtx}
          kind="select"
          options={scanEnforcementOptions}
        />
        <Field
          attr="security-scan-engine"
          ctx={createCtx}
          kind="select"
          options={scanEngineOptions}
        />
        <Field
          attr="security-scan-severity-threshold"
          ctx={createCtx}
          kind="select"
          options={scanThresholdOptions}
        />
        <Field
          attr="security-scan-skip-rules"
          ctx={createCtx}
          kind="list"
          control={
            <StringListEditor
              values={listOf('security-scan-skip-rules')}
              onChange={(next) => setValue('security-scan-skip-rules', next)}
            />
          }
        />
      </>
    ),

    metadata: (
      /* `owner-email` is absent on purpose — see `CREATE_OMITS`. */
      <>
        <Field
          attr="labels"
          ctx={createCtx}
          kind="text"
          control={
            <LabelsEditor
              labels={(values.labels as Record<string, string> | undefined) ?? {}}
              onChange={(next) => setValue('labels', next)}
            />
          }
        />
        <Field
          attr="var-files"
          ctx={createCtx}
          kind="list"
          control={
            <StringListEditor
              values={listOf('var-files')}
              onChange={(next) => setValue('var-files', next)}
              placeholder={tDetail('fields.varFilesPlaceholder')}
            />
          }
        />
      </>
    ),

    cloudIdentity: (
      /* A map of provider configuration to its audiences, not a list — one
         token is minted per key. The same editor the autodiscovery rule form
         uses, so an inherited entry reads as inherited here too. */
      <Field
        attr="oidc-audiences"
        ctx={createCtx}
        kind="text"
        control={
          <OidcAudiencesEditor
            value={(values['oidc-audiences'] as OidcAudiences | undefined) ?? {}}
            fallbacks={oidcDefaults.audiences}
            inert={!oidcDefaults.issuerEnabled}
            onChange={(next) => setValue('oidc-audiences', sanitizeOidcAudiences(next))}
            audiencePlaceholder={tDetail('fields.oidcAudiencesPlaceholder')}
            addAudienceLabel={tDetail('fields.oidcAudiencesAdd')}
          />
        }
      />
    ),

    diagnostics: (
      <>
        <Field attr="debug-mode" ctx={createCtx} kind="checkbox" />
        <Field
          attr="slack-channel"
          ctx={createCtx}
          kind="text"
          placeholder={tDetail('slack.channelPlaceholder')}
        />
      </>
    ),
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={t('title')}
          description={t('description')}
          actions={
            <div className="flex items-center gap-3">
              <ConnectionStatus connected={sseConnected} />
              <button
                onClick={() => setShowCreate(!showCreate)}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
              >
                {showCreate ? t('cancel') : t('new')}
              </button>
            </div>
          }
        />

        {error && <ErrorBanner message={error} />}

        {showCreate && (
          <form onSubmit={handleCreate} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-4 min-w-0">
            {/* The shared field groups, in the table's own order (#2253). This
                form and the Configuration tab render one field set from
                `FIELDS`, so a new workspace attribute reaches both or fails the
                parity gate — where before they shared only chrome and NOT ONE
                field component, and drifted until create offered 13 controls
                against the ~50 the create route accepts.

                A group renders nothing when every field in it is behind an off
                gate, so the form is no longer than it was until an operator
                turns something on: with configure off there is no Ansible
                block, and with no VCS connection chosen there is no workflow or
                auto-merge. */}
            {GROUPS.map((g) => (
              <Group
                key={g}
                id={g}
                ctx={createCtx}
                title={tDetail(`groups.${g}`)}
                advanced={groupIsAdvanced(g, engine)}
              >
                {groupBody[g]}
              </Group>
            ))}
            <button
              type="submit"
              disabled={creating}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
            >
              {creating ? t('creating') : t('create')}
            </button>
          </form>
        )}

        {!loading && workspaces.length > 0 && (() => {
          const total = workspaces.length
          const locked = workspaces.filter(ws => ws.attributes.locked).length
          // `unhealthy` is driven by the Health chip, not the Status dropdown,
          // so it doesn't count toward the dropdown's active badge (which would
          // otherwise show a count with no matching checked row inside).
          const activeStatusCount = parsedFilter.terms.filter(
            t => t.kind === 'status' && t.value !== HEALTH_ISSUE_STATUS && t.value !== LOCKED_STATUS,
          ).length
          const healthLabel = healthFilterActive
            ? t('stats.clearHealthFilter')
            : t('stats.filterHealth')
          return (
            <div className="mb-4">
              {/* Compact toolbar (#719): stat chips + Status/Label share one
                  row; the filter input drops to its own row (basis-full) via
                  flex `order`. Total/Locked are desktop-only secondary counts;
                  Health is always shown (primary signal) and toggles the
                  `status:unhealthy` filter. No big stat cards on any viewport. */}
              <div className="flex flex-wrap items-center gap-2">
                <StatChip
                  label={t('stats.total')}
                  value={total}
                  onClick={parsedFilter.terms.length > 0 ? () => setFilterInput('') : undefined}
                  ariaLabel={t('stats.clearAllFilters')}
                  className="order-1 max-sm:hidden"
                />
                <StatChip
                  label={t('stats.health')}
                  value={withConditions}
                  valueClassName={withConditions > 0 ? 'text-red-400' : 'text-slate-300'}
                  onClick={withConditions > 0 ? toggleHealth : undefined}
                  active={healthFilterActive}
                  ariaLabel={healthLabel}
                  className="order-2"
                />
                <StatChip
                  label={t('stats.locked')}
                  value={locked}
                  valueClassName={locked > 0 ? 'text-amber-400' : undefined}
                  onClick={locked > 0 ? toggleLocked : undefined}
                  active={lockedFilterActive}
                  activeClassName="bg-amber-500/10 border-amber-500/50"
                  ariaLabel={lockedFilterActive ? t('stats.clearLockedFilter') : t('stats.filterLocked')}
                  className="order-3 max-sm:hidden"
                />
                {engines.length > 1 && (
                  <select
                    value={engineFilter}
                    onChange={e => setEngineFilter(e.target.value)}
                    aria-label={t('filter.engineLabel')}
                    data-testid="ws-engine-filter"
                    className="order-3 min-h-11 sm:min-h-0 px-2 py-1.5 rounded-lg bg-slate-800/50 border border-slate-700/50 text-base sm:text-sm text-slate-200 focus:outline-none focus:border-brand-500"
                  >
                    <option value="">{t('filter.engineAll')}</option>
                    {engines.map(e => (
                      <option key={e} value={e}>{engineName(e)}</option>
                    ))}
                  </select>
                )}
                <div className="order-4 flex-1 min-w-0" />
                {/* Input + Clear share their own full-width row (order-8),
                    Clear pinned to the right of the input — desktop and mobile. */}
                <div className="order-8 basis-full flex items-center gap-2">
                  <div className="relative flex-1 min-w-0" ref={suggestWrapRef}>
                  <input
                    type="text"
                    value={filterInput}
                    onChange={e => { setFilterInput(e.target.value); setSuggestOpen(true); setSuggestIndex(-1) }}
                    onFocus={() => { if (currentToken.trim()) setSuggestOpen(true) }}
                    onKeyDown={onFilterKeyDown}
                    ref={filterInputRef}
                    placeholder={t('filter.placeholder')}
                    aria-label={t('filter.ariaLabel')}
                    role="combobox"
                    aria-expanded={suggestOpen && suggestions.length > 0}
                    aria-controls="workspace-filter-suggestions"
                    // Without this the highlight moves silently: a screen reader
                    // announces the listbox but never which option the arrow keys
                    // landed on.
                    aria-activedescendant={
                      suggestOpen && suggestIndex >= 0
                        ? `workspace-filter-suggestion-${suggestIndex}`
                        : undefined
                    }
                    aria-autocomplete="list"
                    autoComplete="off"
                    className="w-full px-3 py-2 rounded-lg bg-slate-800/50 border border-slate-700/50 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-brand-500"
                  />
                  {suggestOpen && suggestions.length > 0 && (
                    <div
                      role="listbox"
                      id="workspace-filter-suggestions"
                      data-testid="filter-suggestions"
                      className="absolute start-0 end-0 z-20 mt-1 rounded-lg bg-slate-800 border border-slate-700 shadow-xl py-1 max-h-72 overflow-y-auto"
                    >
                      {suggestions.map((s, i) => (
                        <button
                          key={`${s.kind}:${s.insert}`}
                          id={`workspace-filter-suggestion-${i}`}
                          type="button"
                          role="option"
                          aria-selected={i === suggestIndex}
                          // onMouseDown (not onClick) so it fires before the input blur closes the menu.
                          // Clicking applies THIS suggestion directly (independent of the
                          // keyboard-highlight index), so a hover never hijacks a bare Enter.
                          onMouseDown={e => { e.preventDefault(); applyFilterSuggestion(s) }}
                          className={
                            'w-full flex items-center gap-2 px-3 py-1.5 text-sm transition-colors text-start border-s-2 ' +
                            (i === suggestIndex
                              ? 'bg-brand-600/40 text-white border-brand-300 font-semibold ring-1 ring-inset ring-brand-400/60'
                              : 'border-transparent text-slate-300 hover:bg-slate-700/40')
                          }
                        >
                          {s.dot ? (
                            <span className={'w-1.5 h-1.5 rounded-full ' + s.dot} />
                          ) : (
                            <span className="w-1.5" />
                          )}
                          <span className="flex-1 font-mono text-xs">{s.display}</span>
                          {typeof s.count === 'number' && (
                            <span className="text-[10px] text-slate-500">{s.count}</span>
                          )}
                          <span className="text-[10px] uppercase tracking-wide text-slate-500">{hintLabel(s.hint)}</span>
                        </button>
                      ))}
                    </div>
                  )}
                  </div>
                  {parsedFilter.terms.length > 0 && (
                    <button
                      type="button"
                      onClick={() => setFilterInput('')}
                      className="px-3 py-2 rounded-lg text-sm text-slate-400 hover:text-slate-200 transition-colors whitespace-nowrap flex-shrink-0"
                    >
                      {t('filter.clear')}
                    </button>
                  )}
                </div>
                {/* Status dropdown — single entry point for all status presets.
                    Picks any combination via toggle; the existing chips below
                    show what's active and let the user remove individually. */}
                <div className="relative order-5" ref={statusMenuRef}>
                  <button
                    type="button"
                    aria-haspopup="menu"
                    aria-expanded={statusMenuOpen}
                    onClick={() => setStatusMenuOpen(o => !o)}
                    className={
                      'inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium border transition-colors ' +
                      (activeStatusCount > 0
                        ? 'bg-slate-700/60 text-slate-100 border-slate-600'
                        : 'bg-slate-800/50 text-slate-300 border-slate-700/50 hover:bg-slate-700/60')
                    }
                  >
                    <span>{t('filter.status')}</span>
                    {activeStatusCount > 0 && (
                      <span className="inline-flex items-center justify-center min-w-5 px-1.5 rounded-full text-[10px] font-semibold bg-brand-600 text-white">
                        {activeStatusCount}
                      </span>
                    )}
                    <svg className={'w-3 h-3 transition-transform ' + (statusMenuOpen ? 'rotate-180' : '')} viewBox="0 0 12 12" fill="currentColor" aria-hidden>
                      <path d="M3 4.5l3 3 3-3" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </button>
                  {statusMenuOpen && (
                    <div
                      role="menu"
                      className="absolute end-0 z-10 mt-1 w-64 rounded-lg bg-slate-800 border border-slate-700 shadow-xl py-1 max-h-96 overflow-y-auto"
                    >
                      {WORKSPACE_STATUSES.map(opt => {
                        const active = hasStatusTerm(parsedFilter, opt.filter)
                        const count = statusCounts[opt.filter] || 0
                        return (
                          <button
                            key={opt.filter}
                            type="button"
                            role="menuitemcheckbox"
                            aria-checked={active}
                            onClick={() => setFilterInput(serializeFilter(toggleStatusTerm(parsedFilter, opt.filter)))}
                            className={
                              'w-full flex items-center gap-2 px-3 py-1.5 text-sm transition-colors ' +
                              (active ? 'bg-slate-700/60 text-slate-100' : 'text-slate-300 hover:bg-slate-700/40')
                            }
                          >
                            {/* Checkmark rail keeps the option labels aligned whether
                                checked or not. */}
                            <span className="w-3 inline-flex justify-center text-brand-400">
                              {active ? '✓' : ''}
                            </span>
                            <span className={'w-1.5 h-1.5 rounded-full ' + opt.dot} />
                            <span className="flex-1 text-start">{ts(opt.filter)}</span>
                            <span className={'text-xs ' + (count > 0 ? 'text-slate-400' : 'text-slate-600')}>{count}</span>
                          </button>
                        )
                      })}
                    </div>
                  )}
                </div>
                {/* Label dropdown — two-level picker for arbitrary label
                    key/value pairs. First level lists distinct label keys
                    in the visible workspaces; clicking a key drills into a
                    second menu of distinct values for that key. */}
                <div className="relative order-6" ref={labelMenuRef}>
                  {(() => {
                    const activeLabelCount = parsedFilter.terms.filter(t => t.kind === 'label' && t.value !== null).length
                    return (
                      <>
                        <button
                          type="button"
                          aria-haspopup="menu"
                          aria-expanded={labelMenuOpen}
                          onClick={() => {
                            setLabelMenuOpen(o => !o)
                            setLabelMenuKey(null)
                          }}
                          disabled={sortedLabelKeys.length === 0}
                          className={
                            'inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium border transition-colors disabled:opacity-50 disabled:cursor-not-allowed ' +
                            (activeLabelCount > 0
                              ? 'bg-slate-700/60 text-slate-100 border-slate-600'
                              : 'bg-slate-800/50 text-slate-300 border-slate-700/50 hover:bg-slate-700/60')
                          }
                        >
                          <span>{t('filter.label')}</span>
                          {activeLabelCount > 0 && (
                            <span className="inline-flex items-center justify-center min-w-5 px-1.5 rounded-full text-[10px] font-semibold bg-brand-600 text-white">
                              {activeLabelCount}
                            </span>
                          )}
                          <svg className={'w-3 h-3 transition-transform ' + (labelMenuOpen ? 'rotate-180' : '')} viewBox="0 0 12 12" fill="currentColor" aria-hidden>
                            <path d="M3 4.5l3 3 3-3" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                        </button>
                        {labelMenuOpen && labelMenuKey === null && (
                          <div role="menu" className="absolute end-0 z-10 mt-1 w-64 rounded-lg bg-slate-800 border border-slate-700 shadow-xl py-1 max-h-96 overflow-y-auto">
                            {sortedLabelKeys.map(k => (
                              <button
                                key={k}
                                type="button"
                                role="menuitem"
                                onClick={() => setLabelMenuKey(k)}
                                className="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-700/40 transition-colors"
                              >
                                <span className="flex-1 text-start font-mono text-xs">{k}</span>
                                <span className="text-xs text-slate-500">{t('filter.valueCount', { count: labelIndex.get(k)?.size || 0 })}</span>
                                <span aria-hidden className="text-slate-500">›</span>
                              </button>
                            ))}
                          </div>
                        )}
                        {labelMenuOpen && labelMenuKey !== null && (
                          <div role="menu" className="absolute end-0 z-10 mt-1 w-64 rounded-lg bg-slate-800 border border-slate-700 shadow-xl py-1 max-h-96 overflow-y-auto">
                            <button
                              type="button"
                              onClick={() => setLabelMenuKey(null)}
                              className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-slate-400 hover:bg-slate-700/40 transition-colors border-b border-slate-700/50"
                            >
                              <span aria-hidden>‹</span>
                              <span>{t('filter.back')}</span>
                              <span className="ms-auto font-mono text-slate-500">{labelMenuKey}</span>
                            </button>
                            {Array.from(labelIndex.get(labelMenuKey)?.entries() || [])
                              .sort(([a], [b]) => a.localeCompare(b))
                              .map(([v, count]) => {
                                const active = hasLabelTerm(parsedFilter, labelMenuKey, v)
                                return (
                                  <button
                                    key={v}
                                    type="button"
                                    role="menuitemcheckbox"
                                    aria-checked={active}
                                    onClick={() => {
                                      setFilterInput(serializeFilter(toggleLabelTerm(parsedFilter, labelMenuKey, v)))
                                    }}
                                    className={
                                      'w-full flex items-center gap-2 px-3 py-1.5 text-sm transition-colors ' +
                                      (active ? 'bg-slate-700/60 text-slate-100' : 'text-slate-300 hover:bg-slate-700/40')
                                    }
                                  >
                                    <span className="w-3 inline-flex justify-center text-brand-400">{active ? '✓' : ''}</span>
                                    <span className="flex-1 text-start font-mono text-xs">{v}</span>
                                    <span className="text-xs text-slate-500">{count}</span>
                                  </button>
                                )
                              })}
                          </div>
                        )}
                      </>
                    )
                  })()}
                </div>
                {/* Group by dropdown — path-based grouping for VCS workspaces. */}
                <div className="relative order-[7]" ref={groupMenuRef}>
                  <button
                    type="button"
                    aria-haspopup="menu"
                    aria-expanded={groupMenuOpen}
                    onClick={() => setGroupMenuOpen(o => !o)}
                    className={
                      'inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium border transition-colors ' +
                      (groupMode !== 'flat'
                        ? 'bg-slate-700/60 text-slate-100 border-slate-600'
                        : 'bg-slate-800/50 text-slate-300 border-slate-700/50 hover:bg-slate-700/60')
                    }
                  >
                    <span>{t('group.label')}</span>
                    {groupMode !== 'flat' && (
                      <span className="inline-flex items-center justify-center min-w-5 px-1.5 rounded-full text-[10px] font-semibold bg-brand-600 text-white">
                        1
                      </span>
                    )}
                    <svg className={'w-3 h-3 transition-transform ' + (groupMenuOpen ? 'rotate-180' : '')} viewBox="0 0 12 12" fill="currentColor" aria-hidden>
                      <path d="M3 4.5l3 3 3-3" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </button>
                  {groupMenuOpen && (
                    <div role="menu" className="absolute right-0 z-10 mt-1 w-52 rounded-lg bg-slate-800 border border-slate-700 shadow-xl py-1 max-h-96 overflow-y-auto">
                      {([['flat', t('group.flat')], ['repo', t('group.repo')], ['repo-path', t('group.repoPath')]] as const).map(([value, label]) => (
                        <button
                          key={value}
                          type="button"
                          role="menuitemradio"
                          aria-checked={groupMode === value}
                          onClick={() => { setGroupMode(value); setGroupMenuOpen(false) }}
                          className={
                            'w-full flex items-center gap-2 px-3 py-1.5 text-sm transition-colors ' +
                            (groupMode === value ? 'bg-slate-700/60 text-slate-100' : 'text-slate-300 hover:bg-slate-700/40')
                          }
                        >
                          <span className="w-3 inline-flex justify-center text-brand-400">
                            {groupMode === value ? '✓' : ''}
                          </span>
                          <span className="flex-1 text-left">{label}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </div>
              {parsedFilter.terms.length > 0 && (
                <div className="flex flex-wrap gap-2 mt-2">
                  {parsedFilter.terms.map((term, i) => {
                    const label =
                      term.kind === 'name'
                        ? t('filter.termName', { value: term.value })
                        : term.kind === 'status'
                          ? t('filter.termStatus', { value: term.value })
                          : term.value === null
                            ? t('filter.termLabelAny', { key: term.key })
                            : t('filter.termLabel', { key: term.key, value: term.value })
                    return (
                      <button
                        type="button"
                        key={`${i}-${label}`}
                        onClick={() => setFilterInput(serializeFilter(removeTerm(parsedFilter, i)))}
                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-slate-700/50 text-slate-300 hover:bg-slate-700 transition-colors"
                        title={t('filter.removeTerm')}
                      >
                        <span>{label}</span>
                        <span aria-hidden className="text-slate-500">×</span>
                      </button>
                    )
                  })}
                  <span className="text-xs text-slate-500 self-center">
                    {t('filter.showing', { filtered: filteredWorkspaces.length, total: workspaces.length })}
                  </span>
                </div>
              )}
            </div>
          )
        })()}

        {loading ? (
          <LoadingSpinner />
        ) : workspaces.length === 0 ? (
          <EmptyState message={t('empty.none')} />
        ) : filteredWorkspaces.length === 0 ? (
          <EmptyState message={t('empty.noMatch')} />
        ) : groupMode !== 'flat' && workspaceTree.length > 0 ? (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
            <div className="flex items-center justify-end px-4 py-2 border-b border-slate-700/50">
              <button
                type="button"
                onClick={() => {
                  if (collapsedGroups.size === 0) {
                    setCollapsedGroups(new Set(allGroupPaths))
                  } else {
                    setCollapsedGroups(new Set())
                  }
                }}
                className="text-xs text-slate-400 hover:text-slate-200 transition-colors"
              >
                {collapsedGroups.size === 0 ? t('group.collapseAll') : t('group.expandAll')}
              </button>
            </div>
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <SortableHeader label={t('table.name')} sortKey="name" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label={t('table.mode')} sortKey="mode" sortState={sortState} onSort={toggleSort} className="hidden sm:table-cell" />
                  <SortableHeader label={t('table.pool')} sortKey="pool" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
                  <SortableHeader label={t('table.resources')} sortKey="resources" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                  <SortableHeader label={t('table.status')} sortKey="status" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                  <SortableHeader label={t('table.created')} sortKey="created" sortState={sortState} onSort={toggleSort} className="hidden xl:table-cell" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700/30">
                <WorkspaceGroupRows
                  groups={workspaceTree}
                  collapsedGroups={collapsedGroups}
                  toggleGroup={toggleGroup}
                  pathPrefix=""
                  parsedFilter={parsedFilter}
                  setFilterInput={setFilterInput}
                />
              </tbody>
            </table>
          </div>
        ) : (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <SortableHeader label={t('table.name')} sortKey="name" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label={t('table.mode')} sortKey="mode" sortState={sortState} onSort={toggleSort} className="hidden sm:table-cell" />
                  <SortableHeader label={t('table.pool')} sortKey="pool" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
                  <SortableHeader label={t('table.resources')} sortKey="resources" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                  <SortableHeader label={t('table.status')} sortKey="status" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                  <SortableHeader label={t('table.created')} sortKey="created" sortState={sortState} onSort={toggleSort} className="hidden xl:table-cell" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700/30">
                {sortedWorkspaces.map((ws) => {
                  const { def, runId } = resolveStatus(ws)
                  return (
                  <tr key={ws.id} className="hover:bg-slate-700/20 transition-colors">
                    <td className="px-4 py-3">
                      <div className="flex flex-col gap-1.5">
                        <Link
                          href={`/workspaces/${ws.id}`}
                          className="text-sm font-medium text-brand-400 hover:text-brand-300 break-all"
                        >
                          {stackLabel(ws.attributes.name, ws.attributes.engine)}
                          <EngineBadge engine={ws.attributes.engine} />
                        </Link>
                        {ws.attributes.labels && Object.keys(ws.attributes.labels).length > 0 && (
                          <div className="flex flex-wrap gap-1">
                            {Object.entries(ws.attributes.labels).map(([k, v]) => {
                              const active = hasLabelTerm(parsedFilter, k, v)
                              return (
                                <button
                                  key={`${k}=${v}`}
                                  type="button"
                                  onClick={() => setFilterInput(serializeFilter(toggleLabelTerm(parsedFilter, k, v)))}
                                  className={
                                    'inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-mono transition-colors ' +
                                    (active
                                      ? 'bg-brand-700/60 text-brand-100 hover:bg-brand-700'
                                      : 'bg-slate-700/40 text-slate-400 hover:bg-slate-700/80 hover:text-slate-200')
                                  }
                                  title={active ? t('row.removeLabelFilter') : t('row.addLabelFilter')}
                                >
                                  <span className="text-slate-500">{k}:</span>
                                  <span className="ms-0.5">{v}</span>
                                </button>
                              )
                            })}
                          </div>
                        )}
                        {/*
                          Mobile-only status (#719). Below `lg` the STATUS
                          column is hidden, so surface the same badges here
                          where the row is name-only — a phone must never lose
                          the running/errored/applied signal. Hidden at `lg`+
                          where the dedicated STATUS column takes over, so
                          desktop is pixel-identical.
                        */}
                        <div className="lg:hidden" data-testid="ws-row-status-mobile">
                          <WorkspaceStatusBadges
                            workspaceId={ws.id}
                            engine={ws.attributes.engine}
                            def={def}
                            runId={runId}
                            lifecycleState={ws.attributes['lifecycle-state']}
                          />
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-3 hidden sm:table-cell">
                      <span className="text-xs text-slate-400">{ws.attributes['execution-mode']}</span>
                    </td>
                    <td className="px-4 py-3 hidden md:table-cell">
                      <span className="text-xs text-slate-400">
                        {ws.attributes['agent-pool-name'] || '\u2014'}
                      </span>
                    </td>
                    <td className="px-4 py-3 hidden lg:table-cell">
                      <span className="text-xs text-slate-400">
                        {t('table.resourcesValue', { cpu: ws.attributes['resource-cpu'], memory: ws.attributes['resource-memory'] })}
                      </span>
                    </td>
                    <td className="px-4 py-3 hidden lg:table-cell">
                      <WorkspaceStatusBadges
                        workspaceId={ws.id}
                        engine={ws.attributes.engine}
                        def={def}
                        runId={runId}
                        lifecycleState={ws.attributes['lifecycle-state']}
                      />
                    </td>
                    <td className="px-4 py-3 hidden xl:table-cell">
                      <span className="text-xs text-slate-500">
                        {fmt.date(ws.attributes['created-at'])}
                      </span>
                    </td>
                  </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </>
  )
}

/** A Pulumi workspace is named project::stack; shown as "project / stack". */
function stackLabel(name: string, engine?: string): string {
  return engine === 'pulumi' && name.includes('::') ? name.split('::').join(' / ') : name
}

/**
 * Names the engine on a non-Terraform row (#1555). Terraform rows carry no badge,
 * so a Terraform-only deployment sees exactly what it always did.
 */
function EngineBadge({ engine }: { engine?: string }) {
  const t = useTranslations('workspaces')
  if (!engine || engine === 'terraform') return null
  return (
    <span
      data-testid="ws-engine-badge"
      className="ms-2 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium align-middle bg-violet-500/15 text-violet-300"
    >
      {engine === 'pulumi' ? t('form.enginePulumi') : engine}
    </span>
  )
}

export default function WorkspacesPage() {
  return (
    <Suspense fallback={<><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>}>
      <WorkspacesPageInner />
    </Suspense>
  )
}
