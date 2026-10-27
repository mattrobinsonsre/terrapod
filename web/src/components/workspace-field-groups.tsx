'use client'

/**
 * The workspace field groups, rendered identically by the create form and the
 * Configuration tab (#2253).
 *
 * `workspace-fields.tsx` holds WHICH attribute belongs to which group; this
 * holds the markup. Split in two because the table is read by the parity gate
 * (a Python test, so it must stay a plain declaration a regex can see) while
 * this file is JSX the gate has no opinion about.
 *
 * THE CONTRACT, and the reason it is shaped this way: a group never owns how
 * it saves. The edit tab has three save semantics already -- the settings card
 * batches into local state behind an Edit/Save toggle, the drift card has
 * per-field actions plus `Check now`, and the AI/security cards PATCH
 * immediately on change with a read-only `<span>` when `can-update` is false
 * -- and the create form is a fourth, collecting everything for one POST. A
 * component that assumed any one of those would be unusable by the other
 * three, so every group takes `(mode, values, onChange)` and the page decides
 * what `onChange` means.
 *
 * `mode` is therefore the whole adaptation:
 *
 *   create form          mode="create"
 *   settings card        mode={editing ? 'edit' : 'view'}
 *   instant-PATCH cards  mode={perms['can-update'] ? 'edit' : 'view'}
 *
 * I18N. The labels come from `workspaceDetail.fields`, which already carries
 * 111 translated keys in all 33 catalogues -- so the create form inherits
 * edit's translations rather than needing ~50 new strings translated 33 times.
 * The namespace is named after the detail page and is read by both forms,
 * which looks odd and is deliberate: renaming it would churn 33 catalogues for
 * no user-visible gain, and duplicating it would put the same label in two
 * places to drift. Do not "fix" it by copying the keys.
 */

import type { ReactNode } from 'react'
import {
  CREATE_INPUT,
  EDIT_INPUT,
  FIELD_BY_ATTR,
  WsField,
  dependencyMet,
  groupFields,
  groupSummary,
  type FieldMode,
  type GroupId,
} from '@/components/workspace-fields'

/** What a page hands the groups. */
export interface GroupCtx {
  mode: FieldMode
  /** Current values, keyed as the API names the attribute. */
  values: Record<string, unknown>
  onChange: (attr: string, value: unknown) => void
  /** Which engine, so a field that does not apply is not rendered. */
  engine: string
  /** Label for an attribute, from the caller's `useTranslations`. */
  label: (attr: string) => string
  /** Optional hint under a field. */
  hint?: (attr: string) => ReactNode
  /** Select options a page loads at runtime (pools, VCS connections, EEs). */
  options?: Record<string, { value: string; label: string }[]>
  /** Attributes this caller may not change, rendered disabled rather than hidden. */
  readOnly?: ReadonlySet<string>
  /** Disables every control, e.g. while a save is in flight. */
  busy?: boolean
}

const inputClass = (mode: FieldMode) => (mode === 'create' ? CREATE_INPUT : EDIT_INPUT)

/** Whether this field is rendered at all: engine applies, and its gate is on. */
function shown(attr: string, ctx: GroupCtx): boolean {
  const spec = FIELD_BY_ATTR[attr]
  if (!spec) return false
  if (spec.engines && !spec.engines.includes(ctx.engine)) return false
  return dependencyMet(spec, ctx.values)
}

/**
 * One control, chosen by kind.
 *
 * Deliberately a small set of primitives rather than a schema-driven renderer:
 * this tree has no form library and inventing one here would make every future
 * field a fight with an abstraction. The primitives keep the 50 fields
 * consistent; anything genuinely bespoke (the version datalist, the list
 * editors) stays hand-written in its group.
 */
