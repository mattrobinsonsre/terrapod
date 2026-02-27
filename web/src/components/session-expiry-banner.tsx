'use client'

import { useEffect, useState } from 'react'
import { clearAuth, getExpiresAt, loginRedirectUrl } from '@/lib/auth'

const WARNING_THRESHOLD_MS = 5 * 60 * 1000 // 5 minutes
const CHECK_INTERVAL_MS = 30_000 // 30 seconds

export function SessionExpiryBanner() {
  const [showWarning, setShowWarning] = useState(false)
  const [remaining, setRemaining] = useState('')

  useEffect(() => {
    const check = () => {
      const exp = getExpiresAt()
      if (!exp) return

      const ms = exp.getTime() - Date.now()
      if (ms <= 0) {
        clearAuth()
        window.location.href = loginRedirectUrl()
      } else if (ms < WARNING_THRESHOLD_MS) {
        const mins = Math.ceil(ms / 60_000)
        setShowWarning(true)
        setRemaining(`${mins} minute${mins !== 1 ? 's' : ''}`)
      } else {
        setShowWarning(false)
      }
    }

    check()
    const id = setInterval(check, CHECK_INTERVAL_MS)
    return () => clearInterval(id)
  }, [])

  if (!showWarning) return null

  return (
    <div className="bg-amber-900/50 border-b border-amber-700/50 px-4 py-2 text-sm text-amber-200 text-center">
      Session expires in {remaining} —{' '}
      <a href="/login" className="underline hover:text-amber-100">
        re-authenticate
      </a>
    </div>
  )
}
