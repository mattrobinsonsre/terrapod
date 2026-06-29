"""Shared cache exceptions."""


class CacheOnlyError(RuntimeError):
    """Raised when an artifact is not in the cache and sealed (cache_only) mode
    is enabled, so Terrapod must not fall through to upstream. Carries an
    actionable message telling the operator how to pre-populate it.
    """
