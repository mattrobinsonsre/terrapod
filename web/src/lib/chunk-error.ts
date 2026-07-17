// Detect a failed JS/CSS chunk or dynamic-import load. After a new web build is
// deployed, a browser still holding the previous build's chunk hashes will 404
// on the next lazy chunk/RSC fetch (e.g. a route transition or a language
// switch), surfacing as a `ChunkLoadError`. That's a deploy-transient: a full
// reload pulls the current build and self-heals. `global-error.tsx` uses this to
// reload once instead of dead-ending on an error screen. Kept as a tiny pure
// predicate so the match set is reviewable in one place.

const CHUNK_ERROR_RE =
  /ChunkLoadError|Loading chunk [\d]+ failed|Loading CSS chunk|(dynamically )?imported module|importing a module script failed/i;

export function isChunkError(error: unknown): boolean {
  if (!error) return false;
  const e = error as { name?: unknown; message?: unknown };
  if (e.name === 'ChunkLoadError') return true;
  return typeof e.message === 'string' && CHUNK_ERROR_RE.test(e.message);
}
