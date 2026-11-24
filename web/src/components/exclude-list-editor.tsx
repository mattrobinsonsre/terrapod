/**
 * The `exclude` write path, shared by the two surfaces that carry a workspace
 * selector (#2548).
 *
 * One component rather than two, because the thing it edits is one server
 * contract: `WorkspaceFilter.exclude` is a list of selectors, each accepting
 * the same dimensions the top-level filter does. The two call sites differ in
 * exactly two ways, and both are props: which dimensions they may offer, and
 * how their payload spells a key. Everything else -- the 20-entry cap, the
 * numbered rows, the zero-match warning -- is the contract's, not the surface's.
 *
 * ## What the rows are numbered for
 *
 * Every server refusal is INDEXED (`exclude[3] sets no selector`), and nothing
 * on either page mapped an index back to a row. So rows are numbered visibly
 * and the ones the server reports as matching nothing are highlighted, which is
 * what makes those messages actionable rather than a puzzle.
 *
 * ## The asymmetry this exists to make unreachable
 *
 * An exclusion matching nothing is a SOFT field on search and dry-run and a
 * HARD 422 on apply (`workspace_bulk.py`). So a preview can look clean and the
 * apply then refuse. The caller disables its own submit while any row is empty
 * and says why; the server keeps the 422 for API callers, who have no preview.
 *
 * Saving an assignment RULE is deliberately not refused for a zero-match entry
 * -- a standing rule may come to match later -- so the warning there is advice
 * rather than a gate, which is why the gating lives in the caller.
 */

'use client'

import { useTranslations } from 'next-intl'

import { HintWithDetail } from '@/components/hint-with-detail'
import { LabelsEditor } from '@/components/labels-editor'
import { AddButton, RemoveButton } from '@/components/template-editors'
import {
  BULK_EXCLUDE_DIMENSIONS,
  EXCLUDE_DIMENSIONS,
  MAX_EXCLUDE_ENTRIES,
  RULE_EXCLUDE_DIMENSIONS,
  entryIsEmpty,
  spellDimension,
  type ExcludeEntry,
} from '@/lib/exclude-dimensions'

// Re-exported so a call site needs one import, not two. The contract itself
// lives in `@/lib/exclude-dimensions` so it can be tested without a DOM.
export {
  BULK_EXCLUDE_DIMENSIONS,
  MAX_EXCLUDE_ENTRIES,
  RULE_EXCLUDE_DIMENSIONS,
  entryIsEmpty,
  type ExcludeEntry,
}

const INPUT =
  'mt-1 w-full min-w-0 px-2 py-1 text-sm border border-slate-600 rounded bg-slate-700 ' +
  'text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500'

