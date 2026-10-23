// Per-host status -> message key for a configure run's Hosts tab (#1928).
//
// A module rather than a constant inside the panel so it can be tested against
// the real catalogue AND against the server that produces these strings. The
// precedent is `variable-category.ts`: a key the UI builds at runtime is one no
// i18n gate can verify, and exactly that gap shipped a namespace typo once
// (#1334) that every gate passed and a human found at render.
//
// The server derives the status rather than the UI deriving it from the
// counters, so this map's domain is the server's output and nothing else. It is
// checked against `api/routers/runs.py` in `tests/host-status.test.ts`, because
// the failure mode is silent: a status the server adds and this does not know
// renders as a raw lower-case slug in the middle of a translated table.

export const HOST_STATUS_KEY: Record<string, string> = {
  ok: "statusOk",
  changed: "statusChanged",
  failed: "statusFailed",
  "ignored-failure": "statusIgnoredFailure",
  unreachable: "statusUnreachable",
  "not-reached": "statusNotReached",
};

/** The message key for a status, or null when the server sent one we do not know. */
export function hostStatusKey(status: string): string | null {
  return HOST_STATUS_KEY[status] ?? null;
}

/** Badge classes per status. `not-reached` is deliberately neutral, not a warning:
 *  the run did not get to the host, which is neither a pass nor a failure. */
export function hostStatusBadge(status: string): string {
  switch (status) {
    case "unreachable":
      return "bg-red-900/50 text-red-200";
    case "failed":
      return "bg-red-900/40 text-red-300";
    case "ignored-failure":
      return "bg-amber-900/40 text-amber-300";
    case "changed":
      return "bg-blue-900/40 text-blue-300";
    case "not-reached":
      return "bg-slate-700/60 text-slate-300";
    default:
      return "bg-green-900/40 text-green-300";
  }
}
