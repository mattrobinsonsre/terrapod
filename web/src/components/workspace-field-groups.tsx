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
  fieldApplies,
  fieldGateMet,
  groupFields,
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
  nullable,
}: {
  attr: string
  ctx: GroupCtx
  kind: 'text' | 'number' | 'select' | 'checkbox' | 'list'
  options?: { value: string; label: string }[]
  placeholder?: string
  min?: number
  max?: number
  /**
   * Empty box means `null` rather than `0`, for a number whose unset state is a
   * real value -- `configure_parallelism`'s unset means "inherit parallelism"
   * (#2499).
   *
   * Needed because `Number('')` is `0`, so without it clearing the box sends a
   * zero. For this setting the server REFUSES zero deliberately (terraform
   * reads `-parallelism=0` as "no limit"), so the box would 422 on being
   * cleared -- and for any other nullable number it would silently store 0
   * where the author meant "unset".
   */
  nullable?: boolean
}) {
  const spec = FIELD_BY_ATTR[attr]
  const id = spec?.id ?? attr
  const v = ctx.values[attr]
  const disabled = ctx.busy || ctx.readOnly?.has(attr) || !fieldGateMet(attr, ctx.values)
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
      onChange={(e) => {
        if (kind !== 'number') {
          ctx.onChange(attr, e.target.value)
          return
        }
        // `Number('')` is 0, so an empty box has to be handled before the
        // coercion rather than after it.
        if (nullable && e.target.value === '') {
          ctx.onChange(attr, null)
          return
        }
        ctx.onChange(attr, Number(e.target.value))
      }}
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
  nullable,
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
  /** Empty box means `null` rather than `0`; see `Control` above (#2499). */
  nullable?: boolean
  /**
   * A bespoke control in place of the primitive.
   *
   * The escape hatch that keeps this from becoming a form framework: the edit
   * tab has real per-row editors for the path lists and uses
   * `labels-editor.tsx` for labels, and reimplementing those here would make
   * every future field an argument with an abstraction. A field passing
   * `control` still gets the group's label, view mode, hint and -- since
   * #2413 F20 -- the dependency gate, applied by wrapping it in a disabled
   * `<fieldset>` rather than by the caller remembering to. Only the input
   * differs.
   */
  control?: ReactNode
}) {
  if (!fieldApplies(attr, ctx.engine)) return null
  const spec = FIELD_BY_ATTR[attr]
  const gateMet = fieldGateMet(attr, ctx.values)
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
      {control ? (
        gateMet ? (
          control
        ) : (
          /* A bespoke control builds its own `<input>`, so `Control`'s own
           * `disabled` cannot reach it -- and 38 call sites across the two
           * forms pass one. Wrapping in a disabled `<fieldset>` disables every
           * descendant control by DOM ancestry, so the gate holds for the list
           * editors, the version datalists and the Enabled/Disabled toggles
           * without each of them having to remember it. This is what the e2e
           * caught: the toggles rendered under #2413 F20 and accepted input.
           *
           * Only wrapped when the gate is OFF, which is a state that rendered
           * nothing at all before F20 -- so the extra element cannot change a
           * layout that already works. `min-w-0` because a fieldset defaults
           * to `min-inline-size: min-content` and will not shrink at phone
           * width; the rest zeroes its default chrome. */
          <fieldset disabled className="min-w-0 border-0 p-0 m-0">
            {control}
          </fieldset>
        )
      ) : (
        <Control
          attr={attr}
          ctx={ctx}
          kind={kind}
          options={options}
          placeholder={placeholder}
          min={min}
          max={max}
          nullable={nullable}
        />
      )}
    </WsField>
  )
}

/**
 * The wrapper every group renders inside: a plain `<section>` with a heading,
 * always.
 *
 * **Nothing collapses, anywhere** (#2413 F4). This used to render a `<details>`
 * for a group whose every field was `advanced`, with a derived summary so a
 * collapsed group could not conceal a value -- careful machinery for a
 * mechanism that is now withdrawn. The maintainer's decision: *"sections make
 * the complex forms navigable even without collapsing or hiding anything and I
 * stand by that"*. Sections carry navigability; collapse does not, and it costs
 * an interaction before an operator can see what a form offers.
 *
 * So `advanced`, `summary`, `groupIsAdvanced`, `groupSummary` and the `Summary`
 * type all went with it rather than being left unread -- a flag nothing reads
 * is this repository's most repeated defect, and the one that had a derived
 * summary attached was going to read as load-bearing for a long time.
 *
 * `min-w-0` on the grid stays, and is not a leftover: a grid item will not
 * shrink below its content without it, which is the single CSS line behind a
 * phone-width overflow this repo has already met once.
 */
export function Group({
  id,
  ctx,
  title,
  description,
  children,
}: {
  id: GroupId
  ctx: GroupCtx
  title: string
  description?: ReactNode
  children: ReactNode
}) {
  // Nothing here applies to this engine. A group whose every field is merely
  // behind an OFF GATE still renders, with those fields disabled (#2413 F20).
  const any = groupFields(id, ctx.engine).some((f) => fieldApplies(f.attr, ctx.engine))
  if (!any) return null

  const body = (
    <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4 min-w-0" data-testid={`ws-group-${id}`}>
      {children}
    </dl>
  )

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
