"""Listener identity — Secret-backed cert persistence with split-brain-safe rotation.

Identity model
--------------
One listener identity per Helm release / deployment. The listener-id, cert,
key, and CA cert all live in a single K8s Secret whose name is supplied via
`TERRAPOD_CREDENTIALS_SECRET_NAME` (Helm wires this to the listener
Deployment's fullname + `-credentials`) in the listener pod's own namespace.
Multiple pods of the same deployment share that identity — they all subscribe
to the same SSE channel, all heartbeat the same Redis key, and run-claim is
coordinated via the existing `SELECT … FOR UPDATE SKIP LOCKED` so duplicate
work doesn't happen.

Startup flow
------------
1. Try to read the Secret. If found, return that identity (the renewal loop
   will refresh the cert on the next cycle).
2. If absent, call `/agent-pools/join` with `TERRAPOD_JOIN_TOKEN` and write
   the response into a freshly-created Secret. If the create races (another
   pod beat us → 409 AlreadyExists) or the API rejects the join token
   because another pod already used it (`max_uses` exhausted), retry by
   re-reading the Secret with backoff — the winning pod is in the middle
   of writing it.

Renewal coordination
--------------------
At a per-pod splay-offset within the renewal window:
1. Re-read the Secret. If its cert is "recent enough" (would not itself need
   renewal yet from this pod's perspective), adopt it without calling
   `/renew`. This is what keeps cert renewal serial across the deployment in
   steady state.
2. Otherwise call `/renew`. On success, attempt to write the Secret with the
   `resourceVersion` we read at step 1 — optimistic CAS. On 409 conflict
   (another pod wrote first), drop our newly-issued cert and re-read the
   Secret to adopt theirs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography import x509

from terrapod.logging_config import get_logger

logger = get_logger(__name__)


# Secret structure — keys inside data:
_K_TLS_CRT = "tls.crt"
_K_TLS_KEY = "tls.key"
_K_CA_CRT = "ca.crt"
_K_LISTENER_ID = "listener-id"
_K_POOL_ID = "pool-id"


@dataclass
class ListenerIdentity:
    """Active listener identity with the cert material currently in use."""

    listener_id: uuid.UUID
    name: str
    pool_id: uuid.UUID
    api_url: str
    certificate_pem: str
    private_key_pem: str
    ca_cert_pem: str
    secret_resource_version: str | None = None


# ── Public entry point ──────────────────────────────────────────────


async def establish_identity() -> ListenerIdentity:
    """Establish a listener identity for this pod. See module docstring."""
    name = os.environ.get("TERRAPOD_LISTENER_NAME", "listener")
    api_url = os.environ.get("TERRAPOD_API_URL", "http://localhost:8000")
    secret_name = _credentials_secret_name(name)
    namespace = _read_in_pod_namespace()

    secret = _read_secret(secret_name, namespace)
    if secret is not None:
        identity = _identity_from_secret(secret, name=name, api_url=api_url)
        logger.info(
            "Resumed identity from K8s Secret",
            listener_id=str(identity.listener_id),
            name=name,
            secret=f"{namespace}/{secret_name}",
        )
        return identity

    # No Secret yet — bootstrap via join token, possibly racing other pods.
    return await _bootstrap_via_join_token(name, api_url, secret_name, namespace)


# ── Secret I/O ──────────────────────────────────────────────────────


def clear_credentials_secret() -> None:
    """Delete the credentials Secret so the next establish_identity() bootstraps fresh.

    Used by `_rejoin` after persistent 401s — the cert in the Secret has been
    rejected by the API (e.g. Redis listener registration aged out and the
    fingerprint check now fails). Removing the Secret forces fall-through to
    the join-token path. Other pods sharing the deployment will see the
    Secret vanish on their next renewal cycle and join afresh themselves —
    that's the price of a shared identity going stale.
    """
    from kubernetes.client.rest import ApiException

    name = os.environ.get("TERRAPOD_LISTENER_NAME", "listener")
    secret_name = _credentials_secret_name(name)
    namespace = _read_in_pod_namespace()
    api = _core_api()
    try:
        api.delete_namespaced_secret(name=secret_name, namespace=namespace)
        logger.info("Cleared credentials Secret", secret=f"{namespace}/{secret_name}")
    except ApiException as e:
        if e.status == 404:
            return  # already gone
        logger.warning("Failed to clear credentials Secret", error=str(e))


def _credentials_secret_name(listener_name: str) -> str:
    """Resolve the credentials Secret name.

    Helm sets `TERRAPOD_CREDENTIALS_SECRET_NAME` to the Deployment fullname +
    `-credentials` so the Secret is tied to the Deployment lifecycle. Falls
    back to `{listener_name}-credentials` for environments without Helm
    (legacy deployments, local tests).
    """
    explicit = os.environ.get("TERRAPOD_CREDENTIALS_SECRET_NAME")
    if explicit:
        return explicit
    return f"{listener_name}-credentials"


def _read_in_pod_namespace() -> str:
    """Read the pod's own namespace from the projected SA token mount."""
    path = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        # Local dev fallback. Helm tests / Tilt can set TERRAPOD_LISTENER_NAMESPACE.
        return os.environ.get("TERRAPOD_LISTENER_NAMESPACE", "terrapod")


