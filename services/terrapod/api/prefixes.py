"""The API's path prefixes, in one place (#1529).

`/api/v1` is the canonical Terrapod-native surface. `/api/terrapod/v1` is a
deprecated alias, kept for the support window because our own runner and listener
images construct it directly and are expected to lag the API by minors.

**Why this module exists rather than a literal in each caller.** Serving the same
routes at two prefixes is easy; the hazard is the code that *matches* on a path
and makes a decision. Three places do, and each fails differently and silently if
it only knows one prefix:

* `rate_limit` gives login endpoints a much stricter limit — miss the second
  prefix and password guessing gets the general limit instead, which is a
  security regression that no test would notice;
* `follower_gate` decides what a follower node may serve — miss it and a request
  is refused, or worse allowed, on the wrong node;
* `audit_service` parses the path to derive the resource type (it matches all
  three prefixes in its own regex rather than calling this) — miss one and every
  entry falls through to the first path segment and is recorded against "api";
* the metrics `path_template` label — miss it and one endpoint becomes two series,
  so existing dashboards silently under-report.

So callers normalise first and then match the canonical form only, rather than
each maintaining its own pair of literals.

The SSO callback and SAML ACS URLs are NOT decided here. They are registered
with an operator's IdP, which validates what we send against its own allow-list,
so serving both prefixes does not make moving them safe — see
`routers/auth.py::_sso_url_prefix` and the `auth.legacy_callback_url` switch.
"""

from __future__ import annotations

from datetime import date

#: The canonical Terrapod-native API prefix.
NATIVE_PREFIX = "/api/v1"

#: The deprecated alias. See docs/deprecations.md.
NATIVE_LEGACY_PREFIX = "/api/terrapod/v1"

#: Sunset date advertised in the `Sunset` header on the alias, and published in
#: docs/deprecations.md. The 8-week floor of the support window; the real removal
#: is the later of that and two minor releases, so this is the earliest date the
#: alias could go, which is exactly what RFC 8594 asks for.
#:
#: Kept here rather than in the middleware so the header and the docs have one
#: source; a header promising a date the docs contradict is worse than none.
NATIVE_ALIAS_SUNSET = date(2026, 11, 3)


#: The TFE V2 compatibility surface — canonical (#1528).
#:
#: Named for what it is. `/api/v2` read as the API's main road when it is a
#: compatibility layer for another product's protocol, and it sat inside
#: Terrapod's own version namespace, which would have made `/api/v2` unusable
#: for a future Terrapod v2. Foreign protocols live outside that namespace.
TFE_PREFIX = "/api/tfe/v2"

#: The former location, still served. Advertised in service discovery until the
#: window closes, and the path every existing `terraform`/`tofu` client and every
#: runner image in the field already holds.
TFE_LEGACY_PREFIX = "/api/v2"

#: The provider network mirror — canonical (#1528).
#:
#: A pull-through CACHE we serve, not a registry, so it belongs with
#: `/api/v1/binary-cache` and `/api/v1/package-cache` rather than under the TFE
#: prefix; TFE does not serve a network mirror at all. Moving it off the bare
#: `/v1/` also ends the collision with `/api/v1`.
MIRROR_PREFIX = f"{NATIVE_PREFIX}/provider-mirror"

#: The former location, still served.
MIRROR_LEGACY_PREFIX = "/v1/providers"


def metric_path(path: str) -> str:
    """Fold a path onto ONE stable label for the Prometheus `path_template`.

    Two prefixes serve each endpoint, so without this every route reports as two
    series: a dashboard filtering one sees a fraction of the traffic and says
    nothing about it, and cardinality doubles.

    It folds onto the **legacy** name, not the canonical one, and that is
    deliberate. A dashboard or alert is a consumer holding a literal — the same
    class as an IdP allow-list or a runner's compiled-in matcher — so renaming
    the label mid-window would blank an operator's panels on an upgrade they did
    not ask for. The canonical path is what clients should call; the metric label
    is what someone's alerting already matches. They flip together at the major,
    with the rename called out in the upgrade notes.
    """
    # Longest canonical first. `MIRROR_PREFIX` (`/api/v1/provider-mirror`) sits
    # UNDER `NATIVE_PREFIX` (`/api/v1`), so checking the shorter one first would
    # fold a mirror request onto the native label and quietly file it under the
    # wrong series — which is exactly the confusion this function exists to
    # prevent.
    for canonical, legacy in sorted(
        (
            (NATIVE_PREFIX, NATIVE_LEGACY_PREFIX),
            (TFE_PREFIX, TFE_LEGACY_PREFIX),
            (MIRROR_PREFIX, MIRROR_LEGACY_PREFIX),
        ),
        key=lambda pair: len(pair[0]),
        reverse=True,
    ):
        if path == canonical:
            return legacy
        if path.startswith(canonical + "/"):
            return legacy + path[len(canonical) :]
    return path


