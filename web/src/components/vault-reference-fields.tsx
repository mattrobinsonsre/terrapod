'use client'

/**
 * The Vault reference builder (#1439) — discrete fields, never raw JSON.
 *
 * Shared by the add form and both edit renders (desktop row and mobile card).
 * One component because a reference edited in one place and created in another
 * must produce the same shape; three copies would drift.
 *
 * The Vault selector is always shown, even with a single instance configured.
 * Hiding it means an operator cannot tell which Vault a credential will be read
 * from, which is exactly the thing worth being explicit about.
 */

import { useTranslations } from 'next-intl'

const FIELD =
  'w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 font-mono focus:outline-none focus:ring-1 focus:ring-brand-500'
const SELECT =
  'w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500'

export interface VaultReferenceValue {
  instance: string
  mount: string
  path: string
  field: string
  engine: 'kv2' | 'dynamic'
}

export function VaultReferenceFields({
  idPrefix,
  value,
  onChange,
  instances,
  defaultInstance,
}: {
  idPrefix: string
  value: VaultReferenceValue
  onChange: (next: VaultReferenceValue) => void
  instances: string[]
  defaultInstance: string
}) {
  const t = useTranslations('workspaceDetail.variables')
  const set = (patch: Partial<VaultReferenceValue>) => onChange({ ...value, ...patch })

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label htmlFor={`${idPrefix}-vault`} className="block text-xs text-slate-400 mb-1">
            {t('vaultInstance')}
          </label>
          <select
            id={`${idPrefix}-vault`}
            className={SELECT}
            value={value.instance}
            onChange={(e) => set({ instance: e.target.value })}
          >
            {/* An explicit "use the default" entry rather than a blank: the
                reference omits the name in that case, and the operator should
                see which Vault that resolves to. */}
            <option value="">
              {defaultInstance
                ? t('vaultInstanceDefaultNamed', { name: defaultInstance })
                : t('vaultInstanceDefault')}
            </option>
            {instances.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor={`${idPrefix}-engine`} className="block text-xs text-slate-400 mb-1">
            {t('vaultEngine')}
          </label>
          <select
            id={`${idPrefix}-engine`}
            className={SELECT}
            value={value.engine}
            onChange={(e) => set({ engine: e.target.value as 'kv2' | 'dynamic' })}
          >
            <option value="kv2">{t('vaultEngineKv2')}</option>
            <option value="dynamic">{t('vaultEngineDynamic')}</option>
          </select>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div>
          <label htmlFor={`${idPrefix}-mount`} className="block text-xs text-slate-400 mb-1">
            {t('vaultMount')}
          </label>
          <input
            id={`${idPrefix}-mount`}
            type="text"
            required
            className={FIELD}
            placeholder="secret" /* i18n-ignore: the literal default kv-v2 mount name */
            value={value.mount}
            onChange={(e) => set({ mount: e.target.value })}
          />
        </div>
        <div>
          <label htmlFor={`${idPrefix}-path`} className="block text-xs text-slate-400 mb-1">
            {t('vaultPath')}
          </label>
          <input
            id={`${idPrefix}-path`}
            type="text"
            required
            className={FIELD}
            placeholder="apps/netbox" /* i18n-ignore: example Vault path */
            value={value.path}
            onChange={(e) => set({ path: e.target.value })}
          />
        </div>
        <div>
          <label htmlFor={`${idPrefix}-field`} className="block text-xs text-slate-400 mb-1">
            {t('vaultField')}
          </label>
          <input
            id={`${idPrefix}-field`}
            type="text"
            required
            className={FIELD}
            placeholder="apitoken" /* i18n-ignore: example field name */
            value={value.field}
            onChange={(e) => set({ field: e.target.value })}
          />
        </div>
      </div>

      <p className="text-xs text-slate-500">{t('vaultSecretNeverStored')}</p>
    </div>
  )
}
