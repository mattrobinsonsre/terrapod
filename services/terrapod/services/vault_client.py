"""Vault client for the variable value source (#1439).

Plain httpx against Vault's HTTP API, matching the `vault_transit` KEK provider
rather than adding an `hvac` dependency for the same handful of calls.

Two read shapes, because Vault's paths differ:

* **kv-v2** (default) — ``GET /v1/{mount}/data/{path}``, the static secret case.
* **dynamic** — ``/v1/{mount}/{path}``, which is how the dynamic engines work
  (``aws/creds/<role>``, ``database/creds/<role>``). Each read mints a fresh
  short-lived credential, which is the case that actually replaces a Vault Agent
  sidecar.

  Most dynamic engines are a ``vault read``, i.e. GET. Some are a ``vault
  write`` — ``pki/issue/<role>``, ``aws/sts/<role>`` — so a reference may set
  ``method: POST`` and pass ``data`` through as the request body. Supported from
  the outset because the reference shape is a stored, gated contract: adding the
  field later would mean every existing reference had to keep working without
  it anyway, so it may as well be right now.

Authentication is Kubernetes by default: Terrapod presents the API pod's own
ServiceAccount token and Vault validates it, so there is no stored credential.
Tokens are cached per instance until shortly before their lease expires.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import unquote

import httpx
import structlog

from terrapod.config import VaultInstanceConfig
from terrapod.http_retry import arequest_with_retry

logger = structlog.get_logger("vault")

#: Where the kubelet projects the pod's ServiceAccount token.
SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"

#: Renew this many seconds before a lease actually expires, so a long run does
#: not start with a token that dies mid-resolution.
_EXPIRY_MARGIN = 30.0


class VaultError(RuntimeError):
    """A Vault read or login failed.

    Raised rather than swallowed: an unresolvable credential must fail the run.
    A missing value would leave Terraform to fail somewhere confusing, or to
    fall back to another identity and act with credentials nobody chose.
    """


class VaultUnavailable(VaultError):
    """Vault could not be reached or did not answer usefully.

    Distinct from VaultError because the right response differs. A malformed
    reference will never resolve, so the run must fail. A Vault that is
    restarting will answer in thirty seconds, and erroring every queued run in
    the estate for that — leaving an operator to re-queue each by hand — turns a
    brief blip into an incident. A transient failure leaves the run queued for
    the next claim instead.
    """


def _as_vault_error(exc: Exception, what: str, inst_name: str) -> VaultUnavailable:
    """Convert a transport or decode failure into a VaultError.

    Callers upstream catch VaultError only. A bare httpx error escaped both the
    resolver and the run dispatcher, 500'd the listener, and left the run
    claimed in `planning` until the hour-long stale sweep — so a brief Vault
    outage stranded every queued run in the estate, not just the one that
    needed a secret.
    """
    return VaultUnavailable(
        f"Vault {what} on instance {inst_name!r} failed: "
        f"{type(exc).__name__} — {exc}. Vault may be unreachable, slow, or "
        "behind a proxy returning a non-JSON error."
    )


_token_cache: dict[str, tuple[str, float]] = {}


def reset_token_cache() -> None:
    """Drop cached Vault tokens (tests, and config reload)."""
    _token_cache.clear()


def _headers(inst: VaultInstanceConfig, token: str | None = None) -> dict[str, str]:
    h: dict[str, str] = {}
    if token:
        h["X-Vault-Token"] = token
    if inst.namespace:
        h["X-Vault-Namespace"] = inst.namespace
    return h


async def _read_sa_token() -> str:
    """The pod's ServiceAccount JWT, read off the projected volume."""
    try:
        return (await asyncio.to_thread(Path(SA_TOKEN_PATH).read_text)).strip()
    except OSError as e:
        raise VaultError(
            f"could not read the ServiceAccount token at {SA_TOKEN_PATH}: {e}. "
            "Kubernetes auth only works when Terrapod runs in-cluster; use the "
            "approle or token method otherwise."
        ) from e


async def _login(inst: VaultInstanceConfig, static_token: str | None) -> str:
    """Obtain a Vault token for this instance, honouring the cache."""
    cached = _token_cache.get(inst.name)
    if cached and cached[1] > time.monotonic():
        return cached[0]

    method = inst.auth.method
    if method == "token":
        if not static_token:
            raise VaultError(
                f"vault instance {inst.name!r} uses token auth but no token was supplied"
            )
        # A static token has no lease we can see; cache briefly so a burst of
        # variables in one run does not re-read it, and no longer.
        _token_cache[inst.name] = (static_token, time.monotonic() + 60)
        return static_token

    base = inst.address.rstrip("/")
    if method == "kubernetes":
        url = f"{base}/v1/auth/{inst.auth.mount.strip('/')}/login"
        payload = {"role": inst.auth.role, "jwt": await _read_sa_token()}
    elif method == "approle":
        if not static_token:
            raise VaultError(
                f"vault instance {inst.name!r} uses approle but no secret_id was supplied"
            )
        url = f"{base}/v1/auth/{inst.auth.mount.strip('/')}/login"
        payload = {"role_id": inst.auth.role, "secret_id": static_token}
    else:  # pragma: no cover - the config validator rejects anything else
        raise VaultError(f"unsupported vault auth method {method!r}")

    try:
        async with httpx.AsyncClient(timeout=15.0, verify=not inst.tls_skip_verify) as c:
            resp = await arequest_with_retry(c, "POST", url, headers=_headers(inst), json=payload)
    except (httpx.HTTPError, OSError) as e:
        raise _as_vault_error(e, "login", inst.name) from e
    if resp.status_code != 200:
        raise VaultError(
            f"Vault login failed for instance {inst.name!r} "
            f"({method} auth, mount {inst.auth.mount!r}, role {inst.auth.role!r}): "
            f"HTTP {resp.status_code}"
        )
    try:
        auth = resp.json().get("auth") or {}
    except ValueError as e:
        raise _as_vault_error(e, "login", inst.name) from e
    token = auth.get("client_token")
    if not token:
        raise VaultError(f"Vault login for {inst.name!r} returned no client_token")

    ttl = float(auth.get("lease_duration") or 0)
    if ttl > _EXPIRY_MARGIN:
        _token_cache[inst.name] = (token, time.monotonic() + ttl - _EXPIRY_MARGIN)
    return token


