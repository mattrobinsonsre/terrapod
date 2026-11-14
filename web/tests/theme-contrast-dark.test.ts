/**
 * Every chromatic hover fill clears WCAG AA on the DARK theme (#2425).
 *
 * `scripts/docs-audit/check_theme_contrast.py` is light-only, and says so. That
 * left the dark theme — the one Terrapod shipped through all of 1.x — checked by
 * nothing, and it is not the easier case: a stop that reads well on a light card
 * can be far too light under white text on a dark one.
 *
 * It bit immediately. The app's own house hover idiom, `bg-X-600
 * hover:bg-X-500`, measured **4.23:1 on dark for brand and 3.76:1 for red** —
 * below AA at 97 sites — while clearing it comfortably on light, which is why
 * the light gate was green throughout. The fix was to step the hover toward the
 * surface with an alpha (`hover:bg-X-600/80`) instead of changing the stop,
 * because `color-mix(… var(--color-X-600) 80%, transparent)` moves the same way
 * on both themes where a stop change moves in opposite directions.
 *
 * ## Why this lives here and not in the Python gate
 *
 * The dark palette is Tailwind's own, and the authoritative copy of it is
 * `node_modules/tailwindcss/theme.css`. The Python gate runs in a job with no
 * `node_modules`, so it would need a hand-copied table — a ledger of 273 stops,
 * with nothing keeping it in step with the installed version. This job has the
 * real file.
 *
 * ## Scope, deliberately narrow
 *
 * Hover fills only, which is the class that was wrong. A full dark sweep is a
 * bigger change: there are ~67 tinted `bg-X-900/40`-style badge fills that sit
 * at 1.06:1 against their card on dark, below the Python gate's own 1.10 floor,
 * and fixing those is its own piece of work rather than a rider on this one.
 * That gap is stated here rather than silently left out.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const WEB = path.join(here, '..')

const AA = 4.5
/** A matcher that stops matching must fail, not pass on nothing. */
const MIN_FILLS = 120
/** The stops Tailwind ships; a floor so a parse failure cannot read as a pass. */
const MIN_STOCK = 200

type RGB = [number, number, number]

/** oklch() -> sRGB: oklch -> oklab -> LMS -> linear sRGB -> sRGB. */
function oklchToRgb(L: number, C: number, hDeg: number): RGB {
  const h = (hDeg * Math.PI) / 180
  const a = C * Math.cos(h)
  const b = C * Math.sin(h)
  const l_ = L + 0.3963377774 * a + 0.2158037573 * b
  const m_ = L - 0.1055613458 * a - 0.0638541728 * b
  const s_ = L - 0.0894841775 * a - 1.291485548 * b
  const l = l_ ** 3
  const m = m_ ** 3
  const s = s_ ** 3
  const lin = [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ]
  return lin.map((c) => {
    const v = c <= 0.0031308 ? 12.92 * c : 1.055 * Math.pow(c, 1 / 2.4) - 0.055
    return Math.max(0, Math.min(255, Math.round(v * 255)))
  }) as RGB
}

