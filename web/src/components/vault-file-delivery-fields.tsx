'use client'

/**
 * File delivery for a Vault reference (#1619) — the `file` object's sub-form.
 *
 * With it on, the variable carries the path of a file on the runner rather
 * than the secret, for providers and tools that only read a credential from a
 * file. Today the only key is `name`. Any key the API knows but this form does
 * not is carried through an edit untouched by `@/lib/vault-reference`, so a
 * reference set up through the API or Terraform survives a UI edit. New `file`
 * keys belong here as further fields, without touching the parent builder.
 *
 * The name is validated server-side and the form does not second-guess it, so
 * the API's message is the one the operator sees.
 */

import { useTranslations } from 'next-intl'

const FIELD =
  'w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 font-mono focus:outline-none focus:ring-1 focus:ring-brand-500'

export interface VaultFileDeliveryValue {
  file: boolean
  fileName: string
}

export function VaultFileDeliveryFields({
  idPrefix,
  value,
  onChange,
}: {
  idPrefix: string
  value: VaultFileDeliveryValue
  onChange: (patch: Partial<VaultFileDeliveryValue>) => void
}) {
  const t = useTranslations('workspaceDetail.variables')

  return (
    <div className="space-y-2">
      <label className="flex items-center gap-2 cursor-pointer min-h-11 sm:min-h-0">
        <input
          id={`${idPrefix}-file`}
          type="checkbox"
          checked={value.file}
          onChange={(e) => onChange({ file: e.target.checked })}
          className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
        />
        <span className="text-sm text-slate-300">{t('vaultDeliverAsFile')}</span>
      </label>
      {value.file && (
        <div>
          <label htmlFor={`${idPrefix}-file-name`} className="block text-xs text-slate-400 mb-1">
            {t('vaultFileName')}
          </label>
          <input
            id={`${idPrefix}-file-name`}
            type="text"
            maxLength={255}
            className={FIELD}
            placeholder={t('vaultFileNameDefault')}
            value={value.fileName}
            onChange={(e) => onChange({ fileName: e.target.value })}
          />
          <p className="mt-1 text-xs text-slate-500">
            {t('vaultFileHint', {
              relExample: 'gcp/adc.json',
              base: '/var/run/terrapod/files/',
              home: '~/',
              homeExample: '~/.aws/credentials',
              fileFn: 'file(var.x)',
            })}
          </p>
        </div>
      )}
    </div>
  )
}
