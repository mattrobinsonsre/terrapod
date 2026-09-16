"""Vault diagnostics: per-instance status and a reference check (#1663).

When a Vault-sourced variable does not resolve, the run's error is often the
only clue, and ``/vault/availability`` says which instances are *configured*,
not whether they work. This module answers "why won't my Vault variable
resolve?" without a run, and without ever minting a credential or returning a
value.

Two halves, with different rules:

**Instance status** is sampled by a periodic scheduler task into Redis
(:func:`sample_cycle`), following ``component_status``: one replica takes the
sample, every replica answers from it, and opening the admin page never logs in
to every Vault. The request path (:func:`read_status`) reads configuration and
Redis only. The last resolution failure per instance is recorded here too, by
``vault_source_service`` when a claim fails (:func:`record_resolution_error`) —
best-effort, so a Redis problem can never change the outcome of a claim.

**The reference check** (:func:`check_reference`) runs on request, because it is
about one reference. It parses and validates the reference, checks the instance
and the allow-list, and asks Vault whether Terrapod's token *could* read the
path with ``sys/capabilities-self``, which reads nothing. Only for kv-v2 does it
then read the secret, to report the key **names** it holds; a dynamic engine is
never read, because every read of one mints a credential.

Nothing here returns, stores or logs a secret value. Error messages come from
``vault_client`` and ``vault_source_service``, which already carry names and
coordinates only.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime

import httpx
import structlog

from terrapod.config import Settings, VaultInstanceConfig, settings
from terrapod.services import vault_client
from terrapod.services.vault_client import (
    VaultDenied,
    VaultError,
    VaultUnavailable,
    read_secret_response,
    secret_path,
)
from terrapod.services.vault_render import LEASE_ROOT, RenderError, parse_template
from terrapod.services.vault_source_service import (
    VaultReadRecord,
    VaultSourceError,
    _secret_for,
    parse_reference,
)

logger = structlog.get_logger("vault_diagnostics")

#: The shared sample. The TTL is generous against the 60s interval: a stale
#: sample is better than none, and its age is visible through ``checked-at``.
STATUS_KEY = "tp:vault:status"
STATUS_TTL = 600
#: How often the scheduler samples.
SAMPLE_INTERVAL_SECONDS = 60

#: Last resolution failure, per instance. Kept a week: "it failed on Tuesday"
#: is still worth seeing on Thursday, and a newer failure overwrites it.
LAST_ERROR_KEY = "tp:vault:last_error:{name}"
LAST_ERROR_TTL = 7 * 86400
#: Bounds on the best-effort write, so a slow Redis cannot hold a run claim.
_RECORD_TIMEOUT = 1.0
_MESSAGE_LIMIT = 500

#: Reference checks per user per minute. A check makes up to three Vault
#: requests, so an unlimited endpoint would let one browser tab turn a
#: variable form into a load generator against Vault.
CHECKS_PER_MINUTE = 20
_CHECK_RATE_KEY = "tp:vault:check_rate:{who}:{window}"

#: How TLS to an instance is verified — the four answers ``_verify_for`` gives.
TLS_INSTANCE_CA = "instance-ca"
TLS_GLOBAL_BUNDLE = "global-bundle"
TLS_DEFAULT = "default"
TLS_SKIP_VERIFY = "skip-verify"

#: Check statuses.
PASS = "pass"
FAIL = "fail"
SKIPPED = "skipped"
UNKNOWN = "unknown"

#: Machine codes for the notes a check can carry. Codes rather than prose so
#: every consumer (the web UI in 32 languages, an agent) can say them its own
#: way; ``docs/vault.md`` lists what each means.
NOTE_DYNAMIC_NOT_READ = "dynamic-not-read"
NOTE_KEYS_NEED_PLAN = "keys-need-plan-permission"
NOTE_LOCAL_EXECUTION = "local-execution"
NOTE_VAULT_DISABLED = "vault-disabled"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _redis():
    from terrapod.redis.client import get_redis_client

    return get_redis_client()


def tls_trust(inst: VaultInstanceConfig) -> str:
    """Which trust store TLS to this instance is verified against.

    Mirrors ``vault_client._verify_for``: a per-instance CA alone when one is
    configured; otherwise httpx's ``verify=True``, which honours
    ``SSL_CERT_FILE`` (the chart's global ``caBundle``) and falls back to
    certifi.
    """
    if inst.tls_skip_verify:
        return TLS_SKIP_VERIFY
    if inst.ca_file:
        return TLS_INSTANCE_CA
    if os.environ.get("SSL_CERT_FILE"):
        return TLS_GLOBAL_BUNDLE
    return TLS_DEFAULT


def _short(exc: BaseException) -> str:
    return str(exc)[:_MESSAGE_LIMIT]


# ── Instance status ──────────────────────────────────────────────────────


async def _health(inst: VaultInstanceConfig, timeout: float) -> dict:
    """``sys/health``, unauthenticated, asking for 200 whatever the state.

    The query parameters make a sealed, uninitialised or standby node answer
    200 with its body, so "sealed" is read from the body rather than inferred
    from a status code. No namespace header: health is a root-namespace
    endpoint.

    Deliberately not retried. The probe is resampled every minute, and a retry
    would only delay the answer an operator is waiting for — "unreachable" is
    the finding, not something to paper over.
    """
    url = (
        f"{inst.address.rstrip('/')}/v1/sys/health"
        "?standbyok=true&perfstandbyok=true&sealedcode=200&uninitcode=200"
    )
    out: dict = {
        "reachable": False,
        "initialized": None,
        "sealed": None,
        "standby": None,
        "version": "",
        "health-error": None,
    }
    try:
        verify = await vault_client._verify_for(inst)
        async with httpx.AsyncClient(timeout=timeout, verify=verify) as c:
            resp = await c.get(url)
    except (httpx.HTTPError, OSError, VaultError) as e:
        out["health-error"] = f"{type(e).__name__}: {_short(e)}"
        return out

    # Any HTTP answer means the address, DNS, network path and TLS all work.
    out["reachable"] = True
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        out["health-error"] = f"sys/health answered HTTP {resp.status_code} without a JSON body"
        return out
    out["initialized"] = bool(body.get("initialized")) if "initialized" in body else None
    out["sealed"] = bool(body.get("sealed")) if "sealed" in body else None
    out["standby"] = bool(body.get("standby")) if "standby" in body else None
    out["version"] = str(body.get("version") or "")
    if resp.status_code != 200:
        out["health-error"] = f"sys/health answered HTTP {resp.status_code}"
    return out


async def _lookup_self(inst: VaultInstanceConfig, token: str, timeout: float) -> httpx.Response:
    url = f"{inst.address.rstrip('/')}/v1/auth/token/lookup-self"
    verify = await vault_client._verify_for(inst)
    async with httpx.AsyncClient(timeout=timeout, verify=verify) as c:
        return await c.get(url, headers=vault_client._headers(inst, token))


def _ttl_of(resp: httpx.Response) -> int | None:
    try:
        data = resp.json().get("data") or {}
        return int(data.get("ttl"))
    except (ValueError, TypeError, AttributeError):
        return None


async def _login_probe(inst: VaultInstanceConfig, timeout: float) -> dict:
    """Whether Terrapod can obtain a token Vault accepts, and its remaining TTL.

    Uses the resolver's own login (and its cache), so a token that works here is
    the token a run would use. ``auth/token/lookup-self`` then reads the TTL; the
    default policy grants it. For kubernetes, jwt and approle the login call
    itself is the proof, so a refused lookup only loses the TTL. A static token
    has no login call, so for it the lookup *is* the proof.

    A refused lookup first drops the cached token and logs in once more: a
    cached token can have been revoked behind Terrapod's back, and reporting
    that as a policy problem would send the operator the wrong way.
    """
    out: dict = {"login-ok": None, "login-error": None, "ttl-seconds": None}
    static = _secret_for(inst.name)
    try:
        token = await vault_client._login(inst, static)
    except VaultError as e:
        out["login-ok"] = False
        out["login-error"] = _short(e)
        return out

    try:
        resp = await _lookup_self(inst, token, timeout)
        if resp.status_code == 403:
            vault_client._token_cache.pop(inst.name, None)
            token = await vault_client._login(inst, static)
            resp = await _lookup_self(inst, token, timeout)
    except VaultError as e:
        out["login-ok"] = False
        out["login-error"] = _short(e)
        return out
    except (httpx.HTTPError, OSError) as e:
        out["login-ok"] = inst.auth.method != "token" or None
        out["login-error"] = f"token lookup failed: {type(e).__name__}: {_short(e)}"
        return out

    if resp.status_code == 200:
        out["login-ok"] = True
        out["ttl-seconds"] = _ttl_of(resp)
    elif inst.auth.method == "token":
        out["login-ok"] = False
        out["login-error"] = (
            f"OpenBao/Vault rejected the static token for instance {inst.name!r} "
            f"(auth/token/lookup-self answered HTTP {resp.status_code})"
        )
    else:
        out["login-ok"] = True
        out["login-error"] = (
            f"logged in, but auth/token/lookup-self answered HTTP {resp.status_code}, "
            "so the token's TTL is unknown (the role's policy may omit the default policy)"
        )
    return out


async def probe_instance(inst: VaultInstanceConfig, *, timeout: float = 10.0) -> dict:
    """Sample one instance: health, login, TLS trust. Never raises."""
    out: dict = {
        "name": inst.name,
        "tls-trust": tls_trust(inst),
        "checked-at": _now(),
        "login-ok": None,
        "login-error": None,
        "ttl-seconds": None,
    }
    try:
        out.update(await _health(inst, timeout))
        if out.get("reachable") and out.get("sealed") is not True:
            out.update(await _login_probe(inst, timeout))
    except Exception as e:  # noqa: BLE001 — a probe must never break the sample
        logger.warning("vault status probe failed", instance=inst.name, error=type(e).__name__)
        out.setdefault("reachable", None)
        out["health-error"] = out.get("health-error") or f"{type(e).__name__}: {_short(e)}"
    return out


async def sample(cfg: Settings | None = None) -> list[dict]:
    """Probe every configured instance concurrently and store the sample."""
    cfg = cfg or settings
    vault = cfg.vault
    if not vault.enabled:
        return []
    results = await asyncio.gather(
        *(probe_instance(i, timeout=vault.timeout_seconds) for i in vault.instances)
    )
    payload = {"sampled-at": _now(), "instances": list(results)}
    try:
        await _redis().set(STATUS_KEY, json.dumps(payload), ex=STATUS_TTL)
    except Exception:  # noqa: BLE001
        logger.debug("could not cache vault status", exc_info=True)
    return list(results)


async def sample_cycle() -> None:
    """Periodic task. Registered only when Vault is enabled; never raises."""
    try:
        if not settings.vault.enabled:
            return
        await sample()
    except Exception:  # noqa: BLE001 — never break the scheduler loop
        logger.warning("vault status sample failed", exc_info=True)


async def record_resolution_error(
    instance: str, exc: BaseException, *, message: str | None = None
) -> None:
    """Remember the latest resolution failure for an instance. Best-effort.

    Called from the run-claim path, so it must never raise and never take long:
    any Redis failure is swallowed and the write is bounded by a timeout.
    ``exc`` is the underlying failure, whose class says what kind it was
    (``VaultDenied``, ``VaultUnavailable``, …); ``message`` is the resolver's
    wording of it, which names variables, paths and causes and never a value.
    """
    try:
        text = message if message is not None else str(exc)
        payload = json.dumps(
            {"class": type(exc).__name__, "message": text[:_MESSAGE_LIMIT], "at": _now()},
            separators=(",", ":"),
        )
        await asyncio.wait_for(
            _redis().set(LAST_ERROR_KEY.format(name=instance), payload, ex=LAST_ERROR_TTL),
            timeout=_RECORD_TIMEOUT,
        )
    except Exception:  # noqa: BLE001 — diagnostics must never fail a claim
        logger.debug("could not record vault resolution error", instance=instance)


async def read_status(cfg: Settings | None = None) -> dict:
    """The status of every configured instance, from Redis alone.

    Always one entry per *configured* instance, so an instance that has not been
    sampled yet — or whose sample expired — still appears, with its health
    unknown rather than missing. Addresses and auth settings come from
    configuration; only the probe results come from the sample.
    """
    cfg = cfg or settings
    vault = cfg.vault
    if not vault.enabled:
        return {"enabled": False, "sampled-at": None, "unavailable-reason": None, "instances": []}

    sampled: dict[str, dict] = {}
    sampled_at = None
    reason = None
    try:
        redis = _redis()
        raw = await redis.get(STATUS_KEY)
        if raw:
            data = json.loads(raw)
            sampled_at = data.get("sampled-at")
            sampled = {i.get("name"): i for i in data.get("instances") or []}
        else:
            reason = "not sampled yet"
        errors = {}
        for inst in vault.instances:
            err_raw = await redis.get(LAST_ERROR_KEY.format(name=inst.name))
            if err_raw:
                errors[inst.name] = json.loads(err_raw)
    except Exception:  # noqa: BLE001
        reason = "cache unreachable"
        errors = {}

    instances = []
    for inst in vault.instances:
        s = sampled.get(inst.name) or {}
        instances.append(
            {
                "name": inst.name,
                "default": inst.default,
                "address": inst.address,
                "namespace": inst.namespace,
                "auth-method": inst.auth.method,
                "auth-mount": inst.auth.mount,
                "auth-role": inst.auth.role,
                "tls-trust": tls_trust(inst),
                "reachable": s.get("reachable"),
                "initialized": s.get("initialized"),
                "sealed": s.get("sealed"),
                "standby": s.get("standby"),
                "version": s.get("version") or "",
                "health-error": s.get("health-error"),
                "login-ok": s.get("login-ok"),
                "login-error": s.get("login-error"),
                "ttl-seconds": s.get("ttl-seconds"),
                "checked-at": s.get("checked-at"),
                "last-error": errors.get(inst.name),
            }
        )
    return {
        "enabled": True,
        "sampled-at": sampled_at,
        "unavailable-reason": reason,
        "instances": instances,
    }


# ── Rate limit ───────────────────────────────────────────────────────────


async def check_rate_allowed(who: str) -> tuple[bool, int]:
    """Count one reference check for ``who``; ``(allowed, retry_after_seconds)``.

    A fixed one-minute window in Redis, multi-replica safe. Fails open when
    Redis is unreachable, like the API's own rate limiter: the Vault policy and
    the capability gate are the protection, this only stops a burst.
    """
    import hashlib
    import time

    now = int(time.time())
    window = now // 60
    ident = hashlib.sha256(who.encode("utf-8")).hexdigest()[:20]
    key = _CHECK_RATE_KEY.format(who=ident, window=window)
    try:
        pipe = _redis().pipeline(transaction=False)
        pipe.incr(key)
        pipe.expire(key, 120)
        count = (await pipe.execute())[0]
    except Exception:  # noqa: BLE001
        return True, 0
    if count > CHECKS_PER_MINUTE:
        return False, 60 - (now % 60)
    return True, 0


# ── Reference check ──────────────────────────────────────────────────────


def _check(name: str, status: str, detail: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail}


def acl_path(ref: dict) -> str:
    """The path Vault's ACL is evaluated on for this reference's read.

    kv-v2 policies name the ``data/`` segment the reference omits, which is the
    single most common policy mistake — so the check asks about the path the
    policy has to grant, not the one the operator typed.
    """
    mount = str(ref["mount"]).strip("/")
    path = str(ref["path"]).strip("/")
    if ref.get("engine", "kv2") == "kv2":
        return f"{mount}/data/{path}"
    return f"{mount}/{path}"


def required_capabilities(ref: dict) -> list[str]:
    """Any one of these grants the read the reference makes."""
    if ref.get("engine", "kv2") != "kv2" and str(ref.get("method", "GET")).upper() == "POST":
        return ["update", "create"]
    return ["read"]


def referenced_fields(ref: dict) -> list[str]:
    """The secret's field names a reference depends on, in order, deduplicated.

    ``field``; a template's tag roots (``{{ a.b }}`` needs ``a``; ``_lease.*`` is
    lease metadata, not a field); a format's ``fields``. A whole-secret format
    with no ``fields`` depends on no particular name.
    """
    names: list[str] = []
    if ref.get("field"):
        names.append(str(ref["field"]))
    spec = ref.get("file") if isinstance(ref.get("file"), dict) else {}
    if spec.get("template") is not None:
        try:
            for token in parse_template(spec["template"]):
                if not isinstance(token, str) and token.path[0] != LEASE_ROOT:
                    names.append(token.path[0])
        except RenderError:
            pass  # parse_reference already refused an invalid template
    for f in spec.get("fields") or []:
        names.append(str(f))
    return list(dict.fromkeys(names))


def _parse_caps(body: object, path: str) -> list[str] | None:
    """Capabilities from a ``sys/capabilities-self`` answer, across Vault versions."""
    if not isinstance(body, dict):
        return None
    for source in (body, body.get("data") if isinstance(body.get("data"), dict) else {}):
        for key in (path, "capabilities"):
            caps = source.get(key)
            if isinstance(caps, list):
                return [str(c) for c in caps]
    return None


async def capabilities_self(
    inst: VaultInstanceConfig, path: str, *, timeout: float = 10.0
) -> list[str]:
    """What Terrapod's token may do on ``path``. Reads nothing at the path.

    Raises :class:`VaultError` (or :class:`VaultUnavailable` for a transient
    answer) from the login or the call itself.
    """
    token = await vault_client._login(inst, _secret_for(inst.name))
    url = f"{inst.address.rstrip('/')}/v1/sys/capabilities-self"
    verify = await vault_client._verify_for(inst)
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=verify) as c:
            resp = await vault_client.arequest_with_retry(
                c,
                "POST",
                url,
                # Asking is side-effect free, so retrying a transient failure is safe.
                idempotent=True,
                headers=vault_client._headers(inst, token),
                json={"paths": [path]},
            )
    except (httpx.HTTPError, OSError) as e:
        raise vault_client._as_vault_error(e, "capabilities check", inst.name) from e
    detail = f"sys/capabilities-self on instance {inst.name!r} answered HTTP {resp.status_code}"
    if resp.status_code == 403:
        raise VaultDenied(
            f"{detail}: the policy attached to role {inst.auth.role!r} does not grant "
            "update on sys/capabilities-self (the default policy does)"
        )
    if resp.status_code != 200:
        if vault_client._is_transient_status(resp.status_code):
            raise VaultUnavailable(detail)
        raise VaultError(detail)
    try:
        caps = _parse_caps(resp.json(), path)
    except ValueError as e:
        raise vault_client._as_vault_error(e, "capabilities check", inst.name) from e
    if caps is None:
        raise VaultError(f"{detail} with no capabilities for {path!r}")
    return caps


async def _kv2_key_names(inst: VaultInstanceConfig, ref: dict, timeout: float) -> list[str]:
    """The key NAMES of a kv-v2 secret. Never a value.

    The only place a check reads a secret, and only ever with ``engine="kv2"``:
    a kv-v2 read mints nothing. The data map is reduced to its sorted keys
    before it leaves this function, so no caller can see a value.
    """
    response = await read_secret_response(
        inst,
        mount=ref["mount"],
        path=ref["path"],
        engine="kv2",
        timeout=timeout,
        static_token=_secret_for(inst.name),
    )
    return sorted(str(k) for k in response.data)


def _record_check_read(
    reads: list | None, keys: list[str], instance: str, ref: dict, outcome: str
) -> None:
    """Note one Vault read the check made, for the caller to audit (#1688).

    The same record a run's reads produce — names and coordinates, never a
    value. This module has no database session by design, so it collects and
    the router writes.
    """
    if reads is None:
        return
    reads.append(
        VaultReadRecord(
            keys=tuple(keys),
            instance=instance,
            mount=str(ref["mount"]).strip("/"),
            path=str(ref["path"]).strip("/"),
            engine=str(ref.get("engine", "kv2")),
            outcome=outcome,
        )
    )


async def check_reference(
    reference: object,
    *,
    key: str = "check",
    cfg: Settings | None = None,
    may_list_keys: bool = True,
    local_execution: bool = False,
    reads: list | None = None,
) -> dict:
    """Check a Vault reference without resolving it. Returns response attributes.

    ``reference`` is the stored JSON string or its parsed object. ``key`` is the
    variable key, which a file name defaults to. ``may_list_keys`` is False when
    the caller cannot run a plan on the workspace: listing a kv-v2 secret's key
    names is only given to someone who could have run a plan with the variable
    anyway. The result is JSON:API attributes; ``checks`` is the ordered list a
    UI renders, and the top-level fields are the same answers for automation.

    ``reads`` collects a :class:`VaultReadRecord` for each Vault read the check
    actually makes, so the caller can audit them (#1688). Only the kv-v2 key
    listing reads a secret; ``sys/capabilities-self`` reads nothing.
    """
    cfg = cfg or settings
    vault = cfg.vault
    checks: list[dict] = []
    notes: list[str] = []
    out: dict = {
        "ok": False,
        "vault-enabled": vault.enabled,
        "parses": False,
        "parse-error": None,
        "instance": None,
        "instance-known": None,
        "engine": None,
        "read-path": None,
        "path-allowed": None,
        "readable": None,
        "capabilities": None,
        "required-capabilities": None,
        "keys": None,
        "fields-present": None,
        "missing-fields": [],
        "notes": notes,
        "checks": checks,
    }
    if local_execution:
        notes.append(NOTE_LOCAL_EXECUTION)

    raw = reference if isinstance(reference, str) else json.dumps(reference)
    try:
        ref = parse_reference(raw, key=key or "check")
    except VaultSourceError as e:
        out["parse-error"] = str(e)
        checks.append(_check("parses", FAIL, str(e)))
        return out
    out["parses"] = True
    out["engine"] = ref.get("engine", "kv2")
    checks.append(_check("parses", PASS))

    if not vault.enabled:
        notes.append(NOTE_VAULT_DISABLED)
        out["instance-known"] = False
        checks.append(
            _check(
                "instance",
                FAIL,
                "the OpenBao/Vault value source is disabled (api.config.vault.enabled)",
            )
        )
        return out

    inst = vault.resolve_instance(ref.get("vault"))
    if inst is None:
        detail = (
            f"unknown vault instance {ref['vault']!r}"
            if ref.get("vault")
            else "the reference omits `vault`, several instances are configured and none "
            "is marked default"
        )
        out["instance"] = ref.get("vault")
        out["instance-known"] = False
        checks.append(_check("instance", FAIL, detail))
        return out
    out["instance"] = inst.name
    out["instance-known"] = True
    checks.append(_check("instance", PASS))

    mount_s, path_s = str(ref["mount"]).strip("/"), str(ref["path"]).strip("/")
    try:
        vault_client._reject_traversal(mount_s, path_s)
        vault_client._check_allowed(inst, secret_path(mount_s, path_s))
    except VaultError as e:
        out["path-allowed"] = False
        checks.append(_check("path-allowed", FAIL, str(e)))
        return out
    out["path-allowed"] = True
    checks.append(_check("path-allowed", PASS))

    read_path = acl_path(ref)
    wanted = required_capabilities(ref)
    out["read-path"] = read_path
    out["required-capabilities"] = wanted
    try:
        caps = await capabilities_self(inst, read_path, timeout=vault.timeout_seconds)
    except VaultUnavailable as e:
        checks.append(_check("readable", UNKNOWN, str(e)))
        return out
    except VaultError as e:
        out["readable"] = False
        checks.append(_check("readable", FAIL, str(e)))
        return out
    out["capabilities"] = caps
    readable = "deny" not in caps and ("root" in caps or any(c in caps for c in wanted))
    out["readable"] = readable
    if not readable:
        checks.append(
            _check(
                "readable",
                FAIL,
                f"the policy attached to role {inst.auth.role!r} grants "
                f"[{', '.join(caps) or 'nothing'}] on {read_path!r}; the read needs "
                f"{' or '.join(wanted)}",
            )
        )
        return out
    checks.append(_check("readable", PASS))

    fields = referenced_fields(ref)
    if out["engine"] != "kv2":
        # Never read: every read of a dynamic engine mints a credential, and a
        # check that minted one would be a resolution by another name.
        notes.append(NOTE_DYNAMIC_NOT_READ)
        checks.append(_check("fields-present", SKIPPED))
    elif not may_list_keys or local_execution:
        # Key names are given only to someone who could have had the value
        # delivered to a run anyway. On a local-execution workspace that
        # argument does not hold: a vault-sourced variable is refused there
        # outright (`variables.py`), so the caller has no path to the value and
        # gets no path to the schema either. `NOTE_LOCAL_EXECUTION` is already
        # in `notes` in that case.
        if not may_list_keys:
            notes.append(NOTE_KEYS_NEED_PLAN)
        checks.append(_check("fields-present", SKIPPED))
    else:
        try:
            keys = await _kv2_key_names(inst, ref, vault.timeout_seconds)
        except VaultUnavailable as e:
            _record_check_read(reads, [], inst.name, ref, "transient")
            checks.append(_check("fields-present", UNKNOWN, str(e)))
            return out
        except VaultError as e:
            _record_check_read(
                reads, [], inst.name, ref, "denied" if isinstance(e, VaultDenied) else "error"
            )
            checks.append(_check("fields-present", FAIL, str(e)))
            return out
        _record_check_read(reads, keys, inst.name, ref, "ok")
        out["keys"] = keys
        missing = [f for f in fields if f not in keys]
        out["missing-fields"] = missing
        out["fields-present"] = not missing
        checks.append(
            _check(
                "fields-present",
                FAIL if missing else PASS,
                f"not present: {', '.join(missing)}" if missing else "",
            )
        )

    out["ok"] = all(c["status"] in (PASS, SKIPPED) for c in checks)
    return out


__all__ = [
    "CHECKS_PER_MINUTE",
    "acl_path",
    "capabilities_self",
    "check_rate_allowed",
    "check_reference",
    "probe_instance",
    "read_status",
    "record_resolution_error",
    "referenced_fields",
    "required_capabilities",
    "sample",
    "sample_cycle",
    "tls_trust",
]
