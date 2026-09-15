'use client'

import { useState } from 'react'
import { useTranslations } from 'next-intl'

const LABEL_INPUT =
  'min-w-0 flex-1 sm:flex-none sm:w-44 px-3 py-2 text-base sm:text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500'

interface LabelsEditorProps {
  labels: Record<string, string>
  onChange?: (labels: Record<string, string>) => void
  readOnly?: boolean
}

export function LabelsEditor({ labels, onChange, readOnly = false }: LabelsEditorProps) {
  const t = useTranslations('common')
  const [newKey, setNewKey] = useState('')
  const [newValue, setNewValue] = useState('')

  const entries = Object.entries(labels)

  function addLabel() {
    const k = newKey.trim()
    const v = newValue.trim()
    if (!k || !onChange) return
    onChange({ ...labels, [k]: v })
    setNewKey('')
    setNewValue('')
  }

  // Enter adds the label. Without preventDefault it would also submit whatever
  // form the editor sits in, saving the form before the label is added.
  function onEnter(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key !== 'Enter') return
    e.preventDefault()
    addLabel()
  }

  function removeLabel(key: string) {
    if (!onChange) return
    const next = { ...labels }
    delete next[key]
    onChange(next)
  }

  if (readOnly) {
    if (entries.length === 0) {
      return <span className="text-sm text-slate-500">{t('labelsEditor.none')}</span>
    }
    return (
      <div className="flex flex-wrap gap-1.5">
        {entries.map(([k, v]) => (
          <span key={k} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-slate-700 text-slate-200 border border-slate-600">
            <span className="text-slate-400">{k}:</span> {v}
          </span>
        ))}
      </div>
    )
  }

  return (
    <div className="space-y-2">
      {entries.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {entries.map(([k, v]) => (
            <span key={k} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-slate-700 text-slate-200 border border-slate-600">
              <span className="text-slate-400">{k}:</span> {v}
              <button
                type="button"
                onClick={() => removeLabel(k)}
                className="ms-0.5 text-slate-400 hover:text-red-400"
                aria-label={t('labelsEditor.removeAria', { key: k })}
              >
                &times;
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="flex gap-2">
        {/* Sized like the forms around it: 16px text below sm so iOS does not zoom
            on focus, and a full tap target (AGENTS.md → Responsive). */}
        <input
          type="text"
          value={newKey}
          onChange={(e) => setNewKey(e.target.value)}
          placeholder={t('labelsEditor.keyPlaceholder')}
          onKeyDown={onEnter}
          className={LABEL_INPUT}
        />
        <input
          type="text"
          value={newValue}
          onChange={(e) => setNewValue(e.target.value)}
          placeholder={t('labelsEditor.valuePlaceholder')}
          onKeyDown={onEnter}
          className={LABEL_INPUT}
        />
        <button
          type="button"
          onClick={addLabel}
          disabled={!newKey.trim()}
          className="shrink-0 px-4 py-2 min-h-11 sm:min-h-0 text-sm font-medium rounded-lg bg-brand-600 text-white hover:bg-brand-500 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {t('labelsEditor.add')}
        </button>
      </div>
    </div>
  )
}
