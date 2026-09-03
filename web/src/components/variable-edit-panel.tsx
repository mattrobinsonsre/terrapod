'use client'

/**
 * The inline editor for a workspace variable.
 *
 * A full-width panel rather than a set of table cells. The Vault reference
 * builder (#1439) needs five fields, and forcing those through the VALUE
 * column truncated the path and field inputs to the point of being unreadable.
 * The edit state now spans the row, so the form has room whatever the value
 * source is — and the same component serves the desktop row and the mobile
 * card, which is what stops the two drifting apart.
 */

import { useTranslations } from 'next-intl'
import { SensitiveValueInput } from '@/components/sensitive-value-input'
import { VaultReferenceFields, type VaultReferenceValue } from '@/components/vault-reference-fields'

const INPUT =
  'w-full px-2 py-1.5 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500'
const MONO = `${INPUT} font-mono`
const LABEL = 'block text-xs text-slate-400 mb-1'

export interface VariableEditState {
  key: string
  value: string
  category: string
  sensitive: boolean
  hcl: boolean
  source: 'static' | 'vault'
  vault: VaultReferenceValue
}

export function VariableEditPanel({
  idPrefix,
  state,
  onChange,
  vaultAvailable,
  vaultInstances,
  vaultDefaultInstance,
  saving,
  onSave,
  onCancel,
}: {
  idPrefix: string
  state: VariableEditState
  onChange: (patch: Partial<VariableEditState>) => void
  vaultAvailable: boolean
  vaultInstances: string[]
  vaultDefaultInstance: string
  saving: boolean
  onSave: () => void
  onCancel: () => void
}) {
  const t = useTranslations('workspaceDetail.variables')
  const tc = useTranslations('workspaceDetail.actions')
  // A git credential is a JSON envelope; a Vault reference resolves to a single
  // field, so the pair cannot work and the API refuses it (#1439). The category
  // is editable inside this panel, so the exclusion has to live here rather
  // than only in the caller's gate.
  const isGitCat = state.category === 'git_http_auth' || state.category === 'git_ssh_auth'
  const isVault = state.source === 'vault' && !isGitCat

  return (
    <div className="space-y-4">
      {/* Identity and shape first — what the variable is, before where its
          value comes from. */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        <div>
          <label htmlFor={`${idPrefix}-key`} className={LABEL}>{t('key')}</label>
          <input
            id={`${idPrefix}-key`}
            type="text"
            className={MONO}
            value={state.key}
            onChange={(e) => onChange({ key: e.target.value })}
          />
        </div>
        <div>
          <label htmlFor={`${idPrefix}-cat`} className={LABEL}>{t('category')}</label>
          <select
            id={`${idPrefix}-cat`}
            className={INPUT}
            value={state.category}
            onChange={(e) => onChange({ category: e.target.value })}
          >
            <option value="terraform">terraform{/* i18n-ignore: category value */}</option>
            <option value="env">env{/* i18n-ignore: category value */}</option>
            <option value="git_http_auth">Git HTTPS credential{/* i18n-ignore: category value */}</option>
            <option value="git_ssh_auth">Git SSH credential{/* i18n-ignore: category value */}</option>
          </select>
        </div>
        {vaultAvailable && !isGitCat && (
          <div>
            <label htmlFor={`${idPrefix}-src`} className={LABEL}>{t('valueSource')}</label>
            <select
              id={`${idPrefix}-src`}
              className={INPUT}
              value={state.source}
              onChange={(e) => onChange({ source: e.target.value as 'static' | 'vault' })}
            >
              <option value="static">{t('sourceStatic')}</option>
              <option value="vault">{t('sourceVault')}</option>
            </select>
          </div>
        )}
      </div>

      {isVault ? (
        <VaultReferenceFields
          idPrefix={idPrefix}
          instances={vaultInstances}
          defaultInstance={vaultDefaultInstance}
          value={state.vault}
          onChange={(vault) => onChange({ vault })}
        />
      ) : (
        <div>
          <label htmlFor={`${idPrefix}-val`} className={LABEL}>{t('value')}</label>
          <SensitiveValueInput
            id={`${idPrefix}-val`}
            value={state.value}
            onChange={(value) => onChange({ value })}
            sensitive={state.sensitive}
            placeholder={state.sensitive ? t('enterNewValue') : ''}
            rows={2}
            className={`${MONO} resize-y`}
          />
        </div>
      )}

      {/* Flags and actions on one baseline: the decisions above, the commit
          below. Sensitive is locked on for a Vault reference — what it resolves
          to is a secret however the row is ticked. */}
      <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
        <div className="flex flex-wrap items-center gap-4">
          <label className={`flex items-center gap-2 ${isVault ? 'opacity-60' : 'cursor-pointer'}`}>
            <input
              type="checkbox"
              checked={isVault ? true : state.sensitive}
              disabled={isVault}
              onChange={(e) => onChange({ sensitive: e.target.checked })}
              className="rounded border-slate-600 bg-slate-700 text-brand-600"
            />
            <span className="text-xs text-slate-400">{t('sensitive')}</span>
          </label>
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={state.hcl}
              onChange={(e) => onChange({ hcl: e.target.checked })}
              className="rounded border-slate-600 bg-slate-700 text-brand-600"
            />
            <span className="text-xs text-slate-400">HCL{/* i18n-ignore: HCL is the language name */}</span>
          </label>
        </div>
        <div className="flex gap-2">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 text-slate-200"
          >
            {tc('cancel')}
          </button>
          <button
            onClick={onSave}
            disabled={saving}
            className="px-3 py-1.5 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white"
          >
            {saving ? tc('saving') : tc('save')}
          </button>
        </div>
      </div>
    </div>
  )
}
