"""Pluggable KEK (key-encryption-key) providers — wrap/unwrap DEKs.

The KEK never appears on the data path: a provider only wraps (encrypts) and
unwraps (decrypts) the small data-encryption keys, at startup and on rotation.
Each provider returns its wrapped DEK as an opaque ``str`` (stored in
``crypto_keys.wrapped_dek``) and accepts that same string back to unwrap.

Backends (chosen for the no-/niche-CSP case first — see docs):
  - ``static``        operator-held master key (works ANYWHERE: bare-metal,
                      on-prem, air-gapped). The operator owns key durability.
  - ``vault_transit`` HashiCorp Vault Transit — CSP-agnostic encrypt/decrypt-as-
                      a-service; the key never leaves Vault. The on-prem / niche-
                      cloud / air-gapped KMS.
  - ``awskms``        AWS KMS — for deployments already on AWS (belt-and-braces
                      on top of native at-rest encryption).

Wrap/unwrap is async (network for vault/kms); ``static`` is local but async for a
uniform interface. None of this is on the per-row hot path.
"""

import base64
import hashlib
import os
from typing import Protocol, runtime_checkable

import httpx

from terrapod.config import settings
from terrapod.crypto.envelope import _b64d, _b64e
from terrapod.http_retry import arequest_with_retry
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

_NONCE_LEN = 12


@runtime_checkable
class KEKProvider(Protocol):
    """Wrap/unwrap a DEK. ``id`` identifies the backend for diagnostics."""

    id: str

    async def wrap(self, dek: bytes) -> str: ...

    async def unwrap(self, wrapped: str) -> bytes: ...


class StaticKEKProvider:
    """Local AES-256-GCM wrap with an operator-supplied master key.

    Key durability is the OPERATOR's responsibility — losing the master key makes
    all encrypted data unrecoverable. The master secret is read from the
    ``TERRAPOD_ENCRYPTION__STATIC_KEY`` env (injected from a K8s Secret), and the
    32-byte KEK is ``sha256(master_secret)``.

    **Supply generated key material, not a passphrase** — ``openssl rand -base64
    32``. A single unsalted SHA-256 is a sound way to turn 32 random bytes into a
    32-byte key, and a poor way to turn a memorable phrase into one: there is no
    salt and no iteration, so guessing the phrase offline costs one hash per
    attempt against a KEK that protects every encrypted value at rest, for as
    long as the backups live (GHSA-hc47-q72v-4vcm). This docstring used to invite
    "any sufficiently-strong passphrase", which is exactly the input the
    construction does not protect.

    The derivation is unchanged on purpose. Salting or stretching it would change
    the KEK, and every DEK already wrapped under the old one would stop
    unwrapping — silent, unrecoverable data loss on a patch upgrade. So the
    weakness is reported at construction (fatal only under
    ``require_strong_secrets``) and a versioned, stretched wrap format belongs to
    a MAJOR, where old wraps can be read and new ones written.
    """

    id = "static"

    def __init__(self, master_secret: str, *, strict: bool | None = None) -> None:
        if not master_secret:
            raise ValueError("static encryption provider requires a master key")
        self._report_strength(master_secret, strict)
        self._kek = hashlib.sha256(master_secret.encode("utf-8")).digest()

    @staticmethod
    def _report_strength(master_secret: str, strict: bool | None) -> None:
        from terrapod.secret_strength import describe_weakness

        problem = describe_weakness(master_secret, name="encryption.static_key")
        if problem is None:
            return
        if strict is None:
            strict = bool(settings.require_strong_secrets)
        if strict:
            raise ValueError(problem)
        logger.warning("weak static encryption key", problem=problem)

    async def wrap(self, dek: bytes) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(_NONCE_LEN)
        ct = AESGCM(self._kek).encrypt(nonce, dek, None)
        return f"{_b64e(nonce)}.{_b64e(ct)}"

    async def unwrap(self, wrapped: str) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce_b64, ct_b64 = wrapped.split(".", 1)
        return AESGCM(self._kek).decrypt(_b64d(nonce_b64), _b64d(ct_b64), None)