#: Both, longest first — order matters when stripping, so that the longer prefix
#: is tried before any prefix that is a prefix of it.
NATIVE_PREFIXES: tuple[str, ...] = tuple(
    sorted((NATIVE_PREFIX, NATIVE_LEGACY_PREFIX), key=len, reverse=True)
)


def canonical_path(path: str) -> str:
    """Rewrite a request path onto the canonical native prefix.

    Returns the path unchanged when it is not on the native surface, so this is
    safe to apply to every request — `/api/v2/...`, `/oauth/...` and `/v2/...`
    pass straight through.
    """
    # The boundary check matters: without it `/api/terrapod/v1xyz` would rewrite
    # to `/api/v1xyz`. Nothing routes there today, but this function feeds two
    # security decisions and a normaliser that mangles adjacent strings is a
    # poor foundation for one.
    if path == NATIVE_LEGACY_PREFIX:
        return NATIVE_PREFIX
    if path.startswith(NATIVE_LEGACY_PREFIX + "/"):
        return NATIVE_PREFIX + path[len(NATIVE_LEGACY_PREFIX) :]
    return path


def on_native_surface(path: str) -> bool:
    """Whether a path is served by the Terrapod-native API, at either prefix."""
    return any(path.startswith(p + "/") or path == p for p in NATIVE_PREFIXES)


def prefix_of(path: str) -> str:
    """The native prefix a request arrived on — canonical if it is neither.

    The mirror image of `canonical_path`, and needed for a different job. Some
    responses embed absolute URLs that a CLIENT then matches against its OWN
    path-scoped configuration: npm resolves `_authToken` by walking the request
    path upward, and NuGet matches credentials by source-URI prefix. For those,
    a URL must come back on the prefix the caller actually used — rewrite it to
    canonical and the client stops recognising its own credential and retries
    the follow-up request unauthenticated.

    So this is not a normalisation. Normalising is right when we are making a
    decision; mirroring is right when the client is.
    """
    for candidate in NATIVE_PREFIXES:
        if path.startswith(candidate + "/") or path == candidate:
            return candidate
    return NATIVE_PREFIX


#: The prefix for URLs handed to Terrapod's OWN software, which may be older
#: than the API that generated them: HA peers, and runner Jobs.
#:
#: Two distinct consumers, one reason. A peer mid-rolling-upgrade does not serve
#: `/api/v1` yet. A runner Job image lags the API by design (the N-2 skew
#: guarantee) and cannot be taught a new prefix retroactively — a runner already
#: deployed has the matcher it shipped with. Handing either a canonical URL is a
#: 404 they cannot recover from, and in both cases the failure is quiet: silent
#: divergence between nodes, or a runner that cannot fetch its config.
#:
#: Deliberately the legacy alias, and deliberately not the canonical one. A peer
#: may be running an older release that does not serve `/api/v1` yet — replication
#: is exactly the path that must survive a rolling upgrade, and a 404 there is a
#: silent divergence between nodes rather than a loud failure.
#:
#: Flip this to NATIVE_PREFIX only once the support window has passed and every
#: supported release serves the canonical prefix. See docs/deprecations.md.
LAGGING_CONSUMER_PREFIX = NATIVE_LEGACY_PREFIX

#: Kept as the name the HA modules read; same value, same reason.
PEER_PREFIX = LAGGING_CONSUMER_PREFIX