def _read_secret(name: str, namespace: str):
    """Return the V1Secret or None if it doesn't exist (404)."""
    from kubernetes.client.rest import ApiException

    api = _core_api()
    try:
        return api.read_namespaced_secret(name=name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def _create_secret(
    name: str,
    namespace: str,
    *,
    cert: str,
    key: str,
    ca: str,
    listener_id: uuid.UUID,
    pool_id: uuid.UUID,
):
    """Create the credentials Secret. Raises 409 ApiException if it already exists."""
    from kubernetes import client as k8s

    api = _core_api()
    body = k8s.V1Secret(
        metadata=k8s.V1ObjectMeta(name=name, namespace=namespace),
        type="Opaque",
        string_data={
            _K_TLS_CRT: cert,
            _K_TLS_KEY: key,
            _K_CA_CRT: ca,
            _K_LISTENER_ID: str(listener_id),
            _K_POOL_ID: str(pool_id),
        },
    )
    return api.create_namespaced_secret(namespace=namespace, body=body)


def _replace_secret(
    name: str,
    namespace: str,
    *,
    cert: str,
    key: str,
    ca: str,
    listener_id: uuid.UUID,
    pool_id: uuid.UUID,
    resource_version: str | None,
):
    """Replace the Secret with optimistic concurrency on resource_version.

    If `resource_version` is provided and stale, the K8s API returns 409
    Conflict — caller must re-read and adopt the winner's cert. If None,
    the replace is unconditional (used after a successful CAS conflict
    where we explicitly want last-writer-wins).
    """
    from kubernetes import client as k8s

    api = _core_api()
    body = k8s.V1Secret(
        metadata=k8s.V1ObjectMeta(
            name=name,
            namespace=namespace,
            resource_version=resource_version,
        ),
        type="Opaque",
        string_data={
            _K_TLS_CRT: cert,
            _K_TLS_KEY: key,
            _K_CA_CRT: ca,
            _K_LISTENER_ID: str(listener_id),
            _K_POOL_ID: str(pool_id),
        },
    )
    return api.replace_namespaced_secret(name=name, namespace=namespace, body=body)


def _core_api():
    """Get the K8s CoreV1Api, lazily initializing the client if needed."""
    from terrapod.runner.job_manager import _get_core_api

    return _get_core_api()


# ── Secret → identity ───────────────────────────────────────────────


def _decode_data_field(secret, key: str) -> str:
    """Read a field from a V1Secret's data, base64-decoded."""
    raw = (secret.data or {}).get(key, "")
    if not raw:
        raise KeyError(f"Secret missing required field: {key}")
    return base64.b64decode(raw).decode()


def _identity_from_secret(secret, *, name: str, api_url: str) -> ListenerIdentity:
    """Convert a V1Secret to a ListenerIdentity."""
    return ListenerIdentity(
        listener_id=uuid.UUID(_decode_data_field(secret, _K_LISTENER_ID)),
        name=name,
        pool_id=uuid.UUID(_decode_data_field(secret, _K_POOL_ID)),
        api_url=api_url,
        certificate_pem=_decode_data_field(secret, _K_TLS_CRT),
        private_key_pem=_decode_data_field(secret, _K_TLS_KEY),
        ca_cert_pem=_decode_data_field(secret, _K_CA_CRT),
        secret_resource_version=secret.metadata.resource_version,
    )


# ── Bootstrap (join token) ──────────────────────────────────────────


async def _bootstrap_via_join_token(
    name: str, api_url: str, secret_name: str, namespace: str
) -> ListenerIdentity:
    """Initial join: try `/agent-pools/join`, write the Secret, retry-read on race.

    Possible failure modes when N pods bootstrap simultaneously with a
    `max_uses=2` token: the first 2 pods succeed at /join, the 3rd+ get
    "max uses exceeded". Either way, the Secret should appear shortly —
    the winner writes it as part of their bootstrap. Losers keep
    re-reading the Secret with backoff until it appears.
    """
    join_token = os.environ.get("TERRAPOD_JOIN_TOKEN", "")
    if not join_token:
        raise RuntimeError(
            "TERRAPOD_JOIN_TOKEN is required for initial bootstrap (no Secret found). "
            "Create a join token via the API, store it in the listener's join-token "
            "Secret, and restart."
        )

    # Bound the time we'll wait for either a successful /join or for
    # another pod's Secret to appear.
    max_attempts = 12  # ~ 1 + 2 + 4 + 8 ... capped at 30 → ~3 min total
    backoff = 1.0

    for attempt in range(max_attempts):
        # Always re-check the Secret first — another pod may have raced ahead
        # since our last attempt.
        secret = _read_secret(secret_name, namespace)
        if secret is not None:
            identity = _identity_from_secret(secret, name=name, api_url=api_url)
            logger.info(
                "Adopted credentials Secret written by another pod",
                listener_id=str(identity.listener_id),
                name=name,
            )
            return identity

        # No Secret yet. Try /join.
        try:
            data = await _call_join(api_url, join_token, name)
        except _JoinTokenExhausted:
            # Another pod consumed the token's last use. The winner is
            # writing the Secret — keep polling.
            wait = min(backoff * (2**attempt), 30)
            logger.info(
                "Join token exhausted; another pod is winning. Re-reading Secret.",
                attempt=attempt + 1,
                wait_seconds=wait,
            )
            await asyncio.sleep(wait)
            continue
        except _JoinTransient as e:
            # API not yet available, network blip, etc. Plain retry.
            wait = min(backoff * (2**attempt), 30)
            logger.warning(
                "Transient /join failure, retrying",
                attempt=attempt + 1,
                wait_seconds=wait,
                error=str(e),
            )
            await asyncio.sleep(wait)
            continue

        # /join succeeded — write the Secret. If another pod beat us to
        # writing it (their /join also succeeded; max_uses>=2 makes this
        # possible), adopt theirs and discard our cert. The losing cert
        # remains valid until expiry but is unused.
        try:
            secret = _create_secret(
                secret_name,
                namespace,
                cert=data["certificate"],
                key=data["private_key"],
                ca=data["ca_certificate"],
                listener_id=uuid.UUID(data["listener_id"]),
                pool_id=uuid.UUID(data["pool_id"]),
            )
        except _SecretAlreadyExists:
            secret = _read_secret(secret_name, namespace)
            if secret is None:
                # Should be impossible — we just hit AlreadyExists.
                continue
            identity = _identity_from_secret(secret, name=name, api_url=api_url)
            logger.info(
                "Lost CAS race on Secret create; adopted winner's identity",
                listener_id=str(identity.listener_id),
                name=name,
            )
            return identity

        identity = ListenerIdentity(
            listener_id=uuid.UUID(data["listener_id"]),
            name=name,
            pool_id=uuid.UUID(data["pool_id"]),
            api_url=api_url,
            certificate_pem=data["certificate"],
            private_key_pem=data["private_key"],
            ca_cert_pem=data["ca_certificate"],
            secret_resource_version=secret.metadata.resource_version,
        )
        logger.info(
            "Joined pool via token, wrote credentials Secret",
            listener_id=str(identity.listener_id),
            name=name,
            pool_id=str(identity.pool_id),
        )
        return identity

    raise RuntimeError(
        f"Failed to establish listener identity after {max_attempts} attempts. "
        "Either the API is unreachable or the join token is invalid."
    )


# ── Renewal ─────────────────────────────────────────────────────────


def cert_not_after(cert_pem: str) -> datetime:
    """Return the not-after timestamp on a PEM-encoded cert."""
    return x509.load_pem_x509_certificate(cert_pem.encode()).not_valid_after_utc


def pod_splay_seconds(pod_name: str = "", max_splay: int = 30) -> int:
    """Deterministic 0..max_splay-1 splay value derived from pod name.

    The splay offsets each pod's renewal trigger so simultaneous pod
    starts don't all hit `/renew` at the same instant; combined with
    the optimistic CAS on the Secret, this means only one renewal
    actually goes through per cycle in steady state.
    """
    src = pod_name or os.environ.get("POD_NAME", "") or os.environ.get("HOSTNAME", "")
    if not src:
        return 0
    digest = hashlib.sha256(src.encode()).digest()
    return int.from_bytes(digest[:2], "big") % max_splay


async def renew_loop(
    identity_holder,
    *,
    secret_name: str,
    namespace: str,
    cert_validity_seconds: int,
    splay_seconds: int,
) -> None:
    """Keep `identity_holder.identity` fresh.

    `identity_holder` exposes a mutable `identity: ListenerIdentity` that
    other listener loops read. We update it in place (well, replace it)
    when we adopt a fresher cert from the Secret or successfully renew.

    Step 1 of each cycle: re-read the Secret. If its cert has more remaining
    life than our renewal threshold, adopt it without calling `/renew`. This
    serializes renewal across pods — first pod to renew wins; the others
    pick it up here.

    Step 2: if the Secret cert is stale (or matches ours), we are the
    renewer. Call `/renew`, then write the new cert with `resourceVersion`
    CAS. On 409, drop the cert we just got and adopt the Secret's instead.
    """
    threshold = cert_validity_seconds // 2 + splay_seconds
    skew = 30  # seconds

    while True:
        cert = identity_holder.identity
        try:
            expires = cert_not_after(cert.certificate_pem)
        except Exception as e:
            logger.error("Cannot parse current cert; sleeping 60s", error=str(e))
            await asyncio.sleep(60)
            continue

        remaining = (expires - datetime.now(UTC)).total_seconds()
        time_until_renewal = remaining - threshold

        if time_until_renewal > 0:
            await asyncio.sleep(time_until_renewal)
            continue

        # ── Step 1: maybe another pod already renewed
        secret = _read_secret(secret_name, namespace)
        if secret is not None:
            try:
                secret_cert_pem = _decode_data_field(secret, _K_TLS_CRT)
                secret_remaining = (
                    cert_not_after(secret_cert_pem) - datetime.now(UTC)
                ).total_seconds()
            except Exception:
                secret_remaining = -1
            if secret_remaining > threshold + skew:
                # Adopt the freshly-renewed cert and skip /renew.
                identity_holder.identity = _identity_from_secret(
                    secret, name=cert.name, api_url=cert.api_url
                )
                logger.debug(
                    "Adopted fresher cert from Secret; skipping /renew",
                    secret_remaining_seconds=int(secret_remaining),
                )
                continue
            current_resource_version = secret.metadata.resource_version
        else:
            current_resource_version = None

        # ── Step 2: we're the renewer
        new_cert = await _call_renew_with_retries(cert)
        if new_cert is None:
            # Transient failure (rate limit, 5xx, network). The Secret may
            # still hold a valid-ish cert; keep using it and try again on the
            # next cycle. Sleep a bounded interval — never go negative when
            # we're already past expiry, and never burn-loop on rate limits.
            sleep_for = min(max(remaining, 0), 60) + 30
            logger.warning(
                "Renewal failed after retries; will retry",
                sleep_seconds=int(sleep_for),
            )
            await asyncio.sleep(sleep_for)
            continue

        try:
            secret = _replace_secret(
                secret_name,
                namespace,
                cert=new_cert["certificate"],
                key=new_cert["private_key"],
                ca=new_cert["ca_certificate"],
                listener_id=cert.listener_id,
                pool_id=cert.pool_id,
                resource_version=current_resource_version,
            )
            identity_holder.identity = ListenerIdentity(
                listener_id=cert.listener_id,
                name=cert.name,
                pool_id=cert.pool_id,
                api_url=cert.api_url,
                certificate_pem=new_cert["certificate"],
                private_key_pem=new_cert["private_key"],
                ca_cert_pem=new_cert["ca_certificate"],
                secret_resource_version=secret.metadata.resource_version,
            )
            logger.info("Renewed listener cert; wrote Secret")
        except _SecretConflict:
            # Lost CAS race — another pod wrote first. Drop our cert,
            # adopt theirs.
            secret = _read_secret(secret_name, namespace)
            if secret is not None:
                identity_holder.identity = _identity_from_secret(
                    secret, name=cert.name, api_url=cert.api_url
                )
                logger.info("Lost renewal CAS race; adopted other pod's cert")
            else:
                # Secret vanished — extremely unusual. Sleep and retry next loop.
                logger.warning("Secret disappeared mid-renewal; will retry shortly")
                await asyncio.sleep(5)


# ── HTTP helpers ────────────────────────────────────────────────────


class _JoinTokenExhausted(Exception):
    """Raised when /join returns 401/403 because max_uses is exhausted."""


class _JoinTransient(Exception):
    """Raised on transient /join failures (5xx, network)."""


class _SecretAlreadyExists(Exception):
    """Raised on Secret create returning 409 AlreadyExists."""


class _SecretConflict(Exception):
    """Raised on Secret replace returning 409 Conflict (stale resourceVersion)."""


async def _call_join(api_url: str, join_token: str, name: str) -> dict:
    import httpx

    async with httpx.AsyncClient(base_url=api_url, timeout=30) as client:
        r = await client.post(
            "/api/v2/agent-pools/join",
            json={"join_token": join_token, "name": name},
        )
    if r.status_code in (401, 403):
        # Token revoked / expired / max_uses exhausted
        raise _JoinTokenExhausted(r.text)
    if r.status_code >= 500 or r.status_code == 0:
        raise _JoinTransient(f"{r.status_code}: {r.text}")
    if r.status_code >= 400:
        raise RuntimeError(f"/join failed: {r.status_code}: {r.text}")
    return r.json()["data"]


async def _call_renew_with_retries(identity: ListenerIdentity) -> dict | None:
    """Try `/listeners/{id}/renew` up to 3 times with backoff.

    Returns the new cert dict on success, or None if all attempts failed.
    A 401/403 from the API is *not* retried — the cert is rejected, return
    None so the caller can decide whether to fall back to join token.
    """
    import httpx

    cert_b64 = base64.b64encode(identity.certificate_pem.encode()).decode()
    headers = {"X-Terrapod-Client-Cert": cert_b64}
    backoff = 1.0
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(base_url=identity.api_url, timeout=30) as client:
                r = await client.post(
                    f"/api/v2/listeners/listener-{identity.listener_id}/renew",
                    headers=headers,
                )
            if r.status_code == 200:
                return r.json()["data"]
            if r.status_code in (401, 403):
                logger.warning(
                    "Renew rejected by API — cert may be revoked or listener gone",
                    status=r.status_code,
                    body=r.text[:200],
                )
                return None
            # 429 deserves a longer wait — rate limits are per-minute so 1s
            # backoff just hot-loops. Honor Retry-After if present.
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 30.0
                logger.warning("Renew rate-limited", attempt=attempt + 1, wait_seconds=wait)
                await asyncio.sleep(wait)
                continue
            # 5xx / other — fall through to retry
            logger.warning("Renew transient failure", status=r.status_code, attempt=attempt + 1)
        except httpx.HTTPError as e:
            logger.warning("Renew network error", error=str(e), attempt=attempt + 1)
        await asyncio.sleep(backoff)
        backoff *= 2
    return None


# Bridge K8s ApiException → our typed exceptions so callers don't import kubernetes
def _wrap_k8s_errors(fn):
    def wrapper(*args, **kwargs):
        from kubernetes.client.rest import ApiException

        try:
            return fn(*args, **kwargs)
        except ApiException as e:
            if e.status == 409:
                if "AlreadyExists" in str(e.body or ""):
                    raise _SecretAlreadyExists(str(e)) from e
                raise _SecretConflict(str(e)) from e
            raise

    return wrapper


_create_secret = _wrap_k8s_errors(_create_secret)
_replace_secret = _wrap_k8s_errors(_replace_secret)
