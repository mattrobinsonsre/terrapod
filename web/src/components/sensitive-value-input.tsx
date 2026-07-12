'use client'

import { useState } from 'react'
import { useTranslations } from 'next-intl'

interface SensitiveValueInputProps {
  id?: string
  value: string
  onChange: (value: string) => void
  sensitive: boolean
  placeholder?: string
  rows?: number
  className: string
}

/**
 * Variable-value entry field. When the variable is marked sensitive the typed
 * text is masked by default (with a Show/Hide toggle), so a secret isn't
 * visible on screen — or shoulder-surfable — while it's being entered. It stays
 * a multi-line textarea so certs / keys paste intact; masking swaps in the
 * `text-security-disc` font (the `.text-masked` class in globals.css) so it
 * works in every browser, Firefox included — `-webkit-text-security` is
 * Chromium/WebKit-only. Non-sensitive values render exactly as before.
 */
export function SensitiveValueInput({
  id,
  value,
  onChange,
  sensitive,
  placeholder,
  rows = 2,
  className,
}: SensitiveValueInputProps) {
  const t = useTranslations('common')
  const [reveal, setReveal] = useState(false)

  if (!sensitive) {
    return (
      <textarea
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        rows={rows}
        className={className}
      />
    )
  }

  return (
    <div className="relative">
      <textarea
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        rows={rows}
        // Mask the characters when hidden via the cross-browser disc font;
        // revealing (or an empty field) drops the class so the value shows
        // normally and the placeholder stays readable (the mask is a font,
        // so it would otherwise dot-out the placeholder too).
        className={reveal || value.length === 0 ? className : `${className} text-masked`}
        autoComplete="off"
        spellCheck={false}
      />
      <button
        type="button"
        onClick={() => setReveal((r) => !r)}
        tabIndex={-1}
        aria-label={reveal ? t('sensitiveInput.hideAria') : t('sensitiveInput.showAria')}
        className="absolute top-1.5 right-2 text-xs text-slate-400 hover:text-slate-200"
      >
        {reveal ? t('sensitiveInput.hide') : t('sensitiveInput.show')}
      </button>
    </div>
  )
}
