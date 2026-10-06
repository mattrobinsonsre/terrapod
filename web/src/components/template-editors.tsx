'use client'

import { useState } from 'react'
import { useTranslations } from 'next-intl'

import { isValidOidcProvider, type OidcAudiences } from '@/lib/oidc-audiences'

/**
 * Repeatable nested editors shared by the autodiscovery rule form (#318)
 * and the bulk-update page (#318). Each editor owns a JS array in the
 * parent's form state and renders add/remove "card" rows. The card object
 * shapes match the JSON:API attribute contracts exactly:
 *
 *   - var-files                → string[]
 *   - run-task-templates       → RunTaskSpec[]   (also bulk-update `run-tasks`)
 *   - notification-templates   → NotificationSpec[]
 *                                (also bulk-update `notification-configurations`)
 *
 * The hyphenated wire keys (`hmac-key`, `enforcement-level`,
 * `destination-type`, `email-addresses`) are kept verbatim on the spec
 * objects so the parent can drop the array straight into the request body.
 */

export type RunTaskSpec = {
  name: string
  url: string
  'hmac-key': string
  stage: string
  'enforcement-level': string
  enabled: boolean
}

export type NotificationSpec = {
  name: string
  'destination-type': string
  url: string
  token: string
  triggers: string[]
  'email-addresses': string[]
  enabled: boolean
}

export const RUN_TASK_STAGES = ['pre_plan', 'post_plan', 'pre_apply'] as const
export const RUN_TASK_ENFORCEMENT = ['mandatory', 'advisory'] as const
export const NOTIFICATION_DEST_TYPES = ['generic', 'slack', 'email'] as const
export const NOTIFICATION_TRIGGERS = [
  'run:created',
  'run:planning',
  'run:needs_attention',
  'run:planned',
  'run:applying',
  'run:completed',
  'run:errored',
  'run:drift_detected',
] as const

export function emptyRunTask(): RunTaskSpec {
  return {
    name: '',
    url: '',
    'hmac-key': '',
    stage: 'post_plan',
    'enforcement-level': 'mandatory',
    enabled: true,
  }
}

export function emptyNotification(): NotificationSpec {
  return {
    name: '',
    'destination-type': 'generic',
    url: '',
    token: '',
    triggers: [],
    'email-addresses': [],
    enabled: true,
  }
}

// 16px text below `sm` so iOS does not zoom the page on focus, dropping to the
// desktop size from `sm` up — identical on desktop, matching labels-editor.
const inputCls =
  'w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-base sm:text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent'
const labelCls = 'block text-xs font-medium text-slate-400 mb-1'

function AddButton({
  onClick,
  label,
  disabled = false,
}: {
  onClick: () => void
  label: string
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="shrink-0 px-3 py-1.5 min-h-11 sm:min-h-0 text-xs rounded-lg bg-slate-700 hover:bg-slate-600 text-slate-100 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
    >
      {label}
    </button>
  )
}

// A real button with a background and padding, not bare coloured text
// (AGENTS.md → Responsive: an action is a button, and it needs a tap target).
// It was the latter, which on a phone was a ~16px-tall run of red text sat
// beside a full-height input.
function RemoveButton({ onClick }: { onClick: () => void }) {
  const t = useTranslations('common')
  return (
    <button
      type="button"
      onClick={onClick}
      className="shrink-0 px-3 py-1.5 min-h-11 sm:min-h-0 text-xs font-medium rounded-lg bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors"
    >
      {t('templateEditors.remove')}
    </button>
  )
}

/* ------------------------------------------------------------------ */
/* var-files: repeatable single-string rows                            */
/* ------------------------------------------------------------------ */

