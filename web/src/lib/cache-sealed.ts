/**
 * The sentence a purge confirmation has to carry on a sealed node (#2474).
 *
 * `registry.cache_only` seals upstream fetching, and the scheduled retention
 * sweep SKIPS these caches when it is set — its own comment gives the reason:
 * "an un-refetchable artifact would be lost permanently". The admin routes are
 * the manual path to the same deletion, so on a sealed node a purge is not a
 * purge at all; it is a deletion with nothing able to restore it.
 *
 * The server reports it on the TOTALS as well as on the eviction responses,
 * which is what lets this reach the confirmation rather than the banner
 * afterwards — a prompt that cannot say "these bytes will not come back" is the
 * one place the fact matters most.
 *
 * One producer, because two panels saying it differently is how one of them
 * comes to say it less plainly.
 */
export function withSealedWarning(message: string, sealed: boolean, sealedText: string): string {
  return sealed ? `${message}\n\n${sealedText}` : message
}
