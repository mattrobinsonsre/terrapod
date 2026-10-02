/**
 * Escape a string that is about to be handed to something which will treat it
 * as HTML.
 *
 * React escapes for us everywhere in this app — which is exactly why the one
 * place that does not was easy to miss. `react-force-graph`'s hover tooltip is
 * rendered by `float-tooltip`, whose `.html(content)` is a d3 `innerHTML`
 * assignment, and its default `nodeLabel` accessor is the string `'name'`. So a
 * graph node's `name` reached `innerHTML` verbatim.
 *
 * In the state graph that name comes out of the uploaded state blob, so anyone
 * with workspace write could store `<img src=x onerror=…>` as a resource name
 * and have it run for every viewer with no more than plan permission. The
 * tooltip is useful and stays; what goes into it is escaped here.
 *
 * Escapes the five characters that matter in both element content and a quoted
 * attribute value, so one function covers a label placed in either. `&` must be
 * replaced FIRST or the entities this produces get double-escaped.
 */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}
