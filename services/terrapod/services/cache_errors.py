"""Shared cache exceptions."""


class CacheOnlyError(RuntimeError):
    """Raised when an artifact is not in the cache and sealed (cache_only) mode
    is enabled, so Terrapod must not fall through to upstream. Carries an
    actionable message telling the operator how to pre-populate it.
    """


class UpstreamArtifactMissing(ValueError):
    """The publisher serves a 404 for this exact artifact.

    A *deterministic* upstream absence, as distinct from a transport failure,
    and the two must not look alike to the caller. `_fetch_and_store_binary`
    calls `raise_for_status()`, so without this a 404 arrived at the router's
    bare `except Exception` and became a **502** — which the runner's download
    helper classifies as transient and retries, twice, before giving up with a
    gateway error that names nothing.

    That is how #2066 stayed invisible: Checkov stopped publishing
    `checkov_linux_arm64.zip` at 3.3.21, the platform table went on claiming it
    (the table is static, and availability is really a property of a
    *(version, platform)* pair), and every security scan on an arm64 agent pool
    recorded `errored` while the logs said "transient failure — will retry".

    ValueError, so the routers' existing mapping answers 400 and the runner
    stops rather than retrying something that cannot succeed. The message must
    name the tool, the version and the platform, because the operator's next
    move is to pin a different version or publish the asset.
    """
