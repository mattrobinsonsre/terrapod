'use client'

import { forwardRef, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import {
  Layers,
  Network,
  Package,
  Blocks,
  Key,
  Activity,
  HardDrive,
  GitBranch,
  Users,
  Shield,
  Server,
  Variable,
  FileText,
  BookOpen,
  Code,
  LogOut,
  Menu,
  X,
  Compass,
  Wrench,
  ScrollText,
  LayoutGrid,
  Boxes,
  TerminalSquare,
  Library,
  Cog,
  User,
  ChevronDown,
  ArchiveRestore,
  type LucideIcon,
} from 'lucide-react'
import { useTranslations } from 'next-intl'
import { clearAuth, isAdmin, isAdminOrAudit, getAuthState } from '@/lib/auth'
import { SessionExpiryBanner } from '@/components/session-expiry-banner'
import { TokenExpiryBanner } from '@/components/token-expiry-banner'
import { LocaleSwitcher } from '@/components/locale-switcher'
import { HAIndicator } from '@/components/ha-indicator'

/**
 * Navigation is one DRY, viewport-driven component (#719). The link model
 * below is the single source of truth: the desktop bar renders it as flat
 * links + grouped dropdowns, and the mobile hamburger renders the *same*
 * groups as labelled sections. There is no forked mobile nav and no
 * user-agent sniffing — CSS (`md:` breakpoint) decides which layout shows.
 *
 * IA (approved): five primary items stay visible (Workspaces, Registry▾,
 * Catalog, Agent Pools, Labels); the ~11 admin destinations + Audit Log
 * collapse into Admin▾; personal/reference items collapse into Account▾.
 * Agent Pools + Labels are viewable by non-admins (RBAC-filtered), so they
 * stay top-level rather than under the admin-only menu.
 */

type NavItem = {
  href: string
  // i18n key under the `nav` namespace (#767) — resolved at render via
  // useTranslations('nav'), never a hardcoded display string.
  labelKey: string
  icon: LucideIcon
  external?: boolean
}

// Registry destinations (behind Registry▾ on desktop, a section on mobile).
const REGISTRY_ITEMS: NavItem[] = [
  { href: '/registry/modules', labelKey: 'modules', icon: Package },
  { href: '/registry/providers', labelKey: 'providers', icon: Blocks },
]

// Admin destinations (admin only). Audit Log is appended separately because
// it is visible to the audit role too.
const ADMIN_ITEMS: NavItem[] = [
  { href: '/admin/users', labelKey: 'users', icon: Users },
  { href: '/admin/roles', labelKey: 'roles', icon: Shield },
  { href: '/admin/vcs-connections', labelKey: 'vcsConnections', icon: GitBranch },
  { href: '/admin/variable-sets', labelKey: 'variableSets', icon: Variable },
  { href: '/admin/autodiscovery', labelKey: 'autodiscovery', icon: Compass },
  { href: '/admin/bulk-update', labelKey: 'bulkUpdate', icon: Wrench },
  { href: '/admin/execution-hooks', labelKey: 'executionHooks', icon: TerminalSquare },
  { href: '/admin/policy-sets', labelKey: 'policySets', icon: ScrollText },
  { href: '/admin/provider-templates', labelKey: 'providerTemplates', icon: Code },
  { href: '/admin/catalog', labelKey: 'catalogAdmin', icon: Boxes },
  { href: '/admin/deleted-workspaces', labelKey: 'deletedWorkspaces', icon: ArchiveRestore },
  { href: '/admin/binary-cache', labelKey: 'cache', icon: HardDrive },
]

const AUDIT_ITEM: NavItem = { href: '/admin/audit-log', labelKey: 'auditLog', icon: FileText }

// Personal / session destinations (behind the Account menu). Logout is
// rendered separately (it is an action, not a link).
const ACCOUNT_ITEMS: NavItem[] = [
  { href: '/settings/tokens', labelKey: 'apiTokens', icon: Key },
  { href: '/settings/sessions', labelKey: 'sessions', icon: Activity },
]

// Help / reference destinations — NOT account items. Grouped separately so
// the Account menu stays personal (tokens, sessions, log out).
/**
 * The git ref whose docs match a running instance.
 *
 * Linking at `main` from a released instance is quietly wrong: main documents
 * whatever has landed since, so an operator on v1.5.1 can be reading about a
 * flag their build does not have. Release tags are `vX.Y.Z`, and the server
 * reports the bare version, hence the prefix.
 *
 * Anything that is not exactly `X.Y.Z` has no tag to point at — a dev build
 * reporting `0.0.0`, a pre-release, or no version at all before the discovery
 * document has loaded — so it falls back to `main`. Guessing a tag that does
 * not exist would send people to a 404.
 */
function docsRef(version: string): string {
  return /^\d+\.\d+\.\d+$/.test(version) && version !== '0.0.0' ? `v${version}` : 'main'
}

function helpItems(version: string): NavItem[] {
  return [
    { href: '/api-docs', labelKey: 'apiReference', icon: Code },
    {
      href: `https://github.com/mattrobinsonsre/terrapod/blob/${docsRef(version)}/docs/index.md`,
      labelKey: 'docs',
      icon: BookOpen,
      external: true,
    },
  ]
}

function isPathActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(href + '/')
}

