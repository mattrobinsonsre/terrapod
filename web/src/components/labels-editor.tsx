'use client'

import { useState } from 'react'

interface LabelsEditorProps {
  labels: Record<string, string>
  onChange?: (labels: Record<string, string>) => void
  readOnly?: boolean
}

export function LabelsEditor({ labels, onChange, readOnly = false }: LabelsEditorProps) {
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

  function removeLabel(key: string) {
    if (!onChange) return
    const next = { ...labels }
    delete next[key]
    onChange(next)
  }

  if (readOnly) {
    if (entries.length === 0) {
      return <span className="text-sm text-slate-500">None</span>
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
                className="ml-0.5 text-slate-400 hover:text-red-400"
                aria-label={`Remove ${k}`}
              >
                &times;
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="flex gap-2">
        <input
          type="text"
          value={newKey}
          onChange={(e) => setNewKey(e.target.value)}
          placeholder="key"
          className="w-28 px-2 py-1 text-xs border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
        />
        <input
          type="text"
          value={newValue}
          onChange={(e) => setNewValue(e.target.value)}
          placeholder="value"
          onKeyDown={(e) => e.key === 'Enter' && addLabel()}
          className="w-28 px-2 py-1 text-xs border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500"
        />
        <button
          type="button"
          onClick={addLabel}
          disabled={!newKey.trim()}
          className="px-2 py-1 text-xs rounded bg-brand-600 text-white hover:bg-brand-500 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Add
        </button>
      </div>
    </div>
  )
}