def _reject_traversal(mount: str, path: str) -> None:
    """Refuse anything that could re-target the request.

    httpx resolves ``..`` when it builds the URL, so a reference of
    ``apps/../../sys/mounts`` is checked as one path and sent as another — the
    allow-list would be guarding a path Vault never receives.

    Checked on the decoded form too: a percent-encoded ``%2e%2e`` survives a raw
    segment check untouched and is decoded downstream, which is the same
    mismatch by another spelling. Percent signs are refused outright rather than
    decoded-and-hoped-about, because a literal ``%`` has no place in a mount or
    path and allowing it means reasoning about double-encoding.

    Applied whether or not an allow-list is configured: a traversal reference is
    malformed regardless, and the default configuration must not be the
    permissive one.
    """
    illegal = ("?", "#", "%", "\\")
    for part in (mount, path):
        for candidate in (part, unquote(part)):
            if any(seg in (".", "..") for seg in candidate.split("/")):
                raise VaultError(
                    f"vault reference {mount}/{path!r} contains a path traversal "
                    "segment; give the literal mount and path"
                )
            if any(c in candidate for c in illegal):
                raise VaultError(
                    f"vault reference {mount}/{path!r} contains an illegal "
                    "character; give the literal mount and path"
                )


def _check_allowed(inst: VaultInstanceConfig, read_path: str) -> None:
    """Enforce the per-instance path allow-list.

    Second line behind the Vault policy, for an operator whose role is slightly
    wider than they meant. Empty means unrestricted.

    Matching is on SEGMENT boundaries: a bare string prefix let ``secret/app``
    grant ``secret/apple-root-keys``, which is the opposite of what an operator
    writing a prefix intends.
    """
    if not inst.paths:
        return
    target = read_path.strip("/").split("/")
    for prefix in inst.paths:
        stripped = prefix.strip("/")
        if not stripped:
            # "" and "/" meant "no restriction" before segment matching, and an
            # operator who wrote `paths: ["/"]` to mean that would otherwise
            # find every Vault read in the deployment refused after upgrading.
            return
        want = stripped.split("/")
        if target[: len(want)] == want:
            return
    raise VaultError(
        f"path {read_path!r} is not in the allow-list configured for vault instance {inst.name!r}"
    )


async def read_secret(
    inst: VaultInstanceConfig,
    *,
    mount: str,
    path: str,
    field: str,
    engine: str = "kv2",
    method: str = "GET",
    data: dict | None = None,
    timeout: float = 10.0,
    static_token: str | None = None,
) -> str:
    """Read one field from Vault and return it, or raise :class:`VaultError`."""
    mount_s, path_s = mount.strip("/"), path.strip("/")
    if not mount_s or not path_s:
        raise VaultError("a vault reference needs both a mount and a path")

    _reject_traversal(mount_s, path_s)
    read_path = f"{mount_s}/{path_s}"
    _check_allowed(inst, read_path)

    base = inst.address.rstrip("/")
    url = f"{base}/v1/{mount_s}/data/{path_s}" if engine == "kv2" else f"{base}/v1/{read_path}"
    token = await _login(inst, static_token)

    verb = method.upper()
    if engine == "kv2":
        verb = "GET"  # kv-v2 reads are always a GET, whatever the reference says

    try:
        async with httpx.AsyncClient(timeout=timeout, verify=not inst.tls_skip_verify) as c:
            resp = await arequest_with_retry(
                c,
                verb,
                url,
                headers=_headers(inst, token),
                **({"json": data or {}} if verb == "POST" else {}),
            )
    except (httpx.HTTPError, OSError) as e:
        raise _as_vault_error(e, f"read of {read_path!r}", inst.name) from e

    if resp.status_code == 403:
        raise VaultError(
            f"Vault denied {read_path!r} on instance {inst.name!r}. The policy "
            f"attached to role {inst.auth.role!r} does not grant read on this path."
        )
    if resp.status_code == 404:
        raise VaultError(f"Vault has no secret at {read_path!r} on instance {inst.name!r}")
    if resp.status_code != 200:
        # Deliberately NOT echoing resp.text: this message becomes the run's
        # error_message, readable by anyone with run-read, and a third party's
        # response body is not ours to forward there. The status and the path
        # are what diagnose it.
        raise VaultError(
            f"Vault read of {read_path!r} on instance {inst.name!r} failed with "
            f"HTTP {resp.status_code}"
        )

    try:
        body = resp.json().get("data") or {}
    except ValueError as e:
        raise _as_vault_error(e, f"read of {read_path!r}", inst.name) from e
    # kv-v2 nests the secret under data.data; the dynamic engines do not.
    data = body.get("data") if engine == "kv2" else body
    if not isinstance(data, dict) or field not in data:
        available = sorted(data) if isinstance(data, dict) else []
        raise VaultError(
            f"field {field!r} is not present at {read_path!r} "
            f"(available: {', '.join(available) or 'none'})"
        )
    value = data[field]
    return value if isinstance(value, str) else str(value)
