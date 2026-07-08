'use client'

import { forwardRef, useEffect, useState, useSyncExternalStore } from 'react'
import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import {
  Layers,
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
  type LucideIcon,
} from 'lucide-react'
import { clearAuth, isAdmin, isAdminOrAudit, getAuthState } from '@/lib/auth'
import { SessionExpiryBanner } from '@/components/session-expiry-banner'
import { TokenExpiryBanner } from '@/components/token-expiry-banner'

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
  label: string
  icon: LucideIcon
  external?: boolean
}

// Registry destinations (behind Registry▾ on desktop, a section on mobile).
const REGISTRY_ITEMS: NavItem[] = [
  { href: '/registry/modules', label: 'Modules', icon: Package },
  { href: '/registry/providers', label: 'Providers', icon: Blocks },
]

// Admin destinations (admin only). Audit Log is appended separately because
// it is visible to the audit role too.
const ADMIN_ITEMS: NavItem[] = [
  { href: '/admin/users', label: 'Users', icon: Users },
  { href: '/admin/roles', label: 'Roles', icon: Shield },
  { href: '/admin/vcs-connections', label: 'VCS Connections', icon: GitBranch },
  { href: '/admin/variable-sets', label: 'Variable Sets', icon: Variable },
  { href: '/admin/autodiscovery', label: 'Autodiscovery', icon: Compass },
  { href: '/admin/bulk-update', label: 'Bulk Update', icon: Wrench },
  { href: '/admin/execution-hooks', label: 'Execution Hooks', icon: TerminalSquare },
  { href: '/admin/policy-sets', label: 'Policy Sets', icon: ScrollText },
  { href: '/admin/provider-templates', label: 'Provider Templates', icon: Code },
  { href: '/admin/catalog', label: 'Catalog Admin', icon: Boxes },
  { href: '/admin/binary-cache', label: 'Cache', icon: HardDrive },
]

const AUDIT_ITEM: NavItem = { href: '/admin/audit-log', label: 'Audit Log', icon: FileText }

// Personal / session destinations (behind the Account menu). Logout is
// rendered separately (it is an action, not a link).
const ACCOUNT_ITEMS: NavItem[] = [
  { href: '/settings/tokens', label: 'API Tokens', icon: Key },
  { href: '/settings/sessions', label: 'Sessions', icon: Activity },
]

// Help / reference destinations — NOT account items. Grouped separately so
// the Account menu stays personal (tokens, sessions, log out).
const HELP_ITEMS: NavItem[] = [
  { href: '/api-docs', label: 'API Reference', icon: Code },
  {
    href: 'https://github.com/mattrobinsonsre/terrapod/blob/main/docs/index.md',
    label: 'Docs',
    icon: BookOpen,
    external: true,
  },
]

function isPathActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(href + '/')
}

