'use client'

import { useEffect, useState, useSyncExternalStore } from 'react'
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
  Tags,
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

// Account / reference destinations (behind Account▾ on desktop). Logout is
// rendered separately (it is an action, not a link).
const ACCOUNT_ITEMS: NavItem[] = [
  { href: '/settings/tokens', label: 'API Tokens', icon: Key },
  { href: '/settings/sessions', label: 'Sessions', icon: Activity },
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

/** A single link row inside a desktop dropdown. */
function MenuLink({ item }: { item: NavItem }) {
  const pathname = usePathname()
  const Icon = item.icon
  const cls =
    'flex items-center gap-2 px-3 py-2 rounded-md text-sm cursor-pointer outline-none transition-colors data-[highlighted]:bg-slate-700 data-[highlighted]:text-slate-100'
  if (item.external) {
    return (
      <a
        href={item.href}
        target="_blank"
        rel="noopener noreferrer"
        className={`${cls} text-slate-300 hover:bg-slate-700 hover:text-slate-100`}
      >
        <Icon size={16} />
        {item.label}
      </a>
    )
  }
  const active = isPathActive(pathname, item.href)
  return (
    <Link
      href={item.href}
      className={`${cls} ${active ? 'text-brand-400' : 'text-slate-300 hover:bg-slate-700 hover:text-slate-100'}`}
    >
      <Icon size={16} />
      {item.label}
    </Link>
  )
}

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
  const [version, setVersion] = useState('')

  useEffect(() => {
    fetch('/api/v2/ping')
      .then((r) => r.json())
      .then((d) => setVersion(d.version || ''))
      .catch(() => {})
  }, [])

  // Close the mobile sheet on route change (tapping a link navigates).
  useEffect(() => {
    setMenuOpen(false)
  }, [pathname])

  const handleLogout = () => {
    clearAuth()
    router.push('/login')
  }

  const closeMenu = () => setMenuOpen(false)

  // Admin menu contents: full admin list for admins; audit-only users see
  // just the Audit Log entry. Audit Log is appended for anyone admin-or-audit.
  const adminMenuItems: NavItem[] = [...(admin ? ADMIN_ITEMS : []), AUDIT_ITEM]

  const registryActive = REGISTRY_ITEMS.some((i) => isPathActive(pathname, i.href))
  const adminActive = adminMenuItems.some((i) => isPathActive(pathname, i.href))
  const accountActive =
    ACCOUNT_ITEMS.some((i) => !i.external && isPathActive(pathname, i.href))

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
              <NavLink href="/labels" icon={Tags} label="Labels" />
            </div>
            {adminOrAudit && (
              <NavDropdown label="Admin" icon={Cog} items={adminMenuItems} active={adminActive} align="end" />
            )}
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

          {/* Mobile nav */}
          <div className="md:hidden flex items-center justify-between h-14">
            <Link href="/" className="flex items-center gap-2">
              <img src="/logo.svg" alt="Terrapod" className="w-7 h-7" />
              <span className="font-bold text-lg text-slate-100">Terrapod</span>
              {version && <span className="text-xs text-slate-500 font-normal">{version}</span>}
            </Link>
            <button
              onClick={() => setMenuOpen(!menuOpen)}
              aria-label={menuOpen ? 'Close menu' : 'Open menu'}
              aria-expanded={menuOpen}
              aria-controls="mobile-nav-menu"
              className="p-2 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors min-h-[44px] min-w-[44px] flex items-center justify-center"
            >
              {menuOpen ? <X size={22} /> : <Menu size={22} />}
            </button>
          </div>
          {menuOpen && (
            <div id="mobile-nav-menu" className="md:hidden flex flex-col gap-0.5 pb-4">
              <MobileLink item={{ href: '/workspaces', label: 'Workspaces', icon: Layers }} onClick={closeMenu} />
              <MobileSection label="Registry" />
              {REGISTRY_ITEMS.map((it) => (
                <MobileLink key={it.href} item={it} onClick={closeMenu} />
              ))}
              <MobileLink item={{ href: '/catalog', label: 'Catalog', icon: LayoutGrid }} onClick={closeMenu} />
              <MobileLink
                item={{ href: '/admin/agent-pools', label: 'Agent Pools', icon: Server }}
                onClick={closeMenu}
              />
              <MobileLink item={{ href: '/labels', label: 'Labels', icon: Tags }} onClick={closeMenu} />

              {adminOrAudit && (
                <>
                  <MobileSection label="Admin" />
                  {adminMenuItems.map((it) => (
                    <MobileLink key={it.href} item={it} onClick={closeMenu} />
                  ))}
                </>
              )}

              <MobileSection label="Account" />
              {ACCOUNT_ITEMS.map((it) => (
                <MobileLink key={it.href} item={it} onClick={closeMenu} />
              ))}
              <button
                onClick={handleLogout}
                className="flex items-center gap-3 px-3 py-3 rounded-lg text-sm font-medium min-h-[44px] text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors"
              >
                <LogOut size={18} />
                Log out
              </button>
            </div>
          )}
        </div>
      </nav>
    </>
  )
}