function Control({
  attr,
  ctx,
  kind,
  options,
  placeholder,
  min,
  max,
}: {
  attr: string
  ctx: GroupCtx
  kind: 'text' | 'number' | 'select' | 'checkbox' | 'list'
  options?: { value: string; label: string }[]
  placeholder?: string
  min?: number
  max?: number
}) {
  const spec = FIELD_BY_ATTR[attr]
  const id = spec?.id ?? attr
  const v = ctx.values[attr]
  const disabled = ctx.busy || ctx.readOnly?.has(attr)
  const cls = inputClass(ctx.mode)

  if (kind === 'list') {
    // One entry per line. Deliberately plain: the two forms set the same five
    // list-valued attributes, and a bespoke add/remove editor per attribute is
    // what the edit tab already has -- those pass `control` below rather than
    // being reimplemented here, so this primitive serves the create side and
    // anything that does not need per-row controls. Blank lines are dropped on
    // the way out, so a trailing newline is not an empty entry the API refuses.
    const asLines = Array.isArray(v) ? (v as unknown[]).map(String).join('\n') : ''
    return (
      <textarea
        id={id}
        data-testid={id}
        rows={3}
        value={asLines}
        placeholder={placeholder}
        disabled={disabled}
        onChange={(e) =>
          ctx.onChange(
            attr,
            e.target.value
              .split('\n')
              .map((s) => s.trim())
              .filter((s) => s !== ''),
          )
        }
        className={`${cls} font-mono`}
      />
    )
  }
  if (kind === 'checkbox') {
    return (
      <input
        id={id}
        data-testid={id}
        type="checkbox"
        checked={Boolean(v)}
        disabled={disabled}
        onChange={(e) => ctx.onChange(attr, e.target.checked)}
        className="h-4 w-4 rounded border-slate-600 bg-slate-700 text-brand-500 focus:ring-1 focus:ring-brand-500"
      />
    )
  }
  if (kind === 'select') {
    return (
      <select
        id={id}
        data-testid={id}
        value={String(v ?? '')}
        disabled={disabled}
        onChange={(e) => ctx.onChange(attr, e.target.value)}
        className={cls}
      >
        {(options ?? []).map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    )
  }
  return (
    <input
      id={id}
      data-testid={id}
      type={kind === 'number' ? 'number' : 'text'}
      value={v === undefined || v === null ? '' : String(v)}
      placeholder={placeholder}
      min={min}
      max={max}
      disabled={disabled}
      onChange={(e) =>
        ctx.onChange(attr, kind === 'number' ? Number(e.target.value) : e.target.value)
      }
      className={cls}
    />
  )
}

/** A field plus its control, or nothing when the field does not apply here. */
export function Field({
  attr,
  ctx,
  kind,
  options,
  display,
  placeholder,
  min,
  max,
  control,
}: {
  attr: string
  ctx: GroupCtx
  kind: 'text' | 'number' | 'select' | 'checkbox' | 'list'
  options?: { value: string; label: string }[]
  /** What `view` mode shows; defaults to the raw value. */
  display?: ReactNode
  placeholder?: string
  min?: number
  max?: number
  /**
   * A bespoke control in place of the primitive.
   *
   * The escape hatch that keeps this from becoming a form framework: the edit
   * tab has real per-row editors for the path lists and uses
   * `labels-editor.tsx` for labels, and reimplementing those here would make
   * every future field an argument with an abstraction. A field passing
   * `control` still gets the group's label, view mode, hint and dependency
   * hiding -- only the input differs.
   */
  control?: ReactNode
}) {
  if (!shown(attr, ctx)) return null
  const spec = FIELD_BY_ATTR[attr]
  const v = ctx.values[attr]
  const shownValue =
    display ??
    (kind === 'checkbox'
      ? Boolean(v)
        ? '✓'
        : '—'
      : kind === 'list'
        ? Array.isArray(v) && v.length > 0
          ? (v as unknown[]).map(String).join(', ')
          : '—'
        : v === undefined || v === ''
          ? '—'
          : String(v))
  return (
    <WsField
      id={spec?.id ?? attr}
      label={ctx.label(attr)}
      mode={ctx.mode}
      display={shownValue}
      hint={ctx.hint?.(attr)}
    >
      {control ?? (
        <Control
          attr={attr}
          ctx={ctx}
          kind={kind}
          options={options}
          placeholder={placeholder}
          min={min}
          max={max}
        />
      )}
    </WsField>
  )
}

/**
 * The wrapper every group renders inside.
 *
 * A plain `<section>` for a standard group; a `<details>` for an advanced one,
 * whose summary carries what the group holds so a collapsed group cannot
 * conceal a value. The indicator is derived from the values -- a count for a
 * list, set/unset for an optional scalar -- and never from a defaults table;
 * `workspace-fields.tsx` explains why at `Summary`.
 *
 * `<details>` rather than a `useState` disclosure so the browser owns the
 * open/closed state: it needs no URL round-trip, survives nothing, and is
 * exactly the kind of transient view state the URL-as-source-of-truth rule is
 * NOT about. `min-w-0` on the grid, because a grid item will not shrink below
 * its content without it -- the single CSS line behind a phone-width overflow
 * this repo has already met once on the autodiscovery page's own `<details>`.
 */
export function Group({
  id,
  ctx,
  title,
  description,
  advanced,
  children,
}: {
  id: GroupId
  ctx: GroupCtx
  title: string
  description?: ReactNode
  advanced?: boolean
  children: ReactNode
}) {
  // Nothing applies to this engine, or every field is behind an off gate.
  const any = groupFields(id, ctx.engine).some((f) => shown(f.attr, ctx))
  if (!any) return null

  const body = (
    <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4 min-w-0" data-testid={`ws-group-${id}`}>
      {children}
    </dl>
  )

  if (!advanced) {
    return (
      <section className="min-w-0">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
          {title}
        </h4>
        {description}
        {body}
      </section>
    )
  }

  const held = groupSummary(id, ctx.engine, ctx.values, ctx.label)
  return (
    <details className="min-w-0 rounded-lg border border-slate-700/60" data-testid={`ws-adv-${id}`}>
      <summary className="cursor-pointer select-none px-3 py-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
        {title}
        {held.length > 0 && (
          <span className="ml-2 normal-case font-normal text-slate-500" data-testid={`ws-adv-${id}-held`}>
            {held.map((h) => h.text).join(' · ')}
          </span>
        )}
      </summary>
      <div className="px-3 pb-3 min-w-0">
        {description}
        {body}
      </div>
    </details>
  )
}