export function StringListEditor({
  values,
  onChange,
  placeholder,
  addLabel,
}: {
  values: string[]
  onChange: (next: string[]) => void
  placeholder?: string
  addLabel?: string
}) {
  const t = useTranslations('common')
  function setAt(i: number, v: string) {
    const next = values.slice()
    next[i] = v
    onChange(next)
  }
  function removeAt(i: number) {
    onChange(values.filter((_, idx) => idx !== i))
  }
  return (
    <div className="space-y-2">
      {values.map((v, i) => (
        <div key={i} className="flex gap-2 items-center">
          <input
            type="text"
            value={v}
            onChange={(e) => setAt(i, e.target.value)}
            placeholder={placeholder ?? t('templateEditors.valuePlaceholder')}
            className={`${inputCls} font-mono`}
          />
          <RemoveButton onClick={() => removeAt(i)} />
        </div>
      ))}
      <AddButton
        onClick={() => onChange([...values, ''])}
        label={addLabel ?? t('templateEditors.add')}
      />
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* run-task-templates / run-tasks: repeatable card rows                */
/* ------------------------------------------------------------------ */

export function RunTaskTemplatesEditor({
  items,
  onChange,
}: {
  items: RunTaskSpec[]
  onChange: (next: RunTaskSpec[]) => void
}) {
  const t = useTranslations('common')
  function patch(i: number, patch: Partial<RunTaskSpec>) {
    const next = items.slice()
    next[i] = { ...next[i], ...patch }
    onChange(next)
  }
  function removeAt(i: number) {
    onChange(items.filter((_, idx) => idx !== i))
  }
  return (
    <div className="space-y-3">
      {items.map((it, i) => (
        <div
          key={i}
          className="p-3 rounded-lg bg-slate-900/60 border border-slate-700/60 space-y-3"
        >
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className={labelCls}>{t('templateEditors.name')}</label>
              <input
                type="text"
                value={it.name}
                onChange={(e) => patch(i, { name: e.target.value })}
                placeholder={t('templateEditors.runTaskNamePlaceholder')}
                className={inputCls}
              />
            </div>
            <div>
              <label className={labelCls}>{t('templateEditors.url')}</label>
              <input
                type="text"
                value={it.url}
                onChange={(e) => patch(i, { url: e.target.value })}
                placeholder={t('templateEditors.runTaskUrlPlaceholder')}
                className={inputCls}
              />
            </div>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <div>
              <label className={labelCls}>{t('templateEditors.hmacKey')}</label>
              <input
                type="password"
                value={it['hmac-key']}
                onChange={(e) => patch(i, { 'hmac-key': e.target.value })}
                placeholder={t('templateEditors.hmacKeyPlaceholder')}
                className={inputCls}
              />
            </div>
            <div>
              <label className={labelCls}>{t('templateEditors.stage')}</label>
              <select
                value={it.stage}
                onChange={(e) => patch(i, { stage: e.target.value })}
                className={inputCls}
              >
                {RUN_TASK_STAGES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className={labelCls}>{t('templateEditors.enforcement')}</label>
              <select
                value={it['enforcement-level']}
                onChange={(e) => patch(i, { 'enforcement-level': e.target.value })}
                className={inputCls}
              >
                {RUN_TASK_ENFORCEMENT.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="flex items-center justify-between">
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={it.enabled}
                onChange={(e) => patch(i, { enabled: e.target.checked })}
              />
              {t('templateEditors.enabled')}
            </label>
            <RemoveButton onClick={() => removeAt(i)} />
          </div>
        </div>
      ))}
      <AddButton
        onClick={() => onChange([...items, emptyRunTask()])}
        label={t('templateEditors.addRunTask')}
      />
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* notification-templates / notification-configurations: card rows     */
/* ------------------------------------------------------------------ */

export function NotificationTemplatesEditor({
  items,
  onChange,
}: {
  items: NotificationSpec[]
  onChange: (next: NotificationSpec[]) => void
}) {
  const t = useTranslations('common')
  function patch(i: number, patch: Partial<NotificationSpec>) {
    const next = items.slice()
    next[i] = { ...next[i], ...patch }
    onChange(next)
  }
  function removeAt(i: number) {
    onChange(items.filter((_, idx) => idx !== i))
  }
  function toggleTrigger(i: number, trig: string) {
    const cur = items[i].triggers
    const next = cur.includes(trig)
      ? cur.filter((t) => t !== trig)
      : [...cur, trig]
    patch(i, { triggers: next })
  }
  return (
    <div className="space-y-3">
      {items.map((it, i) => (
        <div
          key={i}
          className="p-3 rounded-lg bg-slate-900/60 border border-slate-700/60 space-y-3"
        >
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className={labelCls}>{t('templateEditors.name')}</label>
              <input
                type="text"
                value={it.name}
                onChange={(e) => patch(i, { name: e.target.value })}
                placeholder={t('templateEditors.notificationNamePlaceholder')}
                className={inputCls}
              />
            </div>
            <div>
              <label className={labelCls}>{t('templateEditors.destinationType')}</label>
              <select
                value={it['destination-type']}
                onChange={(e) => patch(i, { 'destination-type': e.target.value })}
                className={inputCls}
              >
                {NOTIFICATION_DEST_TYPES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
          </div>
          {it['destination-type'] !== 'email' && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className={labelCls}>{t('templateEditors.url')}</label>
                <input
                  type="text"
                  value={it.url}
                  onChange={(e) => patch(i, { url: e.target.value })}
                  placeholder={t('templateEditors.notificationUrlPlaceholder')}
                  className={inputCls}
                />
              </div>
              <div>
                <label className={labelCls}>{t('templateEditors.token')}</label>
                <input
                  type="password"
                  value={it.token}
                  onChange={(e) => patch(i, { token: e.target.value })}
                  placeholder={t('templateEditors.tokenPlaceholder')}
                  className={inputCls}
                />
              </div>
            </div>
          )}
          {it['destination-type'] === 'email' && (
            <div>
              <label className={labelCls}>{t('templateEditors.emailAddresses')}</label>
              <StringListEditor
                values={it['email-addresses']}
                onChange={(next) => patch(i, { 'email-addresses': next })}
                placeholder={t('templateEditors.emailPlaceholder')}
                addLabel={t('templateEditors.addEmail')}
              />
            </div>
          )}
          <div>
            <label className={labelCls}>{t('templateEditors.triggers')}</label>
            <div className="flex flex-wrap gap-2">
              {NOTIFICATION_TRIGGERS.map((trig) => (
                <label
                  key={trig}
                  className="flex items-center gap-1.5 text-xs text-slate-300 px-2 py-1 rounded border border-slate-700 bg-slate-800/60"
                >
                  <input
                    type="checkbox"
                    checked={it.triggers.includes(trig)}
                    onChange={() => toggleTrigger(i, trig)}
                  />
                  {trig}
                </label>
              ))}
            </div>
          </div>
          <div className="flex items-center justify-between">
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={it.enabled}
                onChange={(e) => patch(i, { enabled: e.target.checked })}
              />
              {t('templateEditors.enabled')}
            </label>
            <RemoveButton onClick={() => removeAt(i)} />
          </div>
        </div>
      ))}
      <AddButton
        onClick={() => onChange([...items, emptyNotification()])}
        label={t('templateEditors.addNotification')}
      />
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* oidc-audiences: provider configuration -> its audiences (#1901)     */
/* ------------------------------------------------------------------ */

/* The shape and the two wire helpers live in `lib/oidc-audiences.ts` so the
   unit suite can import them (node strips types from a `.ts`, not the JSX in a
   `.tsx`). Re-exported here so a page needs only this one import. */
export {
  isValidOidcProvider,
  sanitizeOidcAudiences,
  type OidcAudiences,
} from '@/lib/oidc-audiences'

export function OidcAudiencesEditor({
  value,
  onChange,
  readOnly = false,
  audiencePlaceholder,
  addAudienceLabel,
}: {
  value: OidcAudiences
  onChange?: (next: OidcAudiences) => void
  readOnly?: boolean
  audiencePlaceholder?: string
  addAudienceLabel?: string
}) {
  const t = useTranslations('common')
  const [newProvider, setNewProvider] = useState('')

  // Object key order is insertion order for string keys, and every write below
  // spreads the existing object, so a card never jumps position while editing.
  const entries = Object.entries(value)
  const typed = newProvider.trim()
  const duplicate = typed !== '' && Object.prototype.hasOwnProperty.call(value, typed)
  const malformed = typed !== '' && !isValidOidcProvider(typed)

  function addProvider() {
    if (!onChange || !typed || duplicate || malformed) return
    // Seeded with one blank row so there is somewhere to type. A row still
    // blank at save time is dropped by `sanitizeOidcAudiences`, and with it the
    // provider — the server refuses an empty list, so a half-finished entry
    // cannot be sent.
    onChange({ ...value, [typed]: [''] })
    setNewProvider('')
  }

  // Enter adds the provider. Without preventDefault it would also submit the
  // form the editor sits in, saving before the provider is added.
  function onEnter(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key !== 'Enter') return
    e.preventDefault()
    addProvider()
  }

  function removeProvider(key: string) {
    if (!onChange) return
    const next = { ...value }
    delete next[key]
    onChange(next)
  }

  function setAudiences(key: string, list: string[]) {
    if (!onChange) return
    onChange({ ...value, [key]: list })
  }

  if (readOnly) {
    // Nothing when empty: each page words its own "no overrides" line, because
    // what the fallback means differs between a workspace and a rule template.
    if (entries.length === 0) return null
    return (
      <div className="flex flex-col gap-2">
        {entries.map(([k, list]) => (
          <div key={k} className="flex flex-col sm:flex-row sm:items-baseline gap-1 sm:gap-2 min-w-0">
            <code className="shrink-0 text-xs font-mono text-slate-400">{k}</code>
            <div className="flex flex-wrap gap-1 min-w-0">
              {(list || []).map((aud) => (
                <code
                  key={aud}
                  className="bg-slate-700 px-2 py-0.5 rounded text-xs break-all"
                >
                  {aud}
                </code>
              ))}
            </div>
          </div>
        ))}
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {entries.map(([k, list]) => (
        <div
          key={k}
          /* `min-w-0` so a long opaque audience (`api://AzureADTokenExchange`)
             cannot set the card's intrinsic width and push the page sideways
             at phone width (AGENTS.md -> Responsive: no horizontal page
             scroll). */
          className="p-3 rounded-lg bg-slate-900/60 border border-slate-700/60 space-y-2 min-w-0"
        >
          <div className="flex items-center justify-between gap-2 min-w-0">
            <code className="text-xs font-mono text-slate-200 break-all min-w-0">{k}</code>
            <button
              type="button"
              onClick={() => removeProvider(k)}
              aria-label={t('oidcAudiences.removeProviderAria', { provider: k })}
              className="shrink-0 px-3 py-1.5 min-h-11 sm:min-h-0 text-xs font-medium rounded-lg bg-red-900/40 hover:bg-red-900/60 text-red-300 transition-colors"
            >
              {t('templateEditors.remove')}
            </button>
          </div>
          <div>
            <span className={labelCls}>{t('oidcAudiences.audiences')}</span>
            <StringListEditor
              values={list || []}
              onChange={(next) => setAudiences(k, next)}
              placeholder={audiencePlaceholder}
              addLabel={addAudienceLabel}
            />
          </div>
        </div>
      ))}
      <div className="space-y-1">
        <div className="flex gap-2">
          <input
            type="text"
            value={newProvider}
            onChange={(e) => setNewProvider(e.target.value)}
            onKeyDown={onEnter}
            placeholder={t('oidcAudiences.providerPlaceholder')}
            data-testid="oidc-provider-input"
            /* `min-w-0` because a flex item's default `min-width: auto` is its
               content-based minimum, and an input's is its intrinsic `size` —
               enough to push this row past a phone viewport once the Add
               button sits beside it. */
            className={`${inputCls} min-w-0 font-mono`}
          />
          <AddButton
            onClick={addProvider}
            label={t('oidcAudiences.addProvider')}
            disabled={!onChange || !typed || duplicate || malformed}
          />
        </div>
        {malformed && (
          <p className="text-xs text-amber-400">{t('oidcAudiences.providerInvalid')}</p>
        )}
        {duplicate && (
          <p className="text-xs text-amber-400">{t('oidcAudiences.providerDuplicate')}</p>
        )}
      </div>
    </div>
  )
}
