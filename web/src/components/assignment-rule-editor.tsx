'use client'

/**
 * Editor for a variable set's workspace assignment rule (#1440).
 *
 * A rule selects workspaces by their attributes rather than binding each one by
 * hand, and membership re-evaluates on every run — so the question an operator
 * needs answered before saving is not "is this valid" but "who will this
 * reach". For a set carrying credentials that is the blast radius, and getting
 * it wrong is silent: the set simply starts applying to workspaces nobody
 * chose. Hence the live match count, which resolves the rule against the real
 * estate through the same selector the server will use.
 *
 * Every dimension the API accepts is present. One left out would be a dimension
 * the form silently drops on save, quietly widening or narrowing a rule the
 * operator set through the API or Terraform.
 */

import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { LabelsEditor } from '@/components/labels-editor'
import { apiFetch } from '@/lib/api'

export type AssignmentRule = Record<string, unknown>

const INPUT =
  'mt-1 w-full px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500'

/** Tri-state: unset means the dimension does not participate in the rule. */
function BoolFilter({
  label,
  value,
  onChange,
  anyLabel,
  yesLabel,
  noLabel,
}: {
  label: string
  value: boolean | undefined
  onChange: (v: boolean | undefined) => void
  anyLabel: string
  yesLabel: string
  noLabel: string
}) {
  return (
    <div>
      <label className="text-xs text-slate-500">{label}</label>
      <select
        className={INPUT}
        value={value === undefined ? '' : value ? 'yes' : 'no'}
        onChange={(e) =>
          onChange(e.target.value === '' ? undefined : e.target.value === 'yes')
        }
      >
        <option value="">{anyLabel}</option>
        <option value="yes">{yesLabel}</option>
        <option value="no">{noLabel}</option>
      </select>
    </div>
  )
}