/** A top-level desktop bar link (Workspaces, Catalog, Agent Pools, Labels). */
/**
 * Whether the toolbar has room for its labels.
 *
 * A breakpoint cannot answer this. The bar's required width is not a constant:
 * labels are translated into 27 languages, and German or Finnish run materially
 * wider than English. Any hard-coded threshold is wrong for somebody.
 *
 * So it is measured — but NOT by measuring the live bar. Two earlier attempts
 * did, and both were wrong in ways that only showed up on screen:
 *
 *   - measuring a bar whose primary group had `flex-1` measures the CONTAINER,
 *     because a growing child always fills it. It collapsed to icons at 1200px
 *     with half the bar empty.
 *   - recording the labelled width once and reusing it captures whatever the
 *     first paint happened to measure, before webfonts settle. It recorded a
 *     width that was too small, so the bar stayed labelled past the point it
 *     fitted and the rightmost control was silently clipped.
 *
 * Instead there is a hidden probe: a copy of the bar that ALWAYS renders
 * labels, laid out but invisible. Its width is the true labelled width, right
 * now, in this locale, with these fonts. Comparing it against the real bar's
 * available width is a pure function of the current layout — no stored state to
 * go stale, and no feedback loop, because the probe never changes when the
 * visible bar does.
 */
function useFitsWithLabels(
  probeRef: React.RefObject<HTMLDivElement | null>,
  containerRef: React.RefObject<HTMLDivElement | null>
) {
  const [compact, setCompact] = useState(false)

  useLayoutEffect(() => {
    const probe = probeRef.current
    const container = containerRef.current
    if (!probe || !container) return

    const measure = () => setCompact(probe.scrollWidth > container.clientWidth)
    measure()

    const ro = new ResizeObserver(measure)
    ro.observe(container)
    ro.observe(probe) // fires when the labels themselves change width
    // Webfonts land after first paint and change every label's width.
    document.fonts?.ready.then(measure).catch(() => {})
    return () => ro.disconnect()
  }, [probeRef, containerRef])

  return compact
}

/**
 * The label for an icon-only control: a tooltip on hover AND on keyboard focus.
 *
 * AGENTS.md forbids hover-only affordances, and this does not break that rule.
 * Icon-only mode exists only on the desktop bar, which is itself gated at `lg`
 * — below that the hamburger renders full labels, so a touch device never sees
 * it. The tooltip appears on focus as well as hover, and the control carries a
 * real `aria-label` regardless, so the name never depends on pointing at it.
 */
function IconLabel({ text, align = 'start' }: { text: string; align?: 'start' | 'end' }) {
  return (
    <span
      role="tooltip"
      className={`pointer-events-none absolute top-full mt-1.5 z-50 whitespace-nowrap rounded-md border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-200 opacity-0 shadow-lg transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100 ${
        align === 'end' ? 'end-0' : 'start-0'
      }`}
    >
      {text}
    </span>
  )
}

