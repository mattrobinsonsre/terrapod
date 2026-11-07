'use client'

/**
 * A field hint in two tiers (#2413 F15).
 *
 * Every control keeps a SHORT always-on line; where the full explanation is
 * long it carries an ⓘ that opens the rest. The maintainer's framing: *"we
 * could have a very limited always-on hint and then an i icon on that for a
 * more detailed explanation where the full explanation is long"*, and *"yes
 * always click. I don't like hover."*
 *
 * ## The split is by CONTENT, not by length
 *
 * Visible: what the control does, and — for anything that relaxes a protection
 * — the consequence of relaxing it. Behind the icon: mechanism, precedence,
 * and the alternatives. So an operator never has to open anything to learn that
 * a setting lets third-party code run on a runner; they open it to learn how.
 *
 * ## Click, never hover
 *
 * A hover tooltip is unreachable on touch and invisible to a keyboard, and the
 * native `title=` attribute is both — which is why `AGENTS.md` forbids it for
 * explanation and why this component exists rather than another `title`.
 *
 * The detail is a plain in-flow block rather than a floating popover,
 * deliberately: a popover needs collision handling, a portal and a focus trap
 * to be correct, and at phone width it would cover the control it describes. An
 * expanding block pushes the form down, which is the one behaviour that cannot
 * hide anything.
 */

import { useId, useState } from 'react'
import { useTranslations } from 'next-intl'
import type { ReactNode } from 'react'
import { HINT_LEADS } from '@/lib/hint-tiers'

export function HintWithDetail({ lead, detail }: { lead: ReactNode; detail: ReactNode }) {
  const t = useTranslations('common')
  const [open, setOpen] = useState(false)
  const id = useId()

  return (
    <div className="mt-1 min-w-0 text-xs text-slate-400">
      <span>{lead}</span>{' '}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={id}
        // The accessible name says what opening it gets you. "More
        // information" would be true of every one of these on a page.
        aria-label={open ? t('hideDetail') : t('showDetail')}
        data-testid="hint-detail-toggle"
        // 44px on touch, collapsing on a precise pointer: it is the smallest
        // control on a form full of inputs, so it is the one most likely to be
        // missed with a thumb.
        className="inline-flex min-h-11 min-w-11 items-center justify-center rounded text-brand-400 hover:text-brand-300 sm:min-h-0 sm:min-w-0 sm:align-baseline"
      >
        <span aria-hidden="true" className="text-sm leading-none">
          &#9432;
        </span>
      </button>
      {open && (
        <div
          id={id}
          data-testid="hint-detail"
          // In flow, so it pushes rather than covers. `break-words` because
          // these carry identifiers and paths that do not wrap on their own.
          className="mt-1 min-w-0 break-words rounded-lg border border-slate-700/60 bg-slate-900/40 p-2 text-xs text-slate-300"
        >
          {detail}
        </div>
      )}
    </div>
  )
}

/**
 * A hint, two-tier if it is long enough to need it.
 *
 * One resolver so a surface's hint code does not have to know which hints are
 * long — it passes the key and gets the right thing. A key with no lead in
 * `HINT_LEADS` falls back to the plain paragraph every short hint already used,
 * so this is safe to call for every hint rather than only the long ones, and a
 * hint that becomes long later needs a registry entry and nothing else.
 *
 * `t` is passed in rather than bound here because the keys span namespaces:
 * `workspaceDetail` on three surfaces and `ansibleCredentials` in the
 * credential fields.
 */
export function tieredHint(
  t: (key: string) => string,
  key: string,
  trailing?: ReactNode,
): ReactNode {
  const lead = HINT_LEADS[key]
  if (!lead)
    return (
      <p className="mt-1 text-xs text-slate-500">
        {t(key)}
        {trailing}
      </p>
    )
  // `trailing` rides on the LEAD rather than the detail: it is a live
  // indicator ("Saving…"), so it has to stay visible whether or not the
  // detail happens to be open.
  return (
    <HintWithDetail
      lead={
        <>
          {t(lead)}
          {trailing}
        </>
      }
      detail={t(key)}
    />
  )
}
