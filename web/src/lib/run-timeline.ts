/**
 * The run timeline's key order, which IS chronological order (#2334).
 *
 * The timeline renders this array in sequence and filters out the keys a run
 * has no timestamp for, so the array's order is the only thing deciding what
 * appears above what. `configuring-at` was appended after `applied-at`, and
 * for a COMPOSITE run that is backwards:
 *
 *   `applied-at` is served as `configure_finished_at or apply_finished_at`
 *   (`runs.py`), so on a composite it carries the moment the CONFIGURE
 *   finished -- strictly later than `configuring-at`, which is when the
 *   configure started. A composite therefore showed "Applied" above
 *   "Configure started".
 *
 * So `configuring-at` sits between `applying-at` and `applied-at`: apply
 * starts, configure starts, the run finishes. A plan/apply run has no
 * `configuring-at` and is unaffected.
 *
 * `configured-at` is deliberately NOT here, and that is a decision rather than
 * an omission: on a composite it is the same instant `applied-at` already
 * carries, so rendering both would print one moment twice under two labels. It
 * stays on the API document because it is the honest per-phase timestamp and a
 * consumer that wants the phases apart needs it.
 */
export const RUN_TIMELINE_KEYS: ReadonlyArray<string> = [
  'queued-at',
  'planning-at',
  'planned-at',
  'confirmed-at',
  'applying-at',
  'configuring-at',
  'applied-at',
  'errored-at',
  'canceled-at',
  'discarded-at',
]