function NavLink({
  href,
  icon: Icon,
  label,
  compact = false,
}: {
  href: string
  icon: LucideIcon
  label: string
  compact?: boolean
}) {
  const pathname = usePathname()
  const active = isPathActive(pathname, href)
  return (
    <Link
      href={href}
      // aria-label unconditionally, not only when compact: the accessible name
      // must not depend on whether the viewport happens to be wide today.
      aria-label={label}
      title={undefined}
      className={`group relative flex items-center gap-2 rounded-lg py-2 text-sm font-medium whitespace-nowrap transition-colors ${
        compact ? 'px-2' : 'px-3'
      } ${
        active
          ? 'bg-brand-600/20 text-brand-400'
          : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
      }`}
    >
      <Icon size={16} />
      {compact ? <IconLabel text={label} /> : label}
    </Link>
  )
}

/** A desktop dropdown group (Registry / Admin / Account). */
function NavDropdown({
  label,
  icon: Icon,
  items,
  active,
  align = 'start',
  footer,
  header,
  compact = false,
}: {
  label: string
  icon: LucideIcon
  items: NavItem[]
  active: boolean
  align?: 'start' | 'end'
  footer?: React.ReactNode
  header?: React.ReactNode
  compact?: boolean
}) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label={label}
          className={`group relative flex items-center gap-2 rounded-lg py-2 text-sm font-medium whitespace-nowrap transition-colors outline-none focus-visible:ring-2 focus-visible:ring-brand-500 ${
            compact ? 'px-2' : 'px-3'
          } ${
            active
              ? 'bg-brand-600/20 text-brand-400'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800 data-[state=open]:text-slate-200 data-[state=open]:bg-slate-800'
          }`}
        >
          <Icon size={16} />
          {compact ? <IconLabel text={label} align={align} /> : label}
          {/* The chevron stays: it is what says "this opens something", which
              an icon alone does not. It is 14px; the labels were the cost. */}
          <ChevronDown size={14} className="opacity-70" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align={align}
          sideOffset={6}
          className="z-50 min-w-[12rem] rounded-lg border border-slate-700 bg-slate-800 p-1 shadow-xl"
        >
          {header}
          {items.map((it) => (
            <DropdownMenu.Item key={it.href} asChild>
              <MenuLink item={it} />
            </DropdownMenu.Item>
          ))}
          {footer}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  )
}

/**
 * A single link row inside a desktop dropdown. Rendered as the `asChild`
 * target of `DropdownMenu.Item`, so it MUST forward the ref and spread the
 * props Radix's Slot injects (`role="menuitem"`, `tabindex`, the highlight /
 * keyboard handlers, `data-*`). Dropping them — as an earlier version did by
 * accepting only `{item}` — left the anchor with no `menuitem` role, breaking
 * keyboard navigation and making the items invisible to assistive tech and to
 * `getByRole('menuitem')`. `forwardRef` + `{...rest}` restores the contract.
 */
const MenuLink = forwardRef<
  HTMLAnchorElement,
  { item: NavItem } & React.AnchorHTMLAttributes<HTMLAnchorElement>
>(function MenuLink({ item, className, ...rest }, ref) {
  const pathname = usePathname()
  const t = useTranslations('nav')
  const Icon = item.icon
  const cls =
    'flex items-center gap-2 px-3 py-2 rounded-md text-sm cursor-pointer outline-none transition-colors data-[highlighted]:bg-slate-700 data-[highlighted]:text-slate-100'
  if (item.external) {
    return (
      <a
        ref={ref}
        href={item.href}
        target="_blank"
        rel="noopener noreferrer"
        className={`${cls} text-slate-300 hover:bg-slate-700 hover:text-slate-100${className ? ' ' + className : ''}`}
        {...rest}
      >
        <Icon size={16} />
        {t(item.labelKey)}
      </a>
    )
  }
  const active = isPathActive(pathname, item.href)
  return (
    <Link
      ref={ref}
      href={item.href}
      className={`${cls} ${active ? 'text-brand-400' : 'text-slate-300 hover:bg-slate-700 hover:text-slate-100'}${className ? ' ' + className : ''}`}
      {...rest}
    >
      <Icon size={16} />
      {t(item.labelKey)}
    </Link>
  )
})

