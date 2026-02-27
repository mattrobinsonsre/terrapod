'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import { LayoutDashboard, Layers, Package, Blocks, Key, Activity, HardDrive, LogOut, Menu, X } from 'lucide-react'
import { clearAuth, isAdmin } from '@/lib/auth'
import { SessionExpiryBanner } from '@/components/session-expiry-banner'

function NavLink({
  href,
  children,
  onClick,
}: {
  href: string
  children: React.ReactNode
  onClick?: () => void
}) {
  const pathname = usePathname()
  const active = href === '/'
    ? pathname === '/'
    : pathname === href || pathname.startsWith(href + '/')

  return (
    <Link
      href={href}
      onClick={onClick}
      className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors ${
        active
          ? 'bg-brand-600/20 text-brand-400'
          : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
      }`}
    >
      {children}
    </Link>
  )
}

export default function NavBar() {
  const router = useRouter()
  const [admin, setAdmin] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)

  useEffect(() => {
    setAdmin(isAdmin())
  }, [])

  const handleLogout = () => {
    clearAuth()
    router.push('/login')
  }

  const closeMenu = () => setMenuOpen(false)

  const navLinks = (
    <>
      <NavLink href="/" onClick={closeMenu}>
        <LayoutDashboard size={16} />
        Dashboard
      </NavLink>
      <NavLink href="/workspaces" onClick={closeMenu}>
        <Layers size={16} />
        Workspaces
      </NavLink>
      <NavLink href="/registry/modules" onClick={closeMenu}>
        <Package size={16} />
        Modules
      </NavLink>
      <NavLink href="/registry/providers" onClick={closeMenu}>
        <Blocks size={16} />
        Providers
      </NavLink>
      <NavLink href="/settings/tokens" onClick={closeMenu}>
        <Key size={16} />
        API Tokens
      </NavLink>
      <NavLink href="/settings/sessions" onClick={closeMenu}>
        <Activity size={16} />
        Sessions
      </NavLink>
      {admin && (
        <NavLink href="/admin/binary-cache" onClick={closeMenu}>
          <HardDrive size={16} />
          Binary Cache
        </NavLink>
      )}
    </>
  )

  return (
    <>
      <SessionExpiryBanner />
      <nav className="border-b border-slate-800 bg-slate-900/80 backdrop-blur-sm sticky top-0 z-10">
        <div className="px-4 sm:px-6 lg:px-8">
          {/* Desktop nav */}
          <div className="hidden md:flex items-center gap-1 py-2">
            <Link href="/" className="flex items-center gap-2 mr-3 flex-shrink-0">
              <img src="/logo.svg" alt="Terrapod" className="w-7 h-7" />
              <span className="font-bold text-lg text-slate-100">Terrapod</span>
            </Link>
            <div className="flex items-center gap-1 flex-wrap flex-1">
              {navLinks}
            </div>
            <button
              onClick={handleLogout}
              className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors flex-shrink-0 ml-2"
            >
              <LogOut size={16} />
              Logout
            </button>
          </div>

          {/* Mobile nav */}
          <div className="md:hidden flex items-center justify-between h-14">
            <Link href="/" className="flex items-center gap-2">
              <img src="/logo.svg" alt="Terrapod" className="w-7 h-7" />
              <span className="font-bold text-lg text-slate-100">Terrapod</span>
            </Link>
            <div className="flex items-center gap-1">
              <button
                onClick={() => setMenuOpen(!menuOpen)}
                className="p-2 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors"
              >
                {menuOpen ? <X size={20} /> : <Menu size={20} />}
              </button>
              <button
                onClick={handleLogout}
                className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors"
              >
                <LogOut size={16} />
                Logout
              </button>
            </div>
          </div>
          {menuOpen && (
            <div className="md:hidden flex flex-col gap-1 pb-3">
              {navLinks}
            </div>
          )}
        </div>
      </nav>
    </>
  )
}
