'use client'

import { useTranslations } from 'next-intl'

export interface ModuleInterfaceInput {
  name: string
  type: string
  description: string
  default: string | null
  required: boolean
  sensitive: boolean
}

export interface ModuleInterfaceOutput {
  name: string
  description: string
  sensitive: boolean
}

// Why a module version's interface could not be read (#1707). The reason is
// the server's short summary (a file name and position, never source text), so
// it is shown as-is; the hint says what the empty or partial surface means on
// the page showing it.
export function ModuleInterfaceError({ reason, hint }: { reason: string; hint: string }) {
  const t = useTranslations('registry')
  return (
    <div
      role="alert"
      data-testid="module-interface-error"
      className="rounded-lg border border-amber-700/60 bg-amber-900/20 px-4 py-3 text-sm text-amber-200"
    >
      <p className="font-medium">{t('moduleDetail.interface.errorTitle')}</p>
      <p className="mt-1 font-mono text-xs break-words text-amber-100">{reason}</p>
      <p className="mt-1 text-xs text-amber-300">{hint}</p>
    </div>
  )
}

// The inputs and outputs of a module version, shared by the module registry
// page and the catalog item page (#1585) so both show a module's surface the
// same way. Null for both means the interface was never extracted. When
// `interfaceError` is set (#1707) empty lists are a failed parse, not a module
// that declares nothing, so "declares no inputs or outputs" is not claimed.
export function ModuleInterfaceTables({
  inputs,
  outputs,
  interfaceError = null,
}: {
  inputs: ModuleInterfaceInput[] | null
  outputs: ModuleInterfaceOutput[] | null
  interfaceError?: string | null
}) {
  const t = useTranslations('registry')

  if (inputs === null && outputs === null) {
    return <p className="text-sm text-slate-500">{t('moduleDetail.interface.noData')}</p>
  }

  return (
    <>
      {inputs && inputs.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">{t('moduleDetail.interface.inputs')}</h4>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-slate-500 border-b border-slate-700/50">
                  <th className="text-start py-2 pe-4">{t('moduleDetail.interface.name')}</th>
                  <th className="text-start py-2 pe-4">{t('moduleDetail.interface.type')}</th>
                  <th className="text-start py-2 pe-4">{t('moduleDetail.interface.description')}</th>
                  <th className="text-start py-2 pe-4">{t('moduleDetail.interface.default')}</th>
                  <th className="text-start py-2">{t('moduleDetail.interface.required')}</th>
                </tr>
              </thead>
              <tbody>
                {inputs.map((inp) => (
                  <tr key={inp.name} className="border-b border-slate-700/30">
                    <td className="py-2 pe-4 font-mono text-xs text-slate-200">{inp.name}</td>
                    <td className="py-2 pe-4 font-mono text-xs text-slate-400">{inp.type}</td>
                    <td className="py-2 pe-4 text-slate-300">{inp.description}</td>
                    <td className="py-2 pe-4 font-mono text-xs text-slate-400">{inp.default ?? '—'}</td>
                    <td className="py-2">
                      {inp.required ? (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900/50 text-amber-300">{t('moduleDetail.interface.requiredBadge')}</span>
                      ) : (
                        <span className="text-xs text-slate-500">{t('moduleDetail.interface.optionalBadge')}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {outputs && outputs.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">{t('moduleDetail.interface.outputs')}</h4>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-slate-500 border-b border-slate-700/50">
                  <th className="text-start py-2 pe-4">{t('moduleDetail.interface.name')}</th>
                  <th className="text-start py-2 pe-4">{t('moduleDetail.interface.description')}</th>
                  <th className="text-start py-2">{t('moduleDetail.interface.sensitive')}</th>
                </tr>
              </thead>
              <tbody>
                {outputs.map((out) => (
                  <tr key={out.name} className="border-b border-slate-700/30">
                    <td className="py-2 pe-4 font-mono text-xs text-slate-200">{out.name}</td>
                    <td className="py-2 pe-4 text-slate-300">{out.description}</td>
                    <td className="py-2">
                      {out.sensitive && (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-900/50 text-red-300">{t('moduleDetail.interface.sensitiveBadge')}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {!interfaceError && inputs?.length === 0 && outputs?.length === 0 && (
        <p className="text-sm text-slate-500">{t('moduleDetail.interface.noneDeclared')}</p>
      )}
    </>
  )
}
