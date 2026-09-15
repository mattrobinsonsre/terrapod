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
``jwt`` (#1650) presents a projected, audience-scoped ServiceAccount token that
Vault validates against the cluster's OIDC discovery / JWKS, so a Vault outside
the cluster never has to reach back in. Either way the token file is re-read on
every login, because the kubelet rotates it. Vault tokens are cached per
instance until shortly before their lease expires.

TLS (#1650): an instance with ``ca_file`` is verified against that CA alone. One
without it passes ``verify=True``, which is what lets httpx honour
``SSL_CERT_FILE`` — the chart's global ``caBundle`` — and otherwise use certifi.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import httpx
import structlog

from terrapod.config import VAULT_SA_TOKEN_PATH, VaultInstanceConfig
from terrapod.http_retry import arequest_with_retry

logger = structlog.get_logger("vault")

#: Where the kubelet projects the pod's ServiceAccount token.
SA_TOKEN_PATH = VAULT_SA_TOKEN_PATH

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


class VaultDenied(VaultError):
    """Vault — or Terrapod's own allow-list — refused: a 403, or a login refused.

    A subclass, so every existing ``except VaultError`` still fails the run.
    It exists so the read audit (#1651) can record *denied* from the type
    rather than guess from a message.
    """


class VaultNotFound(VaultError):
    """Vault answered 404: nothing at that path."""


@dataclass(frozen=True)
class VaultLease:
    """The lease a dynamic-secret response carries.

    ``lease_id`` is kept so a later change can revoke it (#1649), and is
    excluded from ``repr`` so it never reaches a log line by accident. It is
    never offered to a file template: only the TTL, renewability and the
    computed expiry are (see :meth:`template_metadata`).
    """

    duration: int
    renewable: bool
    received_at: datetime
    lease_id: str = field(default="", repr=False)

    @property
    def expires_at(self) -> datetime:
        return self.received_at + timedelta(seconds=self.duration)

    def template_metadata(self) -> dict:
        """What a file template may read as ``_lease.*``. No lease id, ever."""
        return {
            "ttl": self.duration,
            "renewable": self.renewable,
            "expires_at": self.expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


@dataclass(frozen=True, eq=False)
class VaultResponse:
    """One Vault read: the secret's data, and its lease when it has one.

    ``data`` is the secret — kv-v2's ``data.data``, or a dynamic engine's
    ``data`` — and is excluded from ``repr`` so printing a response cannot print
    a secret. ``lease`` is ``None`` for a response with no lease (kv-v2, whose
    ``lease_duration`` is 0 and ``lease_id`` empty).
    """

    data: dict = field(repr=False)
    lease: VaultLease | None = None


#: HTTP statuses that mean "Vault cannot answer right now", as opposed to
#: "Vault has answered and the answer is no".
#:
#: A SEALED Vault replies 503 to everything, and a restarting or unsealing one
#: does the same — which is the single most common transient case there is, and
#: the one the whole VaultUnavailable/VaultTransient path was built for. Until
#: this classifier existed only TRANSPORT failures were treated as transient, so
#: `vault operator seal` still errored every queued run in the estate: the design
#: worked for a Vault that was unreachable and not for one that was merely shut.
#:
#: 501 is an uninitialised Vault, 429/473 a standby or performance-standby node
#: — all of them answer properly once the cluster settles.
#:
#: Everything else (403 denied, 404 missing, other 4xx) is a real answer and
#: stays permanent: retrying it forever would hide a misconfigured reference.
# 429/473 are standby/perf-standby; 412 is Vault's "missing required state" on a performance-replication or
# consistency-gated node — all resolve once the node catches up, so they are
# transient, not a real answer.
_TRANSIENT_STATUSES = frozenset({412, 429, 473})


def _is_transient_status(status: int) -> bool:
    return status >= 500 or status in _TRANSIENT_STATUSES


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

#: CA file path -> (mtime_ns it was loaded at, context). Keyed on mtime so a
#: rotated CA Secret — remounted by the kubelet — is picked up without a restart.
_ssl_cache: dict[str, tuple[int, ssl.SSLContext]] = {}


def reset_token_cache() -> None:
    """Drop cached Vault tokens and CA contexts (tests, and config reload)."""
    _token_cache.clear()
    _ssl_cache.clear()


def _load_ca_context(inst: VaultInstanceConfig) -> ssl.SSLContext:
    """An SSLContext trusting only the instance's CA file. Sync: run in a thread."""
    path = inst.ca_file
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError as e:
        raise VaultError(
            f"could not read the CA file {path!r} for vault instance {inst.name!r}: {e}. "
            "The chart mounts it from tls.ca_secret / tls.ca_key; check the Secret "
            "exists and holds that key."
        ) from e
    cached = _ssl_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        ctx = ssl.create_default_context(cafile=path)
    except (OSError, ssl.SSLError) as e:
        raise VaultError(
            f"the CA file {path!r} for vault instance {inst.name!r} is not a usable "
            f"PEM certificate bundle: {e}"
        ) from e
    _ssl_cache[path] = (mtime, ctx)
    return ctx


async def _verify_for(inst: VaultInstanceConfig) -> ssl.SSLContext | bool:
    """The httpx ``verify=`` for this instance.

    ``True`` rather than a context when no CA is configured, deliberately: that
    is the value httpx turns into "honour SSL_CERT_FILE, else certifi", so the
    chart's global caBundle keeps working for every instance that does not pin
    its own CA.
    """
    if inst.tls_skip_verify:
        return False
    if not inst.ca_file:
        return True
    return await asyncio.to_thread(_load_ca_context, inst)


def _headers(inst: VaultInstanceConfig, token: str | None = None) -> dict[str, str]:
    h: dict[str, str] = {}
    if token:
        h["X-Vault-Token"] = token
    if inst.namespace:
        h["X-Vault-Namespace"] = inst.namespace
    return h


async def _read_sa_token(path: str = SA_TOKEN_PATH, method: str = "kubernetes") -> str:
    """A ServiceAccount JWT, read off its projected volume.

    Called on every login rather than cached: the kubelet rotates the file (a
    `jwt` instance's token lives ten minutes), so a token read once at startup
    would be expired by the second login.
    """
    try:
        token = (await asyncio.to_thread(Path(path).read_text)).strip()
    except OSError as e:
        if method == "jwt" or path != SA_TOKEN_PATH:
            raise VaultError(
                f"could not read the projected ServiceAccount token at {path} for "
                f"{method} auth: {e}. The chart projects it at "
                "/var/run/secrets/terrapod/vault/<instance>/token when an instance "
                "uses jwt (or kubernetes with an audience); check auth.token_path "
                "and that Terrapod runs in-cluster."
            ) from e
        raise VaultError(
            f"could not read the ServiceAccount token at {path}: {e}. "
            "Kubernetes auth only works when Terrapod runs in-cluster; use the "
            "approle or token method otherwise."
        ) from e
    if not token:
        raise VaultError(f"the ServiceAccount token file at {path} is empty")
    return token


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
    if method in ("kubernetes", "jwt"):
        # Same body for both: Vault's kubernetes and jwt login endpoints each
        # take {role, jwt}. What differs is how Vault validates the token.
        url = f"{base}/v1/auth/{inst.auth.mount.strip('/')}/login"
        token_path = inst.auth.token_path or SA_TOKEN_PATH
        payload = {"role": inst.auth.role, "jwt": await _read_sa_token(token_path, method)}
    elif method == "approle":
        if not static_token:
            raise VaultError(
                f"vault instance {inst.name!r} uses approle but no secret_id was supplied"
            )
        url = f"{base}/v1/auth/{inst.auth.mount.strip('/')}/login"
        payload = {"role_id": inst.auth.role, "secret_id": static_token}
    else:  # pragma: no cover - the config validator rejects anything else
        raise VaultError(f"unsupported vault auth method {method!r}")

    verify = await _verify_for(inst)
    try:
        async with httpx.AsyncClient(timeout=15.0, verify=verify) as c:
            resp = await arequest_with_retry(c, "POST", url, headers=_headers(inst), json=payload)
    except (httpx.HTTPError, OSError) as e:
        raise _as_vault_error(e, "login", inst.name) from e
    if resp.status_code != 200:
        # The audience is named because a jwt login rejected for a mismatched
        # `aud` claim looks, from here, exactly like any other refusal.
        audience = f", audience {inst.auth.audience!r}" if inst.auth.audience else ""
        detail = (
            f"Vault login failed for instance {inst.name!r} "
            f"({method} auth, mount {inst.auth.mount!r}, role {inst.auth.role!r}{audience}): "
            f"HTTP {resp.status_code}"
        )
        if _is_transient_status(resp.status_code):
            raise VaultUnavailable(detail)
        raise VaultDenied(detail)
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
    raise VaultDenied(
        f"path {read_path!r} is not in the allow-list configured for vault instance {inst.name!r}"
    )


async def read_secret_response(
    inst: VaultInstanceConfig,
    *,
    mount: str,
    path: str,
    engine: str = "kv2",
    method: str = "GET",
    data: dict | None = None,
    timeout: float = 10.0,
    static_token: str | None = None,
) -> VaultResponse:
    """Read one secret: its whole data map and its lease, or raise :class:`VaultError`.

    kv-v2's ``data.data`` is unwrapped; a dynamic engine's ``data`` is returned
    as it is. One call is one Vault request, and a dynamic engine mints a new
    credential on every request — so a caller that needs several fields of one
    credential (a certificate and its key, an access key and its secret) must
    read once and take each field with :func:`extract_field` (#1619).

    The lease (``lease_duration``, ``renewable``, ``lease_id``) comes from the
    top level of the response, beside ``data``.
    """
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

    verify = await _verify_for(inst)
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=verify) as c:
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
        raise VaultDenied(
            f"Vault denied {read_path!r} on instance {inst.name!r}. The policy "
            f"attached to role {inst.auth.role!r} does not grant read on this path."
        )
    if resp.status_code == 404:
        raise VaultNotFound(f"Vault has no secret at {read_path!r} on instance {inst.name!r}")
    if resp.status_code != 200:
        # Deliberately NOT echoing resp.text: this message becomes the run's
        # error_message, readable by anyone with run-read, and a third party's
        # response body is not ours to forward there. The status and the path
        # are what diagnose it.
        detail = (
            f"Vault read of {read_path!r} on instance {inst.name!r} failed with "
            f"HTTP {resp.status_code}"
        )
        if _is_transient_status(resp.status_code):
            raise VaultUnavailable(detail)
        raise VaultError(detail)

    try:
        envelope = resp.json()
    except ValueError as e:
        raise _as_vault_error(e, f"read of {read_path!r}", inst.name) from e
    if not isinstance(envelope, dict):
        envelope = {}
    body = envelope.get("data") or {}
    # kv-v2 nests the secret under data.data; the dynamic engines do not.
    secret = body.get("data") if engine == "kv2" and isinstance(body, dict) else body
    return VaultResponse(
        data=secret if isinstance(secret, dict) else {},
        lease=_lease_of(envelope),
    )


def _lease_of(envelope: dict) -> VaultLease | None:
    """The lease a response carries, or None when it has none.

    kv-v2 answers ``lease_duration: 0`` and an empty ``lease_id``: no lease.
    A malformed duration is treated as no lease rather than failing a read that
    otherwise succeeded — the lease is metadata, not the secret.
    """
    lease_id = envelope.get("lease_id") or ""
    try:
        duration = int(envelope.get("lease_duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0 and not lease_id:
        return None
    return VaultLease(
        duration=max(duration, 0),
        renewable=bool(envelope.get("renewable")),
        received_at=datetime.now(UTC),
        lease_id=str(lease_id),
    )


async def read_secret_data(
    inst: VaultInstanceConfig,
    *,
    mount: str,
    path: str,
    engine: str = "kv2",
    method: str = "GET",
    data: dict | None = None,
    timeout: float = 10.0,
    static_token: str | None = None,
) -> dict:
    """:func:`read_secret_response`, keeping only the secret's data map."""
    resp = await read_secret_response(
        inst,
        mount=mount,
        path=path,
        engine=engine,
        method=method,
        data=data,
        timeout=timeout,
        static_token=static_token,
    )
    return resp.data


def secret_path(mount: str, path: str) -> str:
    """The ``mount/path`` a reference names, as error messages show it."""
    return f"{mount.strip('/')}/{path.strip('/')}"


def extract_field(secret: dict, field: str, *, where: str) -> str:
    """One field of a secret from :func:`read_secret_data`, as delivered.

    Raises :class:`VaultError` naming the fields that *are* present — names
    only, never a value. ``where`` is the ``mount/path`` for the message.
    """
    if not isinstance(secret, dict) or field not in secret:
        available = sorted(secret) if isinstance(secret, dict) else []
        raise VaultError(
            f"field {field!r} is not present at {where!r} "
            f"(available: {', '.join(available) or 'none'})"
        )
    value = secret[field]
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        # A map or list field (a service-account JSON document stored as an
        # object, say) is delivered as JSON. str() gave a Python repr —
        # single quotes, True/None — which no consumer can parse (#1619).
        return json.dumps(value, ensure_ascii=False)
    return str(value)


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
    """Read one field from Vault and return it, or raise :class:`VaultError`.

    One request per call. For several fields of one secret, use
    :func:`read_secret_data` once and :func:`extract_field` per field.
    """
    secret = await read_secret_data(
        inst,
        mount=mount,
        path=path,
        engine=engine,
        method=method,
        data=data,
        timeout=timeout,
        static_token=static_token,
    )
    return extract_field(secret, field, where=secret_path(mount, path))
