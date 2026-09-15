'use client'

/**
 * The "Check" action on the Vault reference form (#1663).
 *
 * Posts the reference as the form currently holds it to a reference-check
 * endpoint and shows each step's result. The check never resolves the
 * reference: Terrapod asks Vault whether it *could* read the path, and for
 * kv-v2 only lists the secret's key NAMES. A dynamic engine is never read,
 * because every read of one mints a credential.
 *
 * State is shown in form as well as colour — every row carries a labelled
 * pill with an icon, so the result reads the same without colour vision.
 */

import { useState } from 'react'
import { useTranslations } from 'next-intl'
import { CheckCircle2, CircleSlash, HelpCircle, XCircle, type LucideIcon } from 'lucide-react'
import { apiFetch } from '@/lib/api'

interface CheckItem {
  name: string
  status: 'pass' | 'fail' | 'skipped' | 'unknown'
  detail: string
}

export interface VaultCheckResult {
  ok: boolean
  keys: string[] | null
  notes: string[]
  checks: CheckItem[]
}

const STATUS_STYLE: Record<CheckItem['status'], { cls: string; icon: LucideIcon }> = {
  pass: { cls: 'bg-emerald-950/50 text-emerald-300 border-emerald-800', icon: CheckCircle2 },
  fail: { cls: 'bg-red-950/50 text-red-300 border-red-800', icon: XCircle },
  skipped: { cls: 'bg-slate-800 text-slate-300 border-slate-600', icon: CircleSlash },
  unknown: { cls: 'bg-amber-950/50 text-amber-300 border-amber-800', icon: HelpCircle },
}

const NAME_KEYS: Record<string, string> = {
  parses: 'nameParses',
  instance: 'nameInstance',
  'path-allowed': 'namePathAllowed',
  readable: 'nameReadable',
  'fields-present': 'nameFieldsPresent',
}

const NOTE_KEYS: Record<string, string> = {
  'dynamic-not-read': 'noteDynamic',
  'keys-need-plan-permission': 'noteKeysNeedPlan',
  'local-execution': 'noteLocal',
  'vault-disabled': 'noteDisabled',
}

const STATUS_KEYS: Record<CheckItem['status'], string> = {
  pass: 'statusPass',
  fail: 'statusFail',
  skipped: 'statusSkipped',
  unknown: 'statusUnknown',
}

export function VaultReferenceCheck({
  idPrefix,
  checkUrl,
  reference,
  variableKey,
}: {
  idPrefix: string
  /** The workspace or variable-set `…/vault-reference-checks` endpoint. */
  checkUrl: string
  /** The reference as the form would save it (a JSON string). */
  reference: () => string
  /** The variable key, which a file name defaults to. */
  variableKey?: string
}) {
  const t = useTranslations('vaultCheck')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<VaultCheckResult | null>(null)
  const [error, setError] = useState('')
  // A server-side key this build does not know renders as itself rather than
  // throwing MISSING_MESSAGE — an API a minor ahead may add a check or note.
  const label = (key: string | undefined, fallback: string) =>
    key && t.has(key as 'check') ? t(key as 'check') : fallback

  async function run() {
    setBusy(true)
    setError('')
    setResult(null)
    try {
      let parsed: unknown
      try {
        parsed = JSON.parse(reference() || '{}')
      } catch {
        parsed = {}
      }
      const res = await apiFetch(checkUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'vault-reference-checks',
            attributes: { reference: parsed, ...(variableKey ? { key: variableKey } : {}) },
          },
        }),
      })
      const body = await res.json().catch(() => ({}))
      if (!res.ok) {
        const detail = body?.errors?.[0]?.detail ?? body?.detail ?? res.statusText
        throw new Error(String(detail))
      }
      setResult(body.data.attributes as VaultCheckResult)
    } catch (e) {
      setError(t('error', { message: e instanceof Error ? e.message : String(e) }))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-2" data-testid={`${idPrefix}-vault-check`}>
      <button
        type="button"
        onClick={run}
        disabled={busy}
        className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-100 disabled:opacity-50 min-h-[2.75rem] sm:min-h-0"
      >
        {busy ? t('checking') : t('check')}
      </button>

      {error && (
        <p role="alert" className="text-xs text-red-300 break-words">
          {error}
        </p>
      )}

      {result && (
        <div
          role="status"
          className="rounded-lg border border-slate-700 bg-slate-900/60 p-3 space-y-2"
        >
          <p className={`text-sm font-medium ${result.ok ? 'text-emerald-300' : 'text-red-300'}`}>
            {result.ok ? t('ok') : t('notOk')}
          </p>
          <ul className="space-y-1.5">
            {result.checks.map((c) => {
              const style = STATUS_STYLE[c.status] ?? STATUS_STYLE.unknown
              const Icon = style.icon
              return (
                <li key={c.name} className="flex flex-col gap-1 sm:flex-row sm:items-start sm:gap-3">
                  <span
                    className={`inline-flex w-fit shrink-0 items-center gap-1 rounded border px-2 py-0.5 text-xs font-medium ${style.cls}`}
                  >
                    <Icon className="h-3.5 w-3.5" aria-hidden="true" />
                    {label(STATUS_KEYS[c.status], c.status)}
                  </span>
                  <span className="min-w-0 text-sm text-slate-200">
                    {label(NAME_KEYS[c.name], c.name)}
                    {c.detail && (
                      <span className="block text-xs text-slate-400 break-words">{c.detail}</span>
                    )}
                  </span>
                </li>
              )
            })}
          </ul>
          {result.keys && (
            <div className="text-xs text-slate-400">
              <span>{t('keys')}</span>{' '}
              {result.keys.length ? (
                <span className="font-mono text-slate-200 break-all">{result.keys.join(', ')}</span>
              ) : (
                <span>{t('noKeys')}</span>
              )}
            </div>
          )}
          {result.notes.map((n) => (
            <p key={n} className="text-xs text-slate-400">
              {label(NOTE_KEYS[n], n)}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}
