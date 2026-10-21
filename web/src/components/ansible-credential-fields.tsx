'use client'

/**
 * The discrete fields for the two Ansible credential categories (#1933).
 *
 * Its own file, beside `vault-reference-fields.tsx`, because it is the same
 * kind of thing: a shared field group for a variable whose value is a
 * structured envelope rather than a literal. It is used by three surfaces --
 * the workspace Variables add form, the variable-set add form, and the edit
 * panel -- so living inside any one of them would make the other two import a
 * page's internals.
 *
 * The component owns the JSON parse and serialise, which is why no caller
 * needs per-field state: each already held one `value` string and that string
 * is the envelope.
 */

import { useTranslations } from 'next-intl'
import { SensitiveValueInput } from '@/components/sensitive-value-input'

// Defined here rather than imported, which is this tree's own convention for a
// field-group component: `vault-file-delivery-fields.tsx` carries its own `LABEL`
// the same way. Sharing them would mean a styles module no component currently
// asks for.
const INPUT =
  'w-full px-2 py-1.5 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500'
const MONO = `${INPUT} font-mono`
const LABEL = 'block text-xs text-slate-400 mb-1'

export const ANSIBLE_MACHINE_CATEGORY = 'ansible_machine_auth'
export const ANSIBLE_VAULT_CATEGORY = 'ansible_vault_auth'

/** Whether this category's value is one of the two Ansible envelopes. */
export function isAnsibleAuthCategory(category: string): boolean {
  return category === ANSIBLE_MACHINE_CATEGORY || category === ANSIBLE_VAULT_CATEGORY
}

/**
 * Whether the category carries a JSON credential envelope at all.
 *
 * The git pair and the Ansible pair share every consequence of that: the API
 * forces them sensitive, and it refuses a Vault value source on them, because
 * a reference resolves to a single field and an envelope is an object (#1439).
 * So the form must not offer the source rather than offer it and meet a 422.
 */
export function isCredentialCategory(category: string): boolean {
  return (
    category === 'git_http_auth' ||
    category === 'git_ssh_auth' ||
    isAnsibleAuthCategory(category)
  )
}

// Ansible's own boolean spellings, mirroring `ansible_auth.parse_boolean`. A
// value written through the provider or the CLI may use any of them, and a
// tri-state select that read `yes` as "not set" would quietly offer to drop
// privilege escalation on the next save.
const ANSIBLE_TRUE = new Set(['true', 'yes', '1', 'on'])
const ANSIBLE_FALSE = new Set(['false', 'no', '0', 'off'])

type AnsibleEnvelope = Record<string, string>

/** The stored value as an envelope; `{}` for anything unreadable. */
function parseAnsibleEnvelope(value: string): AnsibleEnvelope {
  if (!value.trim()) return {}
  try {
    const parsed: unknown = JSON.parse(value)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const out: AnsibleEnvelope = {}
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof v === 'string') out[k] = v
    }
    return out
  } catch {
    return {}
  }
}

/**
 * The envelope as the API stores it, or `''` when every field is empty.
 *
 * Empty fields are OMITTED rather than sent as empty strings: the server reads
 * `private_key: ""` as a supplied-but-empty key and refuses the credential for
 * having neither secret, which names the wrong problem. And an envelope with
 * nothing in it serialises to `''`, not `'{}'`, because the edit path sends
 * `value` only when it is non-empty — so clearing every field leaves the
 * stored credential alone instead of replacing it with an empty object the
 * server would refuse.
 */
function serializeAnsibleEnvelope(fields: AnsibleEnvelope): string {
  const out: AnsibleEnvelope = {}
  for (const [k, v] of Object.entries(fields)) {
    if (v !== '') out[k] = v
  }
  return Object.keys(out).length ? JSON.stringify(out) : ''
}

/**
 * Discrete inputs for an Ansible credential, serialising into its JSON value.
 *
 * A raw JSON textarea would be the regression the git builder already avoided:
 * the operator hand-writes an envelope whose field names only the server
 * knows, and a typo is accepted as an unknown field (refused) or lands under a
 * name nothing reads. Discrete fields make the envelope's shape the form's
 * problem rather than theirs.
 *
 * `value` is the whole envelope and `onChange` receives the whole envelope, so
 * a caller needs no per-field state — the same single `value` string it
 * already holds for a plain variable carries this one.
 */
