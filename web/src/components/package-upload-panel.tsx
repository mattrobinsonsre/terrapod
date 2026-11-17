'use client'

import { useMemo, useState } from 'react'
import { useTranslations } from 'next-intl'
import { apiFetch, parseApiError } from '@/lib/api'
import { useConfirm } from '@/lib/use-confirm'

/**
 * Uploading an artifact an operator vetted themselves (#2599).
 *
 * The third posture beside the pull-through and the warm, and the only one that
 * works while the node is SEALED. It is not a weaker form of verification: it is
 * a different claim. Admission-time verification (#2589) answers "did this come
 * from the publisher it claims"; an operator who has already approved an
 * artifact is asserting that it may be served. So this form asks for no
 * signature, and asking for one would defeat it -- the likeliest upload is
 * something built, patched or re-packaged in-house that no upstream key covers.
 *
 * **The coordinates are never checked against an upstream index**, deliberately.
 * An internal package has no upstream entry to check, which is the case this
 * exists for, so a validated-against-upstream form would refuse exactly the
 * artifact it is meant to accept.
 *
 * ## Why the fields are data rather than branches
 *
 * Six ecosystems, each with its own coordinate scheme, and four of them also
 * take an index document. Written as JSX branches that is ten nearly-identical
 * forms, and the failure mode is silent: a field posted under the wrong name
 * lands the artifact at a key no install ever asks for and still reports
 * success. So each shape is one row in SHAPES, and the form is rendered from it.
 *
 * ## The index documents, which are the half that is easy to miss
 *
 * An upload supplies BYTES. A sealed node answers "what versions exist" from a
 * cached index document and nowhere else, so for npm, Galaxy, NuGet and Go an
 * uploaded artifact with no document beside it is present, recorded, and
 * unresolvable. The form says so where the operator chooses, rather than leaving
 * it to the docs.
 *
 * PyPI and Pulumi offer no document and that absence is correct: PyPI builds its
 * sealed index from the rows it holds, and Pulumi plugins are addressed by
 * filename.
 *
 * ## Streaming
 *
 * The body goes through the BFF's `/api/*` Route Handler, which forwards
 * `request.body` unbuffered with `duplex: 'half'`. That is the existing path a
 * config-version tarball already takes, so a hundreds-of-MB provider archive is
 * carried rather than buffered -- confirmed rather than designed (#2599).
 */

/** One coordinate field. `hint` is an i18n key under `upload.hints`, used only
 *  where the value is not self-evident from its label. */
interface Field {
  name: string
  hint?: boolean
}

interface Shape {
  /** The ecosystem segment of the route, and the value the server keys on. */
  ecosystem: string
  /** The artifact route's coordinate fields. */
  artifact: Field[]
  /** The index route's fields, or null where the ecosystem has no index. */
  index: Field[] | null
}

/** Every upload shape, in the order the listing shows the ecosystems.
 *
 *  The field NAMES are the server's form field names and are the contract: each
 *  ecosystem's own `upload_coordinates` reads exactly these. A renamed field
 *  here is a 422 at best and a wrong cache key at worst, which is why
 *  `package-upload-shapes.test.ts` holds this table against the route
 *  contract rather than trusting it. */
export const SHAPES: Shape[] = [
  {
    ecosystem: 'pypi',
    // The filename is SUPPLIED rather than derived: a wheel's name encodes its
    // compatibility tags and only whoever built it knows them.
    artifact: [{ name: 'project' }, { name: 'filename', hint: true }],
    index: null,
  },
  {
    ecosystem: 'npm',
    // No filename: npm's own registry derives it from name and version, and the
    // serve path matches on the derived form.
    artifact: [{ name: 'name' }, { name: 'version' }],
    index: [{ name: 'name' }],
  },
  {
    ecosystem: 'nuget',
    artifact: [{ name: 'package_id' }, { name: 'version' }],
    index: [{ name: 'package_id' }],
  },
  {
    ecosystem: 'go',
    // A module version is THREE files to the proxy protocol, uploaded in their
    // own right exactly as each is fetched in its own right.
    artifact: [{ name: 'module' }, { name: 'version' }, { name: 'suffix', hint: true }],
    index: [{ name: 'module' }, { name: 'document', hint: true }],
  },
  {
    ecosystem: 'galaxy',
    artifact: [{ name: 'namespace' }, { name: 'name' }, { name: 'version' }],
    // Two documents: a sealed node reads the collection record and the version
    // list separately, so one without the other leaves half the path 404ing.
    index: [{ name: 'namespace' }, { name: 'name' }, { name: 'document', hint: true }],
  },
  {
    ecosystem: 'pulumi',
    artifact: [
      { name: 'kind', hint: true },
      { name: 'name' },
      { name: 'version' },
      { name: 'os' },
      { name: 'arch' },
    ],
    index: null,
  },
]

