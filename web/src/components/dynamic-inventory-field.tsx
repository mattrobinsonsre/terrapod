'use client'

/**
 * Dynamic inventory as one ordered select (#2413 F19).
 *
 * Replaces two adjacent booleans whose combination nobody could reason about,
 * one of which permits an arbitrary executable from the repository to run in
 * the runner Job — which is not the same as being the only settings that let
 * third-party code run there, for the reason `lib/dynamic-inventory.ts` gives.
 * Four options
 * rather than the finding's three, because `scripts` without `plugins` is a
 * real configuration — see `lib/dynamic-inventory.ts` for the measurement.
 *
 * Shared by workspace create, the workspace Configuration tab and the
 * autodiscovery rule template: all three set a CONCRETE value, which is what
 * makes one select the right control. Bulk update deliberately keeps its two
 * separate tri-states, because its third state is "leave unchanged" and it is
 * per field — composing there would make "permit declared plugins across the
 * fleet and leave scripts as each workspace has them" unexpressible.
 *
 * It writes BOTH attributes through `ctx.onChange`, so the surfaces keep their
 * existing state and payload and nothing on the server changes. The two
 * attributes are still the contract; this is a control over them.
 */

import { useTranslations } from 'next-intl'
import {
  DYNAMIC_INVENTORY_MODES,
  flagsFromMode,
  modeFromFlags,
  type DynamicInventoryMode,
} from '@/lib/dynamic-inventory'
import { CREATE_INPUT, EDIT_INPUT } from '@/lib/workspace-fields'
import type { GroupCtx } from '@/components/workspace-field-groups'


export function DynamicInventorySelect({ ctx }: { ctx: GroupCtx }) {
  const t = useTranslations('workspaceDetail')
  const plugins = ctx.values['dynamic-inventory-enabled'] === true
  const scripts = ctx.values['inventory-scripts-enabled'] === true
  const mode = modeFromFlags(plugins, scripts)

  return (
    <select
      id="ws-dynamic-inventory-mode"
      data-testid="ws-dynamic-inventory-mode"
      value={mode}
      // Both halves of the gate, every time. Writing only the one that changed
      // would leave the other at whatever it was, so picking `Declared plugins`
      // from `Executable scripts` would silently keep the scripts grant -- which
      // is the combination this control exists to make legible.
      onChange={(e) => {
        const { plugins: p, scripts: s } = flagsFromMode(e.target.value as DynamicInventoryMode)
        ctx.onChange('dynamic-inventory-enabled', p)
        ctx.onChange('inventory-scripts-enabled', s)
      }}
      disabled={
        ctx.busy ||
        ctx.readOnly?.has('dynamic-inventory-enabled') ||
        ctx.readOnly?.has('inventory-scripts-enabled')
      }
      className={ctx.mode === 'create' ? CREATE_INPUT : EDIT_INPUT}
    >
      {DYNAMIC_INVENTORY_MODES.map((m) => (
        <option key={m} value={m}>
          {/* Each key spelled LITERALLY rather than looked up from a map. A
              computed `t(KEY[m])` is invisible to `i18n:check`, which verifies
              that every REQUESTED key resolves in all 32 catalogues -- so a
              missing translation would reach the browser as a raw key instead
              of failing the build. The map read better and cost the gate. */}
          {m === 'none'
            ? t('fields.dynamicInventoryModeNone')
            : m === 'plugins'
              ? t('fields.dynamicInventoryModePlugins')
              : m === 'scripts'
                ? t('fields.dynamicInventoryModeScripts')
                : t('fields.dynamicInventoryModeBoth')}
        </option>
      ))}
    </select>
  )
}
