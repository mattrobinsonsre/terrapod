/**
 * Parsing one line of a cache-warm submission (#2486).
 *
 * Its own module rather than a helper inside `cache-warm-panel.tsx`, because
 * this is the one piece of real logic in that panel and it is where getting it
 * wrong sends the WRONG THING to the server rather than failing visibly. A
 * `.tsx` cannot be imported by `node --test`, which strips types and not JSX,
 * so a parser living in the component is a parser nothing can drive.
 */

/** One parsed line, before anything is sent. */
export interface ParsedWarmItem {
  name: string
  version: string
  /** `os/arch` pairs, meaningful only for Pulumi plugins. */
  platforms: string[]
}

/**
 * Split a line into a name, an optional version and any trailing `os/arch`
 * tokens. Returns null for a blank line, so a trailing newline in a pasted list
 * is not submitted as an empty item.
 *
 * The trailing-platform shape is the binary-cache warm's own, three sections up
 * the same page (`tool version [os/arch …]`), rather than a second syntax for
 * the one ecosystem whose artifacts are published per platform.
 *
 * **The platform test is a slash in a token AFTER the first one, never in the
 * first one.** Every Go module path has slashes in it —
 * `github.com/spf13/cobra` — so a rule applied to the whole line would read a
 * module's name as a platform and leave the item nameless. And the version is
 * the first NON-platform token rather than the second token by position, so a
 * line naming platforms and no version does not silently take `linux/amd64` as
 * its version, which the server would then refuse with a message about a
 * version nobody supplied.
 */
export function parseWarmLine(line: string): ParsedWarmItem | null {
  const tokens = line.trim().split(/\s+/).filter(Boolean)
  if (tokens.length === 0) return null
  const [name, ...rest] = tokens
  return {
    name,
    version: rest.find((t) => !t.includes('/')) ?? '',
    platforms: rest.filter((t) => t.includes('/')),
  }
}