class VaultTransitKEKProvider:
    """HashiCorp Vault Transit — encrypt/decrypt-as-a-service; key stays in Vault.

    The CSP-agnostic KMS: works on-prem, multi-cloud, and air-gapped. Auth via a
    Vault token from ``TERRAPOD_ENCRYPTION__VAULT_TOKEN`` (injected from a Secret).
    """

    id = "vault_transit"

    def __init__(
        self, address: str, mount: str, key_name: str, token: str, namespace: str = ""
    ) -> None:
        if not (address and key_name and token):
            raise ValueError("vault_transit requires address, key_name, and a token")
        self._base = address.rstrip("/")
        self._mount = mount.strip("/") or "transit"
        self._key = key_name
        self._headers = {"X-Vault-Token": token}
        if namespace:
            self._headers["X-Vault-Namespace"] = namespace

    async def wrap(self, dek: bytes) -> str:
        async with httpx.AsyncClient(timeout=15.0) as c:
            resp = await arequest_with_retry(
                c,
                "POST",
                f"{self._base}/v1/{self._mount}/encrypt/{self._key}",
                headers=self._headers,
                json={"plaintext": base64.b64encode(dek).decode("ascii")},
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Vault transit encrypt failed: HTTP {resp.status_code} {resp.text[:200]}"
                )
            return resp.json()["data"]["ciphertext"]

    async def unwrap(self, wrapped: str) -> bytes:
        async with httpx.AsyncClient(timeout=15.0) as c:
            resp = await arequest_with_retry(
                c,
                "POST",
                f"{self._base}/v1/{self._mount}/decrypt/{self._key}",
                headers=self._headers,
                json={"ciphertext": wrapped},
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Vault transit decrypt failed: HTTP {resp.status_code} {resp.text[:200]}"
                )
            return base64.b64decode(resp.json()["data"]["plaintext"])


class AwsKmsKEKProvider:
    """AWS KMS wrap/unwrap. Auth via the API pod's workload identity (IRSA)."""

    id = "awskms"

    def __init__(self, key_id: str, region: str = "") -> None:
        if not key_id:
            raise ValueError("awskms requires a key_id (key ARN or id)")
        self._key_id = key_id
        self._region = region or None

    async def wrap(self, dek: bytes) -> str:
        import aioboto3

        session = aioboto3.Session()
        async with session.client("kms", region_name=self._region) as kms:
            resp = await kms.encrypt(KeyId=self._key_id, Plaintext=dek)
            return base64.b64encode(resp["CiphertextBlob"]).decode("ascii")

    async def unwrap(self, wrapped: str) -> bytes:
        import aioboto3

        session = aioboto3.Session()
        async with session.client("kms", region_name=self._region) as kms:
            resp = await kms.decrypt(KeyId=self._key_id, CiphertextBlob=base64.b64decode(wrapped))
            return resp["Plaintext"]


def build_provider() -> KEKProvider:
    """Construct the configured KEK provider from settings + secret env vars."""
    cfg = settings.encryption
    backend = cfg.provider
    if backend == "static":
        return StaticKEKProvider(os.environ.get("TERRAPOD_ENCRYPTION__STATIC_KEY", ""))
    if backend == "vault_transit":
        return VaultTransitKEKProvider(
            address=cfg.vault_address,
            mount=cfg.vault_mount,
            key_name=cfg.vault_key_name,
            token=os.environ.get("TERRAPOD_ENCRYPTION__VAULT_TOKEN", ""),
            namespace=cfg.vault_namespace,
        )
    if backend == "awskms":
        return AwsKmsKEKProvider(key_id=cfg.aws_kms_key_id, region=cfg.aws_kms_region)
    raise ValueError(f"unsupported encryption provider: {backend!r}")