export function PackageUploadPanel({
  onError,
  onSuccess,
  sealed,
}: {
  onError: (message: string) => void
  onSuccess: (message: string) => void
  /** Whether this node cannot reach upstream (`registry.cache_only`). Shown
   *  because a sealed node is the case this form exists for, not as a warning. */
  sealed: boolean
}) {
  const t = useTranslations('adminBinaryCache')
  const { confirmIrreversible } = useConfirm()

  const [open, setOpen] = useState(false)
  const [ecosystem, setEcosystem] = useState('pypi')
  const [kind, setKind] = useState<'artifact' | 'index'>('artifact')
  const [values, setValues] = useState<Record<string, string>>({})
  const [file, setFile] = useState<File | null>(null)
  const [reference, setReference] = useState('')
  const [replace, setReplace] = useState(false)
  const [busy, setBusy] = useState(false)

  const shape = useMemo(
    () => SHAPES.find((s) => s.ecosystem === ecosystem) ?? SHAPES[0],
    [ecosystem],
  )
  const hasIndex = shape.index !== null
  // ONE resolved kind, because the fields and the ROUTE must never disagree. An
  // earlier version fell back to the artifact fields when the ecosystem had no
  // index but still built the `/index` path from the raw state -- so a state
  // combination the UI prevents today would have posted artifact coordinates to
  // a route that does not exist. `pickEcosystem` also corrects the mode, which
  // makes that unreachable; deriving both from one value makes it impossible.
  const effectiveKind: 'artifact' | 'index' = kind === 'index' && hasIndex ? 'index' : 'artifact'
  const fields = effectiveKind === 'index' && shape.index ? shape.index : shape.artifact

  function pickEcosystem(next: string) {
    setEcosystem(next)
    // The coordinates do not survive an ecosystem change: they are that
    // ecosystem's own names, so keeping them would post a stale `project` to a
    // route expecting `module`.
    setValues({})
    const nextShape = SHAPES.find((s) => s.ecosystem === next)
    if (kind === 'index' && !nextShape?.index) setKind('artifact')
  }

  const ready =
    file !== null && fields.every((f) => (values[f.name] ?? '').trim().length > 0) && !busy

  async function submit() {
    if (!file) return
    // A replacement destroys bytes a run may already depend on, and on a sealed
    // node nothing can fetch them back -- so it is confirmed in BOTH pointer
    // modes, as an irreversible action rather than a reversible toggle. The
    // server refuses with a 409 without `replace`, so this guards the deliberate
    // case rather than standing in for the check.
    if (replace) {
      const prompt = replace && sealed
        ? `${t('upload.confirmReplace', { filename: file.name })}\n\n${t('upload.confirmReplaceSealed')}`
        : t('upload.confirmReplace', { filename: file.name })
      if (!confirmIrreversible(prompt)) return
    }

    const body = new FormData()
    for (const f of fields) body.set(f.name, (values[f.name] ?? '').trim())
    if (reference.trim()) body.set('reference', reference.trim())
    if (replace) body.set('replace', 'true')
    body.set('file', file)

    const path = effectiveKind === 'index' ? 'index' : 'artifacts'
    setBusy(true)
    try {
      // No Content-Type header: the browser sets it with the multipart boundary,
      // and setting it by hand omits the boundary and makes the body unparseable.
      const res = await apiFetch(`/api/v1/admin/package-cache/${ecosystem}/${path}`, {
        method: 'POST',
        body,
      })
      if (!res.ok) throw new Error(await parseApiError(res, t('upload.error')))
      const payload = (await res.json()) as {
        meta?: { replaced?: boolean; unchanged?: boolean }
      }
      // Three outcomes the operator must be able to tell apart: a first upload, a
      // replacement that destroyed something, and an identical re-upload that
      // destroyed nothing. Reporting all three as "uploaded" would hide the
      // middle one, which is the only one with a cost.
      if (payload.meta?.unchanged) onSuccess(t('upload.successUnchanged', { filename: file.name }))
      else if (payload.meta?.replaced) onSuccess(t('upload.successReplaced', { filename: file.name }))
      else onSuccess(t('upload.success', { filename: file.name }))
      setFile(null)
      setValues({})
      setReference('')
      setReplace(false)
    } catch (err) {
      onError(err instanceof Error ? err.message : t('upload.error'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="mt-8" data-testid="package-upload-section">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-100">{t('upload.title')}</h2>
          <p className="mt-1 text-sm text-slate-400 max-w-3xl">{t('upload.description')}</p>
        </div>
        <button
          onClick={() => setOpen((v) => !v)}
          data-testid="package-upload-toggle"
          className="min-h-11 px-3 py-2 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors"
          aria-expanded={open}
        >
          {open ? t('upload.hide') : t('upload.show')}
        </button>
      </div>

      {open ? (
        <div className="mt-4 bg-slate-800/50 rounded-lg border border-slate-700/50 p-4">
          {sealed ? (
            <p className="mb-4 text-sm text-amber-200 bg-amber-900/20 rounded-md px-3 py-2">
              {t('upload.sealedNote')}
            </p>
          ) : null}

          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block">
              <span className="block text-xs text-slate-400 mb-1">{t('upload.ecosystem')}</span>
              <select
                value={ecosystem}
                onChange={(e) => pickEcosystem(e.target.value)}
                data-testid="package-upload-ecosystem"
                className="w-full min-w-0 min-h-11 px-3 py-2 rounded-lg bg-slate-900 border border-slate-700 text-slate-200 text-sm"
              >
                {SHAPES.map((s) => (
                  <option key={s.ecosystem} value={s.ecosystem}>
                    {s.ecosystem}
                  </option>
                ))}
              </select>
            </label>

            <label className="block">
              <span className="block text-xs text-slate-400 mb-1">{t('upload.kind')}</span>
              <select
                value={kind}
                onChange={(e) => setKind(e.target.value as 'artifact' | 'index')}
                data-testid="package-upload-kind"
                disabled={!hasIndex}
                className="w-full min-w-0 min-h-11 px-3 py-2 rounded-lg bg-slate-900 border border-slate-700 text-slate-200 text-sm disabled:opacity-50"
              >
                <option value="artifact">{t('upload.kindArtifact')}</option>
                {hasIndex ? <option value="index">{t('upload.kindIndex')}</option> : null}
              </select>
              <span className="mt-1 block text-xs text-slate-500">
                {hasIndex ? t('upload.kindHintIndexed') : t('upload.kindHintNoIndex')}
              </span>
            </label>

            {fields.map((f) => (
              <label key={f.name} className="block">
                <span className="block text-xs text-slate-400 mb-1">
                  {t(`upload.fields.${f.name}`)}
                </span>
                <input
                  value={values[f.name] ?? ''}
                  onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
                  data-testid={`package-upload-field-${f.name}`}
                  className="w-full min-w-0 min-h-11 px-3 py-2 rounded-lg bg-slate-900 border border-slate-700 text-slate-200 text-base font-mono"
                />
                {f.hint ? (
                  <span className="mt-1 block text-xs text-slate-500">
                    {t(`upload.hints.${f.name}`)}
                  </span>
                ) : null}
              </label>
            ))}

            <label className="block">
              <span className="block text-xs text-slate-400 mb-1">{t('upload.file')}</span>
              <input
                type="file"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                data-testid="package-upload-file"
                className="w-full min-w-0 text-sm text-slate-300 file:min-h-11 file:mr-3 file:px-3 file:py-2 file:rounded-lg file:border-0 file:bg-slate-700 file:text-slate-200"
              />
            </label>

            <label className="block">
              <span className="block text-xs text-slate-400 mb-1">{t('upload.reference')}</span>
              <input
                value={reference}
                onChange={(e) => setReference(e.target.value)}
                className="w-full min-w-0 min-h-11 px-3 py-2 rounded-lg bg-slate-900 border border-slate-700 text-slate-200 text-base"
              />
              <span className="mt-1 block text-xs text-slate-500">{t('upload.referenceHint')}</span>
            </label>
          </div>

          <label className="mt-4 flex items-start gap-2">
            <input
              type="checkbox"
              checked={replace}
              onChange={(e) => setReplace(e.target.checked)}
              className="mt-1 size-4"
            />
            <span className="text-sm text-slate-300">
              {t('upload.replace')}
              <span className="block text-xs text-slate-500">{t('upload.replaceHint')}</span>
            </span>
          </label>

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              onClick={submit}
              disabled={!ready}
              data-testid="package-upload-submit"
              className="min-h-11 px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-600/80 text-white transition-colors disabled:opacity-50"
            >
              {busy ? t('upload.submitting') : t('upload.submit')}
            </button>
            <span className="text-xs text-slate-500">{t('upload.provenanceNote')}</span>
          </div>
        </div>
      ) : null}
    </section>
  )
}