const hexToRgb = (h: string): RGB => [
  parseInt(h.slice(1, 3), 16),
  parseInt(h.slice(3, 5), 16),
  parseInt(h.slice(5, 7), 16),
]
const rgbToHex = ([r, g, b]: RGB) =>
  '#' + [r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('')

const relLum = ([r, g, b]: RGB) => {
  const f = (c: number) => {
    const s = c / 255
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
}
const contrast = (a: RGB, b: RGB) => {
  const [x, y] = [relLum(a), relLum(b)].sort((p, q) => q - p)
  return (x + 0.05) / (y + 0.05)
}
/** What a `/NN` utility paints: fg at alpha over an opaque bg. */
const composite = (fg: RGB, alpha: number, bg: RGB): RGB =>
  fg.map((f, i) => Math.round(f * alpha + bg[i] * (1 - alpha))) as RGB

/** The DARK palette: Tailwind's stock stops, overridden by globals.css `:root`. */
function darkPalette(): { pal: Record<string, RGB>; stock: number } {
  const theme = readFileSync(
    path.join(WEB, 'node_modules', 'tailwindcss', 'theme.css'),
    'utf8',
  )
  const pal: Record<string, RGB> = {}
  let stock = 0
  for (const m of theme.matchAll(
    /--color-([a-z]+)-(\d+):\s*oklch\(([\d.]+)%\s+([\d.]+)\s+([\d.]+)\)/g,
  )) {
    pal[`${m[1]}-${m[2]}`] = oklchToRgb(+m[3] / 100, +m[4], +m[5])
    stock++
  }
  // `:root` is the DARK block -- light is the `:root:not(.dark)` override below
  // it, which is what makes dark "Tailwind's own plus the brand ramp".
  const css = readFileSync(path.join(WEB, 'src', 'app', 'globals.css'), 'utf8')
  const dark = css.slice(0, css.indexOf(':root:not(.dark)'))
  for (const m of dark.matchAll(/--color-([a-z]+)-(\d+):\s*(#[0-9a-fA-F]{6})/g)) {
    pal[`${m[1]}-${m[2]}`] = hexToRgb(m[3])
  }
  return { pal, stock }
}

function sources(): Record<string, string> {
  const out: Record<string, string> = {}
  const walk = (dir: string) => {
    for (const e of readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name)
      if (e.isDirectory()) walk(p)
      else if (e.name.endsWith('.tsx') || e.name.endsWith('.ts'))
        out[p] = readFileSync(p, 'utf8')
    }
  }
  walk(path.join(WEB, 'src'))
  return out
}

// `slate` and the other neutrals are not in scope: this clause is about a
// CHROMATIC fill under white or black text, which is what a solid button is.
const CHROMATIC = ['brand', 'red', 'amber', 'green', 'indigo', 'sky', 'violet', 'purple']

/**
 * A className string split into `(variant, utility)` pairs.
 *
 * The variant is what makes a pairing real. One element writes
 * `bg-brand-600 hover:bg-brand-600/80 disabled:bg-brand-800
 * disabled:text-brand-400 text-white`, which is THREE pairings -- white on
 * brand-600, white on the hover fill, and brand-400 on brand-800 when disabled
 * -- not nine. Ignoring the prefix pairs every fill with every ink and reports
 * most of the app, which is how a gate gets muted.
 */
function tokens(cls: string): { variant: string; util: string }[] {
  return cls
    // NOT on `:` -- that is the variant separator this function exists to
    // read, and splitting on it turned `disabled:text-brand-400` into a BASE
    // ink, which paired every primary button's fill with its disabled ink and
    // reported 25 files.
    .split(/[\s`'"{}()?,]+/)
    .filter(Boolean)
    .map((t) => {
      const k = t.lastIndexOf(':')
      return k === -1
        ? { variant: '', util: t }
        : { variant: t.slice(0, k + 1), util: t.slice(k + 1) }
    })
}

const FILL = new RegExp(`^bg-(${CHROMATIC.join('|')})-(\\d{2,3})(?:/(\\d{1,3}))?$`)
const INK = /^text-(white|black|[a-z]+-\d{2,3})$/

/**
 * `disabled:` is skipped, and that is WCAG's own rule rather than a concession:
 * text that is part of an INACTIVE user-interface component has no contrast
 * requirement (SC 1.4.3). A disabled control is deliberately low-contrast --
 * that is how it reads as disabled -- so flagging it would be asking for the
 * affordance to be removed.
 */
const EXEMPT_VARIANTS = ['disabled:']

/**
 * Every `className` expression in a file, one per element.
 *
 * One expression per element is the unit that matters: pairing a fill with an
 * ink from a DIFFERENT element is how a gate comes to report most of the app.
 * A balanced read of the `{...}` rather than a regex ending at the first `}`,
 * because a real className is a template literal full of ternaries.
 *
 * A styles constant (`warn: 'bg-amber-900/50 text-amber-300 ...'`) carries no
 * `className=`, so each quoted string holding a fill is offered as well -- that
 * is where the badge tables live, and they are elements too.
 */
function classNameExpressions(src: string): string[] {
  const out: string[] = []
  for (const m of src.matchAll(/className=/g)) {
    let i = m.index! + m[0].length
    if (src[i] === '{') {
      let depth = 1
      for (i++; i < src.length && depth > 0; i++) {
        if (src[i] === '{') depth++
        else if (src[i] === '}') depth--
      }
      // Split on quotes, so a ternary's two arms cannot pool their inks. The
      // Python gate does the same for the same reason: `? 'bg-red-600
      // text-white' : 'bg-amber-500 text-black'` is TWO elements as far as a
      // pairing is concerned, and treating it as one picked whichever ink came
      // first and reported the other arm.
      for (const seg of src.slice(m.index!, i).split(/['"`]/)) {
        if (seg.trim()) out.push(seg)
      }
      continue
    }
    const q = src[i]
    if (q === '"' || q === "'" || q === '`') {
      const end = src.indexOf(q, i + 1)
      if (end !== -1) out.push(src.slice(i + 1, end))
    }
  }
  for (const m of src.matchAll(/['"`]([^'"`\n]*\bbg-[a-z]+-\d{2,3}[^'"`\n]*)['"`]/g)) {
    out.push(m[1])
  }
  return out
}

describe('the dark theme is checked too', () => {
  const { pal, stock } = darkPalette()

  it('the oklch converter reproduces the repo own hex overrides exactly', () => {
    // The CONTROL, asserted before anything is measured with it: globals.css's
    // light block states 91 stops as HEX, and every one of them must equal a
    // converted stock stop byte for byte. A converter that is subtly wrong
    // would still produce plausible ratios, so a ratio cannot validate itself.
    const css = readFileSync(path.join(WEB, 'src', 'app', 'globals.css'), 'utf8')
    const light = css.slice(css.indexOf(':root:not(.dark)'))
    const all = new Set(Object.values(pal).map(rgbToHex))
    const misses: string[] = []
    let total = 0
    for (const m of light.matchAll(/--color-([a-z]+)-(\d+):\s*(#[0-9a-fA-F]{6})/g)) {
      total++
      if (!all.has(m[3].toLowerCase())) misses.push(`${m[1]}-${m[2]} = ${m[3]}`)
    }
    assert.ok(total >= 50, `only ${total} hex overrides found; has globals.css moved?`)
    assert.deepEqual(misses, [], `converted stock stops do not cover:\n  ${misses.join('\n  ')}`)
  })

  it('Tailwind own palette parsed, so a pass is not an empty subject', () => {
    assert.ok(stock >= MIN_STOCK, `only ${stock} stock stops parsed from theme.css`)
  })

  it('the checkbox outline clears 3:1 on dark (WCAG 1.4.11)', () => {
    // Classes are read from checkbox.tsx; each state is measured against the box's
    // own fill and the surface behind it (hovering the box also hovers its row).
    const file = path.join(WEB, 'src', 'components', 'checkbox.tsx')
    const m = readFileSync(file, 'utf8').match(/className=\{([^}]*appearance-none[^}]*)\}/)
    assert.ok(m, 'checkbox.tsx: no className with `appearance-none`')
    const toks = [...m[1].matchAll(/'([^']*)'/g)].flatMap((q) => q[1].split(/\s+/)).filter(Boolean)
    // Unprefixed or `hover:`/`checked:` only: `light:` classes are the light gate's.
    const paint = (variant: string, prop: string): [RGB, number, string] | null => {
      for (const t of toks) {
        const k = t.match(/^(?:([a-z]+:))?(bg|border)-([a-z]+-\d+)(?:\/(\d+))?$/)
        if (!k || (k[1] ?? '') !== variant || k[2] !== prop || !pal[k[3]]) continue
        return [pal[k[3]], k[4] === undefined ? 1 : +k[4] / 100, t]
      }
      return null
    }
    const page = pal['slate-900']
    const panel = composite(pal['slate-800'], 0.5, page)
    const row = composite(pal['slate-700'], 0.2, panel)
    const baseFill = paint('', 'bg')
    const hovFill = paint('hover:', 'bg')
    const baseEdge = paint('', 'border')
    const hovEdge = paint('hover:', 'border')
    const chkEdge = paint('checked:', 'border')
    assert.ok(
      baseFill && hovFill && baseEdge && hovEdge && chkEdge,
      'checkbox.tsx: a base, hover or checked border or fill class is missing',
    )
    const states: [string, RGB, [RGB, number, string], [RGB, number, string]][] = [
      ['on the page', page, baseFill, baseEdge],
      ['on the panel', panel, baseFill, baseEdge],
      ['on a hovered row', row, baseFill, baseEdge],
      ['hovered, on a hovered row', row, hovFill, hovEdge],
    ]
    const NON_TEXT = 3
    const offenders: string[] = []
    for (const [name, backdrop, fill, edge] of states) {
      const under = composite(fill[0], fill[1], backdrop)
      const painted = composite(edge[0], edge[1], under)
      for (const [what, other] of [['its own fill', under], ['the surface behind it', backdrop]] as [string, RGB][]) {
        const c = contrast(painted, other)
        if (c < NON_TEXT) offenders.push(`${c.toFixed(2)}:1  \`${edge[2]}\` ${name}, against ${what}`)
      }
    }
    for (const [name, backdrop] of [['the page', page], ['the panel', panel], ['a hovered row', row]] as [string, RGB][]) {
      const c = contrast(chkEdge[0], backdrop)
      if (c < NON_TEXT) offenders.push(`${c.toFixed(2)}:1  checked \`${chkEdge[2]}\` on ${name}`)
    }
    assert.deepEqual(
      offenders,
      [],
      `checkbox.tsx outline needs ${NON_TEXT}:1 (WCAG 1.4.11):\n  ${offenders.join('\n  ')}`,
    )
  })

  it('every chromatic fill clears AA on dark against its declared ink', () => {
    // The three surfaces the app puts a button on, matching the Python gate's
    // own ("ground", 900), ("card", 800), ("input", 700).
    // The ground and a card. NOT `slate-700`, which the Python gate uses as a
    // third parent for a BARE ink -- an ink that inherits its background can
    // genuinely sit inside an input, and a tinted banner or button cannot.
    // Including it reported three panels (`bg-red-900/30` at 3.87:1) that pass
    // over both surfaces they are ever painted on, which is the shape of false
    // positive that gets a gate muted.
    const surfaces: [string, RGB][] = [
      ['ground slate-900', pal['slate-900']],
      ['card slate-800', pal['slate-800']],
    ]
    const offenders: string[] = []
    let examined = 0
    for (const [file, text] of Object.entries(sources())) {
      for (const cls of classNameExpressions(text)) {
        const toks = tokens(cls)
        // The ink for a variant: its own, falling back to the base ink, which
        // is what the element still shows in that state when none is declared.
        const inkFor = (variant: string): string | null => {
          const own = toks.find((t) => t.variant === variant && INK.test(t.util))
          const base = toks.find((t) => t.variant === '' && INK.test(t.util))
          const m = (own ?? base)?.util.match(INK)
          return m ? m[1] : null
        }
        for (const t of toks) {
          const f = t.util.match(FILL)
          if (!f) continue
          if (EXEMPT_VARIANTS.includes(t.variant)) continue
          const base = pal[`${f[1]}-${f[2]}`]
          if (!base) continue
          const inkName = inkFor(t.variant)
          // No declared ink means the element inherits a neutral from the page
          // or a card. That is the light gate's own "bare ink" axis, not a
          // fill/ink pairing, and assuming white here would invent a pairing
          // the app never ships.
          if (!inkName) continue
          const ink: RGB =
            inkName === 'white' ? [255, 255, 255] : inkName === 'black' ? [0, 0, 0] : pal[inkName]
          if (!ink) continue
          examined++
          const alpha = f[3] === undefined ? 1 : +f[3] / 100
          for (const [sname, surf] of surfaces) {
            if (!surf) continue
            const painted = alpha === 1 ? base : composite(base, alpha, surf)
            const c = contrast(ink, painted)
            if (c < AA) {
              offenders.push(
                `${path.relative(WEB, file)}  ${t.variant}${t.util} on ${sname}: ` +
                  `${c.toFixed(2)}:1 with text-${inkName}`,
              )
            }
          }
        }
      }
    }
    assert.ok(
      examined >= MIN_FILLS,
      `only ${examined} chromatic fills with a declared ink examined; the matcher has stopped matching`,
    )
    assert.deepEqual(
      [...new Set(offenders)].sort(),
      [],
      `AA is ${AA}:1. A chromatic hover fill that fails on dark is invisible ` +
        `in the theme Terrapod shipped through 1.x:\n  ${[...new Set(offenders)].sort().join('\n  ')}`,
    )
  })
})