/** A section header in the mobile sheet. */
function MobileSection({ label }: { label: string }) {
  return (
    <div className="px-3 pt-4 pb-1 text-xs font-semibold uppercase tracking-wider text-slate-500">
      {label}
    </div>
  )
}

/** A single link row in the mobile sheet (44px tap target). */
function MobileLink({ item, onClick }: { item: NavItem; onClick: () => void }) {
  const pathname = usePathname()
  const t = useTranslations('nav')
  const Icon = item.icon
  const active = !item.external && isPathActive(pathname, item.href)
  const cls =
    'flex items-center gap-3 px-3 py-3 rounded-lg text-sm font-medium min-h-[44px] transition-colors'
  if (item.external) {
    return (
      <a
        href={item.href}
        target="_blank"
        rel="noopener noreferrer"
        onClick={onClick}
        className={`${cls} text-slate-400 hover:text-slate-200 hover:bg-slate-800`}
      >
        <Icon size={18} />
        {t(item.labelKey)}
      </a>
    )
  }
  return (
    <Link
      href={item.href}
      onClick={onClick}
      className={`${cls} ${
        active ? 'bg-brand-600/20 text-brand-400' : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
      }`}
    >
      <Icon size={18} />
      {t(item.labelKey)}
    </Link>
  )
}

/**
 * A full-screen mobile drawer: its own top bar (title + close) plus an
 * internally-scrolling body. Being `fixed inset-0` it's self-contained and
 * always aligned regardless of the sticky nav / expiry banners above it;
 * `overscroll-contain` + the body-scroll lock stop scrolling from chaining to
 * the page behind it.
 */
function MobileDrawer({
  id,
  title,
  onClose,
  children,
}: {
  id: string
  title: string
  onClose: () => void
  children: React.ReactNode
}) {
  const t = useTranslations('nav')
  return (
    <div
      id={id}
      className="md:hidden fixed top-0 start-0 end-0 h-dvh z-40 bg-slate-900 flex flex-col"
    >
      <div className="flex items-center justify-between h-14 px-4 border-b border-slate-800 flex-shrink-0">
        <span className="font-bold text-lg text-slate-100">{title}</span>
        <button
          onClick={onClose}
          aria-label={t('closeMenu')}
          className="p-2 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors min-h-[44px] min-w-[44px] flex items-center justify-center"
        >
          <X size={22} />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto overscroll-contain px-4 pt-2 pb-8 flex flex-col gap-0.5">
        {children}
      </div>
    </div>
  )
}