export function AssignmentRuleEditor({
  rule,
  onChange,
  disabled = false,
}: {
  rule: AssignmentRule | null
  onChange: (rule: AssignmentRule | null) => void
  disabled?: boolean
}) {
  const t = useTranslations('adminVariableSets.detail')
  const enabled = rule !== null

  // Live blast-radius count, resolved server-side so the preview and the
  // eventual membership come from one selector rather than two.
  //
  // The result is stored tagged with the rule it describes, and matched against
  // the current rule during render. That is what makes a stale response
  // harmless — it simply stops being displayed — without an effect that resets
  // state on every edit, which would cascade renders on each keystroke.
  const [preview, setPreview] = useState<{
    key: string
    matched: number
    names: string[]
    error: string
  } | null>(null)

  const dimensions = Object.keys(rule ?? {}).length
  // Internal cache key, never shown to anyone.
  const ruleKey = enabled && dimensions > 0 ? JSON.stringify(rule) : ''

  const setField = useCallback(
    (key: string, value: unknown) => {
      const next: AssignmentRule = { ...(rule ?? {}) }
      if (value === undefined || value === '' || value === null) delete next[key]
      else next[key] = value
      onChange(next)
    },
    [rule, onChange],
  )

  useEffect(() => {
    if (!ruleKey) return
    // Debounced: the operator is typing into a glob or a label value, and each
    // keystroke is an intermediate rule that would match the wrong thing.
    let cancelled = false
    const id = setTimeout(async () => {
      try {
        const res = await apiFetch('/api/terrapod/v1/workspaces/actions/search', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filter: JSON.parse(ruleKey) }),
        })
        if (cancelled) return
        if (!res.ok) {
          setPreview({ key: ruleKey, matched: 0, names: [], error: 'invalid' })
          return
        }
        const data = await res.json()
        if (cancelled) return
        setPreview({
          key: ruleKey,
          matched: data.matched ?? 0,
          names: (data.workspaces ?? [])
            .map(
              (w: { name?: string; attributes?: { name?: string } }) =>
                w.name ?? w.attributes?.name ?? '',
            )
            .filter(Boolean),
          error: '',
        })
      } catch {
        if (!cancelled) setPreview({ key: ruleKey, matched: 0, names: [], error: 'invalid' })
      }
    }, 400)
    return () => {
      cancelled = true
      clearTimeout(id)
    }
  }, [ruleKey])

  // Only a result describing the rule on screen right now is shown.
  const current = preview && preview.key === ruleKey ? preview : null

  return (
    <div className="mt-4 pt-4 border-t border-slate-700/50">
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={enabled}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked ? {} : null)}
          className="rounded border-slate-600 bg-slate-700 text-brand-600"
        />
        <span className="text-sm font-medium text-slate-300">{t('assignmentRule')}</span>
      </label>
      <p className="mt-1 text-xs text-slate-500">
        {enabled ? t('assignmentRuleDescription') : t('assignmentRuleNone')}
      </p>

      {disabled && enabled && (
        <p className="mt-2 text-xs text-amber-400">{t('ruleConflictsWithGlobal')}</p>
      )}

      {enabled && !disabled && (
        <div className="mt-3 space-y-4">
          <div>
            <label className="text-xs text-slate-500">{t('ruleLabels')}</label>
            <div className="mt-1">
              <LabelsEditor
                labels={(rule?.labels as Record<string, string>) ?? {}}
                onChange={(labels) =>
                  setField('labels', Object.keys(labels).length ? labels : undefined)
                }
              />
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-slate-500">{t('ruleNamePrefix')}</label>
              <input
                type="text"
                className={INPUT}
                value={(rule?.name_prefix as string) ?? ''}
                onChange={(e) => setField('name_prefix', e.target.value)}
              />
            </div>
            <div>
              <label className="text-xs text-slate-500">{t('ruleNameGlob')}</label>
              <input
                type="text"
                className={INPUT}
                placeholder="*-prod-*" /* i18n-ignore: glob syntax example */
                value={(rule?.name_glob as string) ?? ''}
                onChange={(e) => setField('name_glob', e.target.value)}
              />
            </div>
            <div>
              <label className="text-xs text-slate-500">{t('ruleExecutionBackend')}</label>
              <select
                className={INPUT}
                value={(rule?.execution_backend as string) ?? ''}
                onChange={(e) => setField('execution_backend', e.target.value)}
              >
                <option value="">{t('ruleAny')}</option>
                <option value="terraform">{/* i18n-ignore: literal API value */}terraform</option>
                <option value="tofu">{/* i18n-ignore: literal API value */}tofu</option>
              </select>
            </div>
            <div>
              <label className="text-xs text-slate-500">{t('ruleExecutionMode')}</label>
              <select
                className={INPUT}
                value={(rule?.execution_mode as string) ?? ''}
                onChange={(e) => setField('execution_mode', e.target.value)}
              >
                <option value="">{t('ruleAny')}</option>
                <option value="local">{/* i18n-ignore: literal API value */}local</option>
                <option value="agent">{/* i18n-ignore: literal API value */}agent</option>
              </select>
            </div>
            <div>
              <label className="text-xs text-slate-500">{t('ruleTerraformVersion')}</label>
              <input
                type="text"
                className={INPUT}
                value={(rule?.terraform_version as string) ?? ''}
                onChange={(e) => setField('terraform_version', e.target.value)}
              />
            </div>
            <div>
              <label className="text-xs text-slate-500">{t('ruleOwnerEmail')}</label>
              <input
                type="text"
                className={INPUT}
                value={(rule?.owner_email as string) ?? ''}
                onChange={(e) => setField('owner_email', e.target.value)}
              />
            </div>
            <div>
              <label className="text-xs text-slate-500">{t('ruleAgentPoolId')}</label>
              <input
                type="text"
                className={INPUT}
                placeholder="apool-..." /* i18n-ignore: id format */
                value={(rule?.agent_pool_id as string) ?? ''}
                onChange={(e) => setField('agent_pool_id', e.target.value)}
              />
            </div>
            <div>
              <label className="text-xs text-slate-500">{t('ruleVcsConnectionId')}</label>
              <input
                type="text"
                className={INPUT}
                placeholder="vcs-..." /* i18n-ignore: id format */
                value={(rule?.vcs_connection_id as string) ?? ''}
                onChange={(e) => setField('vcs_connection_id', e.target.value)}
              />
            </div>
            <div>
              <label className="text-xs text-slate-500">{t('ruleDriftStatus')}</label>
              <select
                className={INPUT}
                value={(rule?.drift_status as string) ?? ''}
                onChange={(e) => setField('drift_status', e.target.value)}
              >
                <option value="">{t('ruleAny')}</option>
                <option value="drifted">{/* i18n-ignore: literal API value */}drifted</option>
                <option value="no_drift">{/* i18n-ignore: literal API value */}no_drift</option>
                <option value="unknown">{/* i18n-ignore: literal API value */}unknown</option>
              </select>
            </div>
            <BoolFilter
              label={t('ruleLocked')}
              value={rule?.locked as boolean | undefined}
              onChange={(v) => setField('locked', v)}
              anyLabel={t('ruleAny')}
              yesLabel={t('yes')}
              noLabel={t('no')}
            />
            <BoolFilter
              label={t('ruleHasVcs')}
              value={rule?.has_vcs as boolean | undefined}
              onChange={(v) => setField('has_vcs', v)}
              anyLabel={t('ruleAny')}
              yesLabel={t('yes')}
              noLabel={t('no')}
            />
          </div>

          {/* The blast radius, before saving rather than after. */}
          <div className="rounded-lg border border-slate-700/50 bg-slate-900/40 px-3 py-2">
            {dimensions === 0 ? (
              <p className="text-xs text-amber-400">{t('ruleEmpty')}</p>
            ) : current === null ? (
              <p className="text-xs text-slate-500">{t('rulePreviewLoading')}</p>
            ) : current.error ? (
              <p className="text-xs text-amber-400">{t('rulePreviewInvalid')}</p>
            ) : (
              <>
                <p className="text-xs text-slate-300">
                  {t('rulePreview', { count: current.matched })}
                </p>
                {current.names.length > 0 && (
                  <p className="mt-1 text-xs text-slate-500 break-words">
                    {current.names.slice(0, 12).join(', ')}
                    {current.names.length > 12 ? ` +${current.names.length - 12}` : ''}
                  </p>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * The rule as a sentence-ish list of conditions, for the read-only view.
 *
 * Renders each dimension through the same human labels the editor uses. The
 * operator who set the rule through this form should recognise what they typed
 * — never a serialised object, which is what the raw shape would leak.
 */
export function AssignmentRuleSummary({ rule }: { rule: AssignmentRule }) {
  const t = useTranslations('adminVariableSets.detail')

  const LABELS: Record<string, string> = {
    labels: t('ruleLabels'),
    name_prefix: t('ruleNamePrefix'),
    name_glob: t('ruleNameGlob'),
    execution_backend: t('ruleExecutionBackend'),
    execution_mode: t('ruleExecutionMode'),
    terraform_version: t('ruleTerraformVersion'),
    owner_email: t('ruleOwnerEmail'),
    agent_pool_id: t('ruleAgentPoolId'),
    vcs_connection_id: t('ruleVcsConnectionId'),
    drift_status: t('ruleDriftStatus'),
    locked: t('ruleLocked'),
    has_vcs: t('ruleHasVcs'),
  }

  const rows = Object.entries(rule).filter(([, v]) => v !== undefined && v !== null)
  if (rows.length === 0) return null

  return (
    <dl className="mt-2 space-y-1.5">
      {rows.map(([key, value]) => (
        <div key={key} className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <dt className="text-xs text-slate-500">{LABELS[key] ?? key}</dt>
          <dd className="flex flex-wrap items-center gap-1.5">
            {key === 'labels' && value && typeof value === 'object' ? (
              // Same chips the workspace list uses, so a label reads the same
              // wherever it appears.
              Object.entries(value as Record<string, string>).map(([k, v]) => (
                <span key={k} className="px-2 py-0.5 rounded bg-slate-700 text-xs text-slate-200">
                  {k}={v}
                </span>
              ))
            ) : typeof value === 'boolean' ? (
              <span className="px-2 py-0.5 rounded bg-slate-700 text-xs text-slate-200">
                {value ? t('yes') : t('no')}
              </span>
            ) : (
              <span className="px-2 py-0.5 rounded bg-slate-700 text-xs text-slate-200">
                {String(value)}
              </span>
            )}
          </dd>
        </div>
      ))}
    </dl>
  )
}
