// What the Apply / Discard confirmation shows. A native confirm() is unreadable
// past a handful of lines, so it carries the count and the first few names.

export const CONFIRM_NAME_LIMIT = 5

export interface ConfirmLines {
  count: number
  names: string[]
  /** How many names were left out of `names`. */
  more: number
}

export function confirmLines(names: readonly string[]): ConfirmLines {
  return {
    count: names.length,
    names: names.slice(0, CONFIRM_NAME_LIMIT),
    more: Math.max(0, names.length - CONFIRM_NAME_LIMIT),
  }
}