export default function NavBar() {
  const router = useRouter()
  const t = useTranslations('nav')
  const noopSubscribe = () => () => {}
  const admin = useSyncExternalStore(noopSubscribe, isAdmin, () => false)
  const adminOrAudit = useSyncExternalStore(noopSubscribe, isAdminOrAudit, () => false)
  const email = useSyncExternalStore(
    noopSubscribe,
    () => getAuthState()?.email ?? '',
    () => '',
  )
  const pathname = usePathname()
  const [menuOpen, setMenuOpen] = useState(false)
  const [accountOpen, setAccountOpen] = useState(false)
  const [version, setVersion] = useState('')

  useEffect(() => {
    fetch('/api/v2/ping')
      .then((r) => r.json())
      .then((d) => setVersion(d.version || ''))
      .catch(() => {})
  }, [])

  // Close both mobile drawers whenever the route changes. Link taps already
  // close via onClick, but this also covers navigations that don't originate
  // from a drawer link (browser back/forward, programmatic pushes) so a
  // full-screen drawer can never survive a page change. No cascading render:
  // React bails out of the update when the value is already `false`.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- deliberate route-sync close; see comment above
    setMenuOpen(false)
    setAccountOpen(false)
  }, [pathname])

  // A mobile drawer is a full-screen, internally-scrolling overlay; lock the
  // body while one is open so scrolling the drawer doesn't chain to the page
  // behind it. Restored on close/unmount.
  useEffect(() => {
    if (!menuOpen && !accountOpen) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = prev
    }
  }, [menuOpen, accountOpen])

  const handleLogout = () => {
    clearAuth()
    router.push('/login')
  }

  const closeDrawers = () => {
    setMenuOpen(false)
    setAccountOpen(false)
  }

  // Admin menu contents: full admin list for admins; audit-only users see
  // just the Audit Log entry. Audit Log is appended for anyone admin-or-audit.
  const adminMenuItems: NavItem[] = [...(admin ? ADMIN_ITEMS : []), AUDIT_ITEM]

  const registryActive = REGISTRY_ITEMS.some((i) => isPathActive(pathname, i.href))
  const adminActive = adminMenuItems.some((i) => isPathActive(pathname, i.href))
  const HELP = helpItems(version)
  const helpActive = HELP.some((i) => !i.external && isPathActive(pathname, i.href))
  const accountActive = ACCOUNT_ITEMS.some((i) => !i.external && isPathActive(pathname, i.href))
  const barRef = useRef<HTMLDivElement | null>(null)
  const probeRef = useRef<HTMLDivElement | null>(null)
  const compact = useFitsWithLabels(probeRef, barRef)

  // The bar shows an icon, not the identity. You know who you are, and the
  // address was the single widest thing in the toolbar — 111px for `admin`, and
  // roughly double that for a corporate address — which made the width at which
  // the bar stopped fitting depend on WHO WAS LOGGED IN. Moving it into the
  // menu removes the variable and makes the fit deterministic per locale.
  const accountLabel = t('account')

  return (
    <>
      <SessionExpiryBanner />
      <TokenExpiryBanner />
      <nav className="border-b border-slate-800 bg-slate-900/80 backdrop-blur-sm sticky top-0 z-10">
        <div className="px-4 sm:px-6 lg:px-8 relative">
          {/* Desktop nav — enabled at `lg`, not `md`: the full horizontal nav
              (logo + 5 primary items + admin + locale + help + a long account
              email) doesn't fit until ~1024, so below `lg` it would wrap into a
              tall, ugly multi-row bar that (being sticky) also covers page
              content beneath it. Below `lg` we use the clean hamburger instead. */}
          {/* md, not lg: the icon-only bar needs ~650px, so gating the
              hamburger on lg (1024px) hid a bar that fits, for the whole band
              between them. The rest of the UI switches to its phone treatment
              at md — useIsMobile() is `max-width: md`, and the tables flip to
              cards there — so this is where the nav should switch too, and the
              two now agree rather than leaving a 256px dead zone. */}
          {/* One definition, rendered twice: the visible bar, and an
              invisible always-labelled copy that "does it fit?" is asked of.
              A literal duplicate would be a bug factory — every future edit
              would have to be made in both, and missing one would silently
              break the measurement rather than break the build. */}
          {(() => {
            const contents = (labelled: boolean) => (
              <>
            <Link href="/" className="flex items-center gap-2 me-3 flex-shrink-0">
              {/* eslint-disable-next-line @next/next/no-img-element -- a static local SVG at a fixed size — next/image does not optimise SVG without dangerouslyAllowSVG and would add a loader for no benefit */}
              <img src="/logo.svg" alt="Terrapod" className="w-7 h-7" />
              <span className="font-bold text-lg text-slate-100">Terrapod</span>
            </Link>
            {/* The wordmark is a brand, not the first nav item. Without a
                divider "Terrapod" reads as though it were one, and the eye has
                to work out where the navigation actually starts. */}
            <div className="h-5 w-px bg-slate-700/70 me-3 flex-shrink-0" />
            <div className="flex items-center gap-1 flex-nowrap flex-shrink-0">
              <NavLink href="/workspaces" icon={Layers} label={t('workspaces')} compact={!labelled} />
              <NavLink href="/estate" icon={Network} label={t('estate')} compact={!labelled} />
              <NavDropdown label={t('registry')} icon={Library} items={REGISTRY_ITEMS} active={registryActive} compact={!labelled} />
              <NavLink href="/catalog" icon={LayoutGrid} label={t('catalog')} compact={!labelled} />
              <NavLink href="/admin/agent-pools" icon={Server} label={t('agentPools')} compact={!labelled} />
            </div>
            <div className="flex-1" />
            {adminOrAudit && (
              <NavDropdown label={t('admin')} icon={Cog} items={adminMenuItems} active={adminActive} align="end" compact={!labelled} />
            )}
            <LocaleSwitcher />
            <NavDropdown label={t('help')} icon={BookOpen} items={HELP} active={helpActive}
                  align="end"
                  compact
                  footer={
                    version ? (
                      <>
                        <DropdownMenu.Separator className="my-1 h-px bg-slate-700" />
                        {/* Which version you are talking to. It left the
                            toolbar to make room, and this is where it belongs
                            anyway — beside the docs link that now points at the
                            matching tag. Not a menu item: it is not actionable. */}
                        <div className="px-3 py-2 text-xs text-slate-500">
                          {t('version', { version })}
                        </div>
                      </>
                    ) : null
                  }
                />
            <NavDropdown
              label={accountLabel}
              icon={User}
              items={ACCOUNT_ITEMS}
              active={accountActive}
              align="end"
              // Icon-only at every width. Help and the account menu are chrome
              // rather than destinations, and their two labels were most of the
              // difference between the toolbar fitting a 1152px page and not.
              compact
              header={
                email ? (
                  <>
                    {/* Who you are signed in as. Not a menu item — it is not
                        actionable, so it must not be focusable or announced as
                        one. It reads as the heading of the menu it belongs to,
                        which is where an identity belongs. */}
                    <div className="px-3 py-2">
                      <div className="text-xs text-slate-500">{t('account')}</div>
                      <div className="truncate text-sm text-slate-200" title={email}>
                        {email}
                      </div>
                    </div>
                    <DropdownMenu.Separator className="my-1 h-px bg-slate-700" />
                  </>
                ) : null
              }
              footer={
                <>
                  <DropdownMenu.Separator className="my-1 h-px bg-slate-700" />
                  <DropdownMenu.Item asChild>
                    <button
                      type="button"
                      onClick={handleLogout}
                      className="flex w-full items-center gap-2 px-3 py-2 rounded-md text-sm text-slate-300 cursor-pointer outline-none transition-colors data-[highlighted]:bg-slate-700 data-[highlighted]:text-slate-100"
                    >
                      <LogOut size={16} />
                      {t('logOut')}
                    </button>
                  </DropdownMenu.Item>
                </>
              }
            />
            {/* Last in the bar on purpose: it is ambient status about the node
                you are talking to, not another control in the cluster. */}
            <HAIndicator />
              </>
            )
            return (
              <>
                <div
                  ref={barRef}
                  className="hidden md:flex items-center gap-1 py-2 overflow-hidden"
                >
                  {contents(!compact)}
                </div>
                {/* Rendered after the visible bar on purpose: a `.first()`
                    text locator should land on the real one. aria-hidden keeps
                    this copy out of role queries already, but Playwright's text
                    engine ignores aria-hidden, so DOM order is what saves a
                    text-based selector from silently matching a clipped node.
                    The probe must be measurable without being part of the
                    page. `invisible` alone is not enough: visibility:hidden
                    still takes part in layout, so the always-labelled copy —
                    wider than the bar by definition — extended the document and
                    gave every width below it a horizontal scrollbar. Clipping
                    it inside a positioned zero-size box keeps it out of the
                    document's scroll width; its own scrollWidth still reports
                    its content width, which is the whole point of it. */}
                <div className="hidden md:block absolute w-0 h-0 overflow-hidden pointer-events-none -z-10">
                  <div
                    ref={probeRef}
                    aria-hidden="true"
                    // @ts-expect-error -- `inert` is valid HTML that React types lag on
                    inert=""
                    className="flex items-center gap-1 py-2 absolute whitespace-nowrap"
                  >
                    {contents(true)}
                  </div>
                </div>
              </>
            )
          })()}

          {/* Mobile top bar — logo + Account trigger + hamburger (below `lg`) */}
          <div className="md:hidden flex items-center justify-between h-14">
            <Link href="/" className="flex items-center gap-2">
              {/* eslint-disable-next-line @next/next/no-img-element -- a static local SVG at a fixed size — next/image does not optimise SVG without dangerouslyAllowSVG and would add a loader for no benefit */}
              <img src="/logo.svg" alt="Terrapod" className="w-7 h-7" />
              <span className="font-bold text-lg text-slate-100">Terrapod</span>
            </Link>
            <div className="flex items-center gap-1">
              <button
                onClick={() => {
                  setAccountOpen(true)
                  setMenuOpen(false)
                }}
                aria-label={t('openAccountMenu')}
                aria-expanded={accountOpen}
                aria-controls="mobile-account-menu"
                className="p-2 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors min-h-[44px] min-w-[44px] flex items-center justify-center"
              >
                <User size={22} />
              </button>
              <button
                onClick={() => {
                  setMenuOpen(true)
                  setAccountOpen(false)
                }}
                aria-label={t('openMenu')}
                aria-expanded={menuOpen}
                aria-controls="mobile-nav-menu"
                className="p-2 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors min-h-[44px] min-w-[44px] flex items-center justify-center"
              >
                <Menu size={22} />
              </button>
              {/* Last here too, for the same reason as the desktop bar. */}
              <HAIndicator />
            </div>
          </div>

          {/* Mobile main drawer — primary destinations, then Registry / Admin /
              Help sections. Account is deliberately NOT here — it has its own
              trigger + drawer. */}
          {menuOpen && (
            <MobileDrawer id="mobile-nav-menu" title={t('menu')} onClose={closeDrawers}>
              <MobileLink item={{ href: '/workspaces', labelKey: 'workspaces', icon: Layers }} onClick={closeDrawers} />
              <MobileLink item={{ href: '/estate', labelKey: 'estate', icon: Network }} onClick={closeDrawers} />
              <MobileLink item={{ href: '/catalog', labelKey: 'catalog', icon: LayoutGrid }} onClick={closeDrawers} />
              <MobileLink
                item={{ href: '/admin/agent-pools', labelKey: 'agentPools', icon: Server }}
                onClick={closeDrawers}
              />

              <MobileSection label={t('registry')} />
              {REGISTRY_ITEMS.map((it) => (
                <MobileLink key={it.href} item={it} onClick={closeDrawers} />
              ))}

              {adminOrAudit && (
                <>
                  <MobileSection label={t('admin')} />
                  {adminMenuItems.map((it) => (
                    <MobileLink key={it.href} item={it} onClick={closeDrawers} />
                  ))}
                </>
              )}

              <MobileSection label={t('help')} />
              {HELP.map((it) => (
                <MobileLink key={it.href} item={it} onClick={closeDrawers} />
              ))}
            </MobileDrawer>
          )}

          {/* Mobile account drawer — personal / session, opened by the User icon */}
          {accountOpen && (
            <MobileDrawer id="mobile-account-menu" title={t('account')} onClose={closeDrawers}>
              {email && <div className="px-3 pb-2 text-sm text-slate-400 truncate">{email}</div>}
              <div className="px-3 pb-2">
                <LocaleSwitcher />
              </div>
              {ACCOUNT_ITEMS.map((it) => (
                <MobileLink key={it.href} item={it} onClick={closeDrawers} />
              ))}
              <button
                onClick={handleLogout}
                className="flex items-center gap-3 px-3 py-3 rounded-lg text-sm font-medium min-h-[44px] text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors"
              >
                <LogOut size={18} />
                {t('logOut')}
              </button>
            </MobileDrawer>
          )}
        </div>
      </nav>
    </>
  )
}
