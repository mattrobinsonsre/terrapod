'use client'

/**
 * The SSH host-key policy as one ordered select (#2592).
 *
 * Replaces a checkbox, and the shape change is the point rather than tidying.
 * OpenSSH has three behaviours and the checkbox could only name two, so the
 * state a growing fleet actually wants — trust a host on first contact, refuse
 * it once its key changes — was unreachable from the UI. Ansible cannot express
 * that state at all, which is why the attribute stopped being named after
 * ansible's flag: it is `StrictHostKeyChecking`, not `HOST_KEY_CHECKING`.
 *
 * ## Why the middle option exists, since it is the one an operator has to be
 * told about
 *
 * Approved keys are declared per host, and Terrapod renders them as the
 * `known_hosts` file OpenSSH checks against. That file is an ALLOW-LIST and the
 * setting is process-wide, so under `strict` declaring one approved key makes
 * every address in that inventory nobody has pinned unreachable — not merely
 * unverified. `accept-new` is the mixed-fleet answer: the pinned hosts stay
 * strictly verified against their approved keys and the unpinned ones stay
 * reachable. That is the whole reason this is a select and not a checkbox, and
 * it is what the lead and the detail behind the ⓘ say between them.
 *
 * ## Where it is composed, and where it deliberately is not
 *
 * Shared by workspace create, the workspace Configuration tab and the
 * autodiscovery rule template, exactly as `DynamicInventorySelect` is: all
 * three set a CONCRETE value, which is what makes one select the right control.
 *
 * **Bulk update keeps its own**, and for the reason recorded there rather than
 * for want of sharing: its values carry a fourth state, `''` for "leave
 * unchanged", which this control has no room for and must not acquire — a
 * create form whose policy could be unset would write nothing and leave the
 * column at whatever the server chose.
 */

import { useTranslations } from 'next-intl'
import {
  SSH_HOST_KEY_POLICIES,
  asSshHostKeyPolicy,
  type SshHostKeyPolicy,
} from '@/lib/ssh-host-keys'
import { CREATE_INPUT, EDIT_INPUT } from '@/lib/workspace-fields'
import type { GroupCtx } from '@/components/workspace-field-groups'

/**
 * The label for one policy.
 *
 * A plain function taking the translator, the shape `tieredHint` uses, rather
 * than a hook: the Configuration tab's read view needs the same word, and the
 * place it needs it is below an early return, where a hook cannot be called.
 * One function means the picker and the read-back cannot come to disagree about
 * what `accept-new` is called.
 *
 * Each key spelled LITERALLY rather than looked up from a map, the same way
 * `DynamicInventorySelect` spells its four: a computed `t(KEY[p])` is invisible
 * to `i18n:check`, which verifies that every REQUESTED key resolves in all
 * thirty-three catalogues — so a missing translation would reach the browser as
 * a raw key instead of failing the build.
 */
export function sshHostKeyPolicyLabel(
  t: (key: string) => string,
  policy: SshHostKeyPolicy,
): string {
  return policy === 'strict'
    ? t('fields.sshHostKeyPolicyStrict')
    : policy === 'accept-new'
      ? t('fields.sshHostKeyPolicyAcceptNew')
      : t('fields.sshHostKeyPolicyOff')
}

export function SshHostKeyPolicySelect({ ctx }: { ctx: GroupCtx }) {
  const t = useTranslations('workspaceDetail')
  // Read through the coercion rather than cast: the three surfaces seed this
  // from an API response, and an attribute an older server omits — or sends as
  // the boolean this replaced — must land on a real option rather than render
  // the select blank and lose the setting on the next save.
  const policy = asSshHostKeyPolicy(ctx.values['ssh-host-key-policy'])

  return (
    <select
      id="ws-ssh-host-key-policy"
      data-testid="ws-ssh-host-key-policy"
      value={policy}
      onChange={(e) => ctx.onChange('ssh-host-key-policy', e.target.value as SshHostKeyPolicy)}
      disabled={ctx.busy || ctx.readOnly?.has('ssh-host-key-policy')}
      className={ctx.mode === 'create' ? CREATE_INPUT : EDIT_INPUT}
    >
      {SSH_HOST_KEY_POLICIES.map((p) => (
        <option key={p} value={p}>
          {sshHostKeyPolicyLabel(t, p)}
        </option>
      ))}
    </select>
  )
}
