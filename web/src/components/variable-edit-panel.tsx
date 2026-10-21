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

import { nativeCategoryKey } from '@/lib/variable-category'
import { SensitiveValueInput } from '@/components/sensitive-value-input'
import { VaultReferenceFields, type VaultReferenceValue } from '@/components/vault-reference-fields'
import {
  ANSIBLE_MACHINE_CATEGORY,
  ANSIBLE_VAULT_CATEGORY,
  AnsibleCredentialFields,
  isAnsibleAuthCategory,
  isCredentialCategory,
} from './ansible-credential-fields'

const INPUT =
  'w-full px-2 py-1.5 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500'
const MONO = `${INPUT} font-mono`
const LABEL = 'block text-xs text-slate-400 mb-1'

/* -------------------------------------------------------------------------
 * Ansible credentials (#1933)
 *
 * Two categories whose value is a JSON envelope, exactly as the git pair's is
 * (#1028). They live here rather than in a page because all three surfaces
 * that can write a variable — the workspace add form, the variable-set add
 * form, and this edit panel — have to offer the same fields: a field present
 * on one and absent from another is a credential an operator can set in one
 * place and not the other, which reads as the feature being broken rather
 * than as a missing input.
 *
 * The field names are the server's, and the ONE place they are written on
 * this side. `services/terrapod/services/ansible_auth.py` says why that
 * matters: a disagreement about a single key — `become_pass` against
 * `become_password` — is invisible. The API accepts the credential and the
 * configure runs with no escalation and no error.
 * ------------------------------------------------------------------------- */

