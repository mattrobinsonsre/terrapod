'use client'

import { useTranslations } from 'next-intl'

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

const inputCls =
  'w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent'
const labelCls = 'block text-xs font-medium text-slate-400 mb-1'

function AddButton({ onClick, label }: { onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="px-3 py-1.5 text-xs rounded-lg bg-slate-700 hover:bg-slate-600 text-slate-100 transition-colors"
    >
      {label}
    </button>
  )
}

function RemoveButton({ onClick }: { onClick: () => void }) {
  const t = useTranslations('common')
  return (
    <button
      type="button"
      onClick={onClick}
      className="text-xs text-red-400 hover:text-red-300"
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