export function ExcludeListEditor({
  value,
  onChange,
  dimensions,
  keyStyle,
  emptyRows = [],
  disabled = false,
}: {
  value: ExcludeEntry[]
  onChange: (next: ExcludeEntry[]) => void
  /** Canonical keys this surface may offer; see the two exported sets. */
  dimensions: string[]
  keyStyle: 'snake' | 'kebab'
  /** Row indices the server reported as matching nothing (`exclude_empty`). */
  emptyRows?: number[]
  disabled?: boolean
}) {
  const t = useTranslations('exclude')
  const offered = EXCLUDE_DIMENSIONS.filter((d) => dimensions.includes(d.key))
  const atCap = value.length >= MAX_EXCLUDE_ENTRIES

  function setEntry(i: number, next: ExcludeEntry) {
    onChange(value.map((e, j) => (j === i ? next : e)))
  }

  function setField(i: number, key: string, v: unknown) {
    const next: ExcludeEntry = { ...value[i] }
    const k = spellDimension(key, keyStyle)
    // Deleted rather than blanked, so an untouched control never becomes a
    // dimension. `owner_email: ""` is a real selector, so a caller who wants it
    // types a space and clears it -- the server's own asymmetry, not ours to
    // smooth over by keeping empty strings.
    if (v === undefined || v === '' || v === null) delete next[k]
    else next[k] = v
    setEntry(i, next)
  }

  function get(i: number, key: string): unknown {
    return value[i]?.[spellDimension(key, keyStyle)]
  }

  return (
    <div className="mt-4 min-w-0">
      <div className="flex items-baseline gap-2">
        <span className="text-xs font-medium text-slate-300">{t('title')}</span>
        {value.length > 0 && (
          <span className="text-xs text-slate-500">{t('count', { n: value.length })}</span>
        )}
      </div>
      {/* Two-tier: the lead says what an exclusion does and what an empty
          one costs; the mechanism (OR/AND, no nesting, the cap, the
          apply-time refusal) goes behind the icon. `HintWithDetail`
          directly rather than `tieredHint`, because the detail
          interpolates the cap and `tieredHint`'s `t` takes no values. */}
      <HintWithDetail
        lead={t('selectorLead')}
        detail={t('selectorHint', { max: MAX_EXCLUDE_ENTRIES })}
      />

      {value.length === 0 && <p className="mt-2 text-xs text-slate-600">{t('none')}</p>}

      <div className="mt-2 space-y-3">
        {value.map((entry, i) => {
          const flagged = emptyRows.includes(i)
          const blank = entryIsEmpty(entry)
          return (
            <fieldset
              key={i}
              disabled={disabled}
              data-testid={`exclude-row-${i}`}
              /* `min-w-0` is not cosmetic: a fieldset defaults to
                 `min-inline-size: min-content` and so refuses to shrink below
                 its content, which is what overflowed this page at 412px. */
              className={`min-w-0 rounded-lg border p-3 ${
                flagged || blank ? 'border-amber-700/60 bg-amber-950/10' : 'border-slate-700'
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <legend className="text-xs font-medium text-slate-400">
                  {t('entry', { n: i + 1 })}
                </legend>
                <RemoveButton onClick={() => onChange(value.filter((_, j) => j !== i))} />
              </div>

              {(flagged || blank) && (
                <p className="mt-1 text-xs text-amber-400">
                  {blank ? t('rowNoSelector') : t('rowMatchesNothing')}
                </p>
              )}

              <div className="mt-2 grid grid-cols-1 sm:grid-cols-2 gap-3">
                {offered.map((d) => (
                  <div key={d.key} className="min-w-0">
                    <label className="text-xs text-slate-500" htmlFor={`exclude-${i}-${d.key}`}>
                      {t(`dim.${d.key}`)}
                    </label>
                    {d.kind === 'labels' ? (
                      <div className="mt-1">
                        <LabelsEditor
                          labels={(get(i, d.key) as Record<string, string>) ?? {}}
                          onChange={(labels) =>
                            setField(i, d.key, Object.keys(labels).length ? labels : undefined)
                          }
                        />
                      </div>
                    ) : d.kind === 'idlist' ? (
                      <input
                        id={`exclude-${i}-${d.key}`}
                        type="text"
                        className={INPUT}
                        placeholder={d.placeholder} /* i18n-ignore: id prefix example */
                        value={((get(i, d.key) as string[]) ?? []).join(', ')}
                        onChange={(e) => {
                          const list = e.target.value
                            .split(',')
                            .map((s) => s.trim())
                            .filter(Boolean)
                          setField(i, d.key, list.length ? list : undefined)
                        }}
                      />
                    ) : d.kind === 'select' ? (
                      <select
                        id={`exclude-${i}-${d.key}`}
                        className={INPUT}
                        value={(get(i, d.key) as string) ?? ''}
                        onChange={(e) => setField(i, d.key, e.target.value)}
                      >
                        <option value="">{t('any')}</option>
                        {(d.options ?? []).map((o) => (
                          /* i18n-ignore: literal API value */
                          <option key={o} value={o}>
                            {o}
                          </option>
                        ))}
                      </select>
                    ) : d.kind === 'bool' ? (
                      <select
                        id={`exclude-${i}-${d.key}`}
                        className={INPUT}
                        value={
                          get(i, d.key) === undefined ? '' : (get(i, d.key) as boolean) ? 'yes' : 'no'
                        }
                        onChange={(e) =>
                          setField(
                            i,
                            d.key,
                            e.target.value === '' ? undefined : e.target.value === 'yes',
                          )
                        }
                      >
                        <option value="">{t('any')}</option>
                        <option value="yes">{t('yes')}</option>
                        <option value="no">{t('no')}</option>
                      </select>
                    ) : (
                      <input
                        id={`exclude-${i}-${d.key}`}
                        type="text"
                        className={INPUT}
                        placeholder={d.placeholder} /* i18n-ignore: syntax example */
                        value={(get(i, d.key) as string) ?? ''}
                        onChange={(e) => setField(i, d.key, e.target.value)}
                      />
                    )}
                  </div>
                ))}
              </div>
            </fieldset>
          )
        })}
      </div>

      {atCap ? (
        <p className="mt-2 text-xs text-slate-500">{t('atCap', { max: MAX_EXCLUDE_ENTRIES })}</p>
      ) : (
        <div className="mt-2">
          <AddButton
            label={t('add')}
            disabled={disabled}
            onClick={() => onChange([...value, {}])}
          />
        </div>
      )}
    </div>
  )
}
