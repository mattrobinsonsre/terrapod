/**
 * The two things the audit log has to say about an actor (#2701).
 *
 * Both live here rather than in the admin page so a unit test can drive them
 * instead of reading the page's source: the badge map is a four-value switch
 * whose default matters, and the filter mapping has an invariant no render
 * assertion can see (below).
 */

/**
 * `AuditLog.actor_type`, as `services/terrapod/db/models.py` writes it.
 *
 * `terrapod_user` is deliberately absent from the badge map: it is the ordinary
 * case and badging it would badge almost every row, which is the opposite of
 * making the unusual one stand out.
 */
export const ORDINARY_ACTOR_TYPE = 'terrapod_user'

/**
 * The badge an actor type wears, as `{ messageKey, className }`.
 *
 * The shape follows the house badge used across the app -- `bg-<hue>-900/50
 * text-<hue>-300` in a `rounded-full` pill -- and each pairing here is one
 * already shipped elsewhere, so `check_theme_contrast.py` is already measuring
 * it on the light theme.
 *
 * `slack_user` takes PURPLE because purple is already Slack's colour in this
 * app (`destTypeBadge` on the workspace page), and `system` takes the neutral
 * slate that is every badge switch's `default`: the platform acting on its own
 * is background, not something to draw the eye to. `vcs_user` takes sky, which
 * no other badge meaning claims and which is not one of the hues an operator
 * reads as a verdict (amber/red/green).
 */
export const ACTOR_TYPE_BADGES: Record<string, { messageKey: string; className: string }> = {
  vcs_user: { messageKey: 'vcsUser', className: 'bg-sky-900/50 text-sky-300' },
  slack_user: { messageKey: 'slackUser', className: 'bg-purple-900/50 text-purple-300' },
  system: { messageKey: 'system', className: 'bg-slate-700 text-slate-400' },
}

/**
 * The badge for one actor type, or `null` where none is wanted.
 *
 * An actor type this does not know is NOT silently dropped: it returns the raw
 * value as its own label, under the neutral class. An unrecognised type is the
 * one case where showing the stored string is more use than showing nothing --
 * a new type added server-side would otherwise look exactly like the ordinary
 * case, which is the defect this whole badge exists to end.
 */
export function actorTypeBadge(
  actorType: string,
): { messageKey?: string; raw?: string; className: string } | null {
  if (!actorType || actorType === ORDINARY_ACTOR_TYPE) return null
  const known = ACTOR_TYPE_BADGES[actorType]
  if (known) return known
  return { raw: actorType, className: 'bg-slate-700 text-slate-400' }
}

/** What the actor box's value is matched against. */
export type ActorFilterKind = 'email' | 'login'

/**
 * The ONE query parameter an actor filter value is sent as.
 *
 * Both server filters are exact-match and they are ANDed
 * (`audit_service.query_audit_log`), so sending the same value as both would
 * ask for a row whose `actor_email` AND `actor_login` are that value -- and no
 * row has both, by design: a `vcs_user` entry carries a login and an empty
 * email. The pair would therefore match NOTHING, which reads as "that actor did
 * nothing" rather than as a bad request.
 *
 * That is why this returns a single key rather than a pair, and why the UI asks
 * which field the value means instead of guessing: a guess from the value's
 * shape would work (an email has an `@`, a VCS login cannot) and it would also
 * hide the fact that filtering by a VCS login is possible at all, which is the
 * half of #2669 that was unreachable from the UI.
 */
export function actorFilterParam(kind: ActorFilterKind): string {
  return kind === 'login' ? 'filter[actor-login]' : 'filter[actor]'
}
