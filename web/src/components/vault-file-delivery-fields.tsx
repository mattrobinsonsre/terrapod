'use client'

/**
 * File delivery for a Vault reference (#1619, #1648) — the `file` object's sub-form.
 *
 * With it on, the variable carries the path of a file on the runner rather
 * than the secret, for providers and tools that only read a credential from a
 * file. The file's content is one of three kinds (#1648):
 *
 * - one field, optionally base64-decoded (`file.encoding`);
 * - a template over the whole secret (`file.template`), such as an AWS
 *   credentials file built from the access key and secret of one lease;
 * - the whole secret as JSON or env lines (`file.format`, optional `fields`).
 *
 * The parent hides its `field` box for the last two, which read the whole
 * secret. Any key the API knows but this form does not is carried through an
 * edit untouched by `@/lib/vault-reference`, so a reference set up through the
 * API or Terraform survives a UI edit.
 *
 * Names, templates and fields are validated server-side and the form does not
 * second-guess them, so the API's message is the one the operator sees.
 */

import { useTranslations } from 'next-intl'
import type { VaultFileContent } from '@/lib/vault-reference'

const FIELD =
  'w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 font-mono focus:outline-none focus:ring-1 focus:ring-brand-500'
const SELECT =
  'w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500'
const LABEL = 'block text-xs text-slate-400 mb-1'
const HINT = 'mt-1 text-xs text-slate-500'

/** The server's template limit, in characters here (it is 16 KiB in bytes). */
const MAX_TEMPLATE_CHARS = 16 * 1024
const TEMPLATE_PLACEHOLDER =
  '[default]\naws_access_key_id = {{ access_key }}\naws_secret_access_key = {{ secret_key }}' // i18n-ignore: example template syntax, not UI copy

export interface VaultFileDeliveryValue {
  file: boolean
  fileName: string
  fileContent: VaultFileContent
  template: string
  format: string
  fields: string
  encoding: string
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
    <div className="space-y-3">
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
        <div className="space-y-3 border-l-2 border-slate-700 pl-3">
          <div>
            <label htmlFor={`${idPrefix}-file-name`} className={LABEL}>
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
            <p className={HINT}>
              {t('vaultFileHint', {
                relExample: 'gcp/adc.json',
                base: '/var/run/terrapod/files/',
                home: '~/',
                homeExample: '~/.aws/credentials',
                fileFn: 'file(var.x)',
              })}
            </p>
          </div>

          <div>
            <label htmlFor={`${idPrefix}-file-content`} className={LABEL}>
              {t('vaultFileContent')}
            </label>
            <select
              id={`${idPrefix}-file-content`}
              className={SELECT}
              value={value.fileContent}
              onChange={(e) => onChange({ fileContent: e.target.value as VaultFileContent })}
            >
              <option value="field">{t('vaultFileContentField')}</option>
              <option value="template">{t('vaultFileContentTemplate')}</option>
              <option value="format">{t('vaultFileContentFormat')}</option>
            </select>
          </div>

          {value.fileContent === 'template' && (
            <div>
              <label htmlFor={`${idPrefix}-file-template`} className={LABEL}>
                {t('vaultFileTemplate')}
              </label>
              <textarea
                id={`${idPrefix}-file-template`}
                rows={6}
                maxLength={MAX_TEMPLATE_CHARS}
                spellCheck={false}
                className={`${FIELD} min-h-32 whitespace-pre`}
                placeholder={TEMPLATE_PLACEHOLDER}
                value={value.template}
                onChange={(e) => onChange({ template: e.target.value })}
              />
              <p className={HINT}>
                {t('vaultFileTemplateHint', {
                  example: '{{ access_key }}',
                  filters: 'json, base64decode, trim, lines, indent N',
                  lease: '{{ _lease.ttl }}',
                })}
              </p>
            </div>
          )}

          {value.fileContent === 'format' && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label htmlFor={`${idPrefix}-file-format`} className={LABEL}>
                  {t('vaultFileFormat')}
                </label>
                <select
                  id={`${idPrefix}-file-format`}
                  className={SELECT}
                  value={value.format || 'json'}
                  onChange={(e) => onChange({ format: e.target.value })}
                >
                  <option value="json">{t('vaultFileFormatJson')}</option>
                  <option value="env">{t('vaultFileFormatEnv')}</option>
                </select>
              </div>
              <div>
                <label htmlFor={`${idPrefix}-file-fields`} className={LABEL}>
                  {t('vaultFileFields')}
                </label>
                <input
                  id={`${idPrefix}-file-fields`}
                  type="text"
                  className={FIELD}
                  placeholder="username, password" /* i18n-ignore: example Vault field names */
                  value={value.fields}
                  onChange={(e) => onChange({ fields: e.target.value })}
                />
                <p className={HINT}>{t('vaultFileFieldsHint')}</p>
              </div>
            </div>
          )}

          {value.fileContent === 'field' && (
            <div>
              <label htmlFor={`${idPrefix}-file-encoding`} className={LABEL}>
                {t('vaultFileEncoding')}
              </label>
              <select
                id={`${idPrefix}-file-encoding`}
                className={SELECT}
                value={value.encoding}
                onChange={(e) => onChange({ encoding: e.target.value })}
              >
                <option value="">{t('vaultFileEncodingNone')}</option>
                <option value="base64">{t('vaultFileEncodingBase64')}</option>
              </select>
              <p className={HINT}>{t('vaultFileEncodingHint')}</p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