export interface VariableEditState {
  key: string
  value: string
  category: string
  sensitive: boolean
  applyOnly: boolean
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
  vaultCheckUrl,
  engine,
  saveError,
}: {
  idPrefix: string
  state: VariableEditState
  onChange: (patch: Partial<VariableEditState>) => void
  vaultAvailable: boolean
  vaultInstances: string[]
  vaultDefaultInstance: string
  /** The reference-check endpoint for this workspace or variable set (#1663). */
  vaultCheckUrl?: string
  /**
   * The owning workspace's engine, used for the native category's LABEL only
   * (#1898) — never to gate which categories are offered, which is what the
   * engine used to decide and what made the list wrong on both engines.
   * Absent for a variable set, which has no single engine.
   */
  engine?: string
  saving: boolean
  onSave: () => void
  onCancel: () => void
  /**
   * The server's refusal, shown HERE rather than only in the page banner.
   *
   * A conflict on a variable is the one an operator cannot diagnose from the
   * status alone -- clearing the apply-only flag can collide with a standard
   * variable of the same name that it coexisted with a moment earlier (#2189).
   * The page-level banner is at the top of a long variables list, so on the
   * fortieth row the explanation is off-screen from the control that caused it.
   */
  saveError?: string
}) {
  const t = useTranslations('workspaceDetail.variables')
  const tc = useTranslations('workspaceDetail.actions')
  const ta = useTranslations('ansibleCredentials')
  // A git credential is a JSON envelope; a Vault reference resolves to a single
  // field, so the pair cannot work and the API refuses it (#1439). The category
  // is editable inside this panel, so the exclusion has to live here rather
  // than only in the caller's gate.
  //
  // The two Ansible categories are envelopes for the same reason (#1933), so
  // the Vault source is refused on them too. `isCredentialCategory` is the one
  // predicate for "this value is an envelope", rather than a second list of
  // categories to keep in step with the first.
  const isAnsibleCat = isAnsibleAuthCategory(state.category)
  const isVault = state.source === 'vault' && !isCredentialCategory(state.category)
  // `key` is a NAME on both Ansible categories, and they name different things
  // — the credential an inventory refers to as `${terrapod:<name>.<field>}`, or
  // a vault ID label. Labelling both "Key" is how an operator types a
  // credential name into the field that wants a vault label.
  const keyLabel = isAnsibleCat
    ? state.category === ANSIBLE_MACHINE_CATEGORY
      ? ta('name')
      : ta('vaultLabel')
    : t('key')
  const keyHint = isAnsibleCat
    ? state.category === ANSIBLE_MACHINE_CATEGORY
      ? ta('nameHint')
      : ta('vaultLabelHint')
    : ''
  return (
    <div className="space-y-4">
      {/* Identity and shape first — what the variable is, before where its
          value comes from. */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        <div>
          <label htmlFor={`${idPrefix}-key`} className={LABEL}>{keyLabel}</label>
          <input
            id={`${idPrefix}-key`}
            type="text"
            className={MONO}
            value={state.key}
            onChange={(e) => onChange({ key: e.target.value })}
          />
          {keyHint && <p className="mt-1 text-xs text-slate-500">{keyHint}</p>}
        </div>
        <div>
          <label htmlFor={`${idPrefix}-cat`} className={LABEL}>{t('category')}</label>
          <select
            id={`${idPrefix}-cat`}
            className={INPUT}
            value={state.category}
            onChange={(e) => onChange({ category: e.target.value })}
          >
            {/* One category for the engine's own parameters, whatever the
                workspace runs (#1898): Terraform input variables, Pulumi stack
                config, Ansible extra vars are one role with three deliveries.
                There is deliberately no engine-specific option — an engine here
                would be the mechanism showing through, which is what made a
                Pulumi workspace offer "terraform" and a Terraform one offer
                stack config. These are API values, shown as the API spells
                them, so what you pick is what you would write in the provider
                or hand to the CLI. */}
            <option value="native">{t(nativeCategoryKey(engine))}</option>
            <option value="env">{t('categoryEnv')}</option>
            <option value="git_http_auth">Git HTTPS credential{/* i18n-ignore: category value */}</option>
            <option value="git_ssh_auth">Git SSH credential{/* i18n-ignore: category value */}</option>
            {/* Translated, unlike the git pair above. Those predate the
                i18n gate and are allowlisted; a new user-facing option is held
                to the rule the gate states, so the two sit side by side until
                the git pair is caught up. */}
            <option value={ANSIBLE_MACHINE_CATEGORY}>{ta('categoryMachine')}</option>
            <option value={ANSIBLE_VAULT_CATEGORY}>{ta('categoryVault')}</option>
          </select>
        </div>
        {vaultAvailable && !isCredentialCategory(state.category) && (
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
          checkUrl={vaultCheckUrl}
          variableKey={state.key}
        />
      ) : isAnsibleCat ? (
        <AnsibleCredentialFields
          idPrefix={idPrefix}
          category={state.category}
          value={state.value}
          onChange={(value) => onChange({ value })}
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
          to is a secret however the row is ticked — and for an Ansible
          credential, which the API forces sensitive (#1933). Offering a
          checkbox the server overrides shows the operator a choice they do not
          have, and the row comes back ticked after the save either way. */}
      <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
        <div className="flex flex-wrap items-center gap-4">
          <label
            className={`flex items-center gap-2 ${isVault || isAnsibleCat ? 'opacity-60' : 'cursor-pointer'}`}
          >
            <input
              type="checkbox"
              checked={isVault || isAnsibleCat ? true : state.sensitive}
              disabled={isVault || isAnsibleCat}
              onChange={(e) => onChange({ sensitive: e.target.checked })}
              className="rounded border-slate-600 bg-slate-700 text-brand-600"
            />
            <span className="text-xs text-slate-400">{t('sensitive')}</span>
          </label>
          {/* Universal: a credential only the apply needs is withheld from the
              plan whatever its category (#2189). */}
          <label className="flex items-center gap-2 cursor-pointer" title={t('applyOnlyHint')}>
            <input
              type="checkbox"
              checked={state.applyOnly}
              onChange={(e) => onChange({ applyOnly: e.target.checked })}
              className="rounded border-slate-600 bg-slate-700 text-brand-600"
            />
            <span className="text-xs text-slate-400">{t('applyOnly')}</span>
          </label>
          {/* Only where a value can be typed at all. An environment variable
              is a string by definition and a git credential is a JSON
              envelope, so the flag meant nothing there and the API refuses it
              on both — offering it invited a 422 (#1435).

              "Structured", not "HCL": one flag whose meaning is the engine's
              (#1898) — a raw HCL expression for Terraform, a nested config
              value for Pulumi. Gating on the old `terraform` name would have
              left it unreachable on a Pulumi workspace, where it is what sets
              a nested key. */}
          {state.category === 'native' && (
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={state.hcl}
                onChange={(e) => onChange({ hcl: e.target.checked })}
                className="rounded border-slate-600 bg-slate-700 text-brand-600"
              />
              <span className="text-xs text-slate-400">{t('structured')}</span>
            </label>
          )}
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
      {saveError && (
        <p
          role="alert"
          className="text-sm text-red-300 bg-red-900/30 border border-red-800/50 rounded-lg px-3 py-2"
        >
          {saveError}
        </p>
      )}
    </div>
  )
}