/** A top-level desktop bar link (Workspaces, Catalog, Agent Pools, Labels). */
function NavLink({
  href,
  icon: Icon,
  label,
}: {
  href: string
  icon: LucideIcon
  label: string
}) {
  const pathname = usePathname()
  const active = isPathActive(pathname, href)
  return (
    <Link
      href={href}
      className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors ${
        active
          ? 'bg-brand-600/20 text-brand-400'
          : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
      }`}
    >
      <Icon size={16} />
      {label}
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
}: {
  label: string
  icon: LucideIcon
  items: NavItem[]
  active: boolean
  align?: 'start' | 'end'
  footer?: React.ReactNode
}) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors outline-none focus-visible:ring-2 focus-visible:ring-brand-500 ${
            active
              ? 'bg-brand-600/20 text-brand-400'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800 data-[state=open]:text-slate-200 data-[state=open]:bg-slate-800'
          }`}
        >
          <Icon size={16} />
          {label}
          <ChevronDown size={14} className="opacity-70" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align={align}
          sideOffset={6}
          className="z-50 min-w-[12rem] rounded-lg border border-slate-700 bg-slate-800 p-1 shadow-xl"
        >
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
        {item.label}
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
      {item.label}
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
        {item.label}
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
      {item.label}
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
  return (
    <div
      id={id}
      className="md:hidden fixed top-0 left-0 right-0 h-dvh z-40 bg-slate-900 flex flex-col"
    >
      <div className="flex items-center justify-between h-14 px-4 border-b border-slate-800 flex-shrink-0">
        <span className="font-bold text-lg text-slate-100">{title}</span>
        <button
          onClick={onClose}
          aria-label="Close menu"
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
  const helpActive = HELP_ITEMS.some((i) => !i.external && isPathActive(pathname, i.href))
  const accountActive = ACCOUNT_ITEMS.some((i) => !i.external && isPathActive(pathname, i.href))

  const accountLabel = email || 'Account'

  return (
    <>
      <SessionExpiryBanner />
      <TokenExpiryBanner />
      <nav className="border-b border-slate-800 bg-slate-900/80 backdrop-blur-sm sticky top-0 z-10">
        <div className="px-4 sm:px-6 lg:px-8">
          {/* Desktop nav */}
          <div className="hidden md:flex items-center gap-1 py-2">
            <Link href="/" className="flex items-center gap-2 mr-3 flex-shrink-0">
              <img src="/logo.svg" alt="Terrapod" className="w-7 h-7" />
              <span className="font-bold text-lg text-slate-100">Terrapod</span>
              {version && <span className="text-xs text-slate-500 font-normal">{version}</span>}
            </Link>
            <div className="flex items-center gap-1 flex-wrap flex-1">
              <NavLink href="/workspaces" icon={Layers} label="Workspaces" />
              <NavDropdown label="Registry" icon={Library} items={REGISTRY_ITEMS} active={registryActive} />
              <NavLink href="/catalog" icon={LayoutGrid} label="Catalog" />
              <NavLink href="/admin/agent-pools" icon={Server} label="Agent Pools" />
            </div>
            {adminOrAudit && (
              <NavDropdown label="Admin" icon={Cog} items={adminMenuItems} active={adminActive} align="end" />
            )}
            <NavDropdown label="Help" icon={BookOpen} items={HELP_ITEMS} active={helpActive} align="end" />
            <NavDropdown
              label={accountLabel}
              icon={User}
              items={ACCOUNT_ITEMS}
              active={accountActive}
              align="end"
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
                      Log out
                    </button>
                  </DropdownMenu.Item>
                </>
              }
            />
          </div>

          {/* Mobile top bar — logo + Account trigger + hamburger */}
          <div className="md:hidden flex items-center justify-between h-14">
            <Link href="/" className="flex items-center gap-2">
              <img src="/logo.svg" alt="Terrapod" className="w-7 h-7" />
              <span className="font-bold text-lg text-slate-100">Terrapod</span>
              {version && <span className="text-xs text-slate-500 font-normal">{version}</span>}
            </Link>
            <div className="flex items-center gap-1">
              <button
                onClick={() => {
                  setAccountOpen(true)
                  setMenuOpen(false)
                }}
                aria-label="Open account menu"
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
                aria-label="Open menu"
                aria-expanded={menuOpen}
                aria-controls="mobile-nav-menu"
                className="p-2 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors min-h-[44px] min-w-[44px] flex items-center justify-center"
              >
                <Menu size={22} />
              </button>
            </div>
          </div>

          {/* Mobile main drawer — primary destinations, then Registry / Admin /
              Help sections. Account is deliberately NOT here — it has its own
              trigger + drawer. */}
          {menuOpen && (
            <MobileDrawer id="mobile-nav-menu" title="Menu" onClose={closeDrawers}>
              <MobileLink item={{ href: '/workspaces', label: 'Workspaces', icon: Layers }} onClick={closeDrawers} />
              <MobileLink item={{ href: '/catalog', label: 'Catalog', icon: LayoutGrid }} onClick={closeDrawers} />
              <MobileLink
                item={{ href: '/admin/agent-pools', label: 'Agent Pools', icon: Server }}
                onClick={closeDrawers}
              />

              <MobileSection label="Registry" />
              {REGISTRY_ITEMS.map((it) => (
                <MobileLink key={it.href} item={it} onClick={closeDrawers} />
              ))}

              {adminOrAudit && (
                <>
                  <MobileSection label="Admin" />
                  {adminMenuItems.map((it) => (
                    <MobileLink key={it.href} item={it} onClick={closeDrawers} />
                  ))}
                </>
              )}

              <MobileSection label="Help" />
              {HELP_ITEMS.map((it) => (
                <MobileLink key={it.href} item={it} onClick={closeDrawers} />
              ))}
            </MobileDrawer>
          )}

          {/* Mobile account drawer — personal / session, opened by the User icon */}
          {accountOpen && (
            <MobileDrawer id="mobile-account-menu" title="Account" onClose={closeDrawers}>
              {email && <div className="px-3 pb-2 text-sm text-slate-400 truncate">{email}</div>}
              {ACCOUNT_ITEMS.map((it) => (
                <MobileLink key={it.href} item={it} onClick={closeDrawers} />
              ))}
              <button
                onClick={handleLogout}
                className="flex items-center gap-3 px-3 py-3 rounded-lg text-sm font-medium min-h-[44px] text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors"
              >
                <LogOut size={18} />
                Log out
              </button>
            </MobileDrawer>
          )}
        </div>
      </nav>
    </>
  )
}