export function AnsibleCredentialFields({
  idPrefix,
  category,
  value,
  onChange,
}: {
  idPrefix: string
  category: string
  value: string
  onChange: (value: string) => void
}) {
  const t = useTranslations('ansibleCredentials')
  const fields = parseAnsibleEnvelope(value)
  const set = (field: string, next: string) =>
    onChange(serializeAnsibleEnvelope({ ...fields, [field]: next }))

  const becomeState = ANSIBLE_TRUE.has((fields.become ?? '').trim().toLowerCase())
    ? 'true'
    : ANSIBLE_FALSE.has((fields.become ?? '').trim().toLowerCase())
      ? 'false'
      : ''

  const secret = (field: string, rows: number, placeholder?: string) => (
    <SensitiveValueInput
      id={`${idPrefix}-ansible-${field}`}
      value={fields[field] ?? ''}
      onChange={(next) => set(field, next)}
      sensitive
      placeholder={placeholder}
      rows={rows}
      className={`${MONO} resize-y`}
    />
  )

  const text = (field: string, placeholder?: string) => (
    <input
      id={`${idPrefix}-ansible-${field}`}
      type="text"
      value={fields[field] ?? ''}
      onChange={(e) => set(field, e.target.value)}
      placeholder={placeholder}
      className={INPUT}
    />
  )

  if (category === ANSIBLE_VAULT_CATEGORY) {
    return (
      <div className="space-y-3 rounded-lg border border-slate-700/50 bg-slate-900/30 p-3">
        <p className="text-xs text-slate-400">{t('vaultHint')}</p>
        <div>
          <label htmlFor={`${idPrefix}-ansible-password`} className={LABEL}>
            {t('password')}
          </label>
          {secret('password', 2)}
        </div>
        <p className="text-xs text-slate-500">{t('replaceHint')}</p>
      </div>
    )
  }

  return (
    <div className="space-y-3 rounded-lg border border-slate-700/50 bg-slate-900/30 p-3">
      <p className="text-xs text-slate-400">{t('machineHint')}</p>
      {/* One column on a phone, two from `sm` up: seven fields stacked at
          412px is a long form, but a two-column grid there puts an escalation
          password beside a key and neither is wide enough to read. */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="sm:col-span-2">
          <label htmlFor={`${idPrefix}-ansible-private_key`} className={LABEL}>
            {t('privateKey')}
          </label>
          {secret('private_key', 4, '-----BEGIN OPENSSH PRIVATE KEY-----')}
        </div>
        <div>
          <label htmlFor={`${idPrefix}-ansible-password`} className={LABEL}>
            {t('password')}
          </label>
          {secret('password', 2)}
        </div>
        <div>
          <label htmlFor={`${idPrefix}-ansible-user`} className={LABEL}>{t('user')}</label>
          {text('user', 'ansible')}
        </div>
        <div>
          <label htmlFor={`${idPrefix}-ansible-become`} className={LABEL}>{t('become')}</label>
          <select
            id={`${idPrefix}-ansible-become`}
            className={INPUT}
            value={becomeState}
            onChange={(e) => set('become', e.target.value)}
          >
            {/* Three states, not a checkbox: unset is not the same as off.
                An absent `become` leaves whatever the inventory or the
                playbook already says, where `false` overrides it. */}
            <option value="">{t('becomeUnset')}</option>
            <option value="true">{t('becomeYes')}</option>
            <option value="false">{t('becomeNo')}</option>
          </select>
        </div>
        <div>
          <label htmlFor={`${idPrefix}-ansible-become_method`} className={LABEL}>
            {t('becomeMethod')}
          </label>
          {/* Free text rather than a list: the methods are plugin-provided, so
              a fixed list would refuse a deployment's own one. */}
          {text('become_method', 'sudo')}
        </div>
        <div>
          <label htmlFor={`${idPrefix}-ansible-become_user`} className={LABEL}>
            {t('becomeUser')}
          </label>
          {text('become_user', 'root')}
        </div>
        <div>
          <label htmlFor={`${idPrefix}-ansible-become_password`} className={LABEL}>
            {t('becomePassword')}
          </label>
          {secret('become_password', 2)}
        </div>
      </div>
      <p className="text-xs text-slate-500">{t('replaceHint')}</p>
    </div>
  )
}

