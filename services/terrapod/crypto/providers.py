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


#: The version tag on a stretched wrap. A wrap with no tag is the legacy
#: unsalted-sha256 format, which is still read and never written.
_WRAP_V2 = "v2"

#: 16 bytes, the scrypt convention.
_SALT_LEN = 16

#: scrypt parameters. n=2**15 keeps a single wrap around a tenth of a second on
#: ordinary hardware -- unnoticeable next to a startup that already talks to a
#: database, and about 32,768 times the cost per guess that the unsalted sha256
#: it replaces imposed on an attacker holding a backup. r and p are the values
#: RFC 7914 gives as the general-purpose choice.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1

#: scrypt needs 128 * n * r * p bytes -- exactly 32 MiB for the parameters above.
#: `maxmem=0` is OpenSSL's own 32 MB default, which these parameters sit just
#: over, so a wrap fails with "memory limit exceeded" rather than being slow.
#: Stated explicitly with headroom, so a later n bump fails a test rather than
#: every wrap at run time.
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def _stretch(secret: str, salt: bytes) -> bytes:
    """Derive a 32-byte KEK from a passphrase and a per-wrap salt.

    `hashlib.scrypt` rather than a `cryptography` KDF object: it is stdlib, it is
    the same primitive, and it keeps this module importable without reaching for
    a backend. The cost is deliberate -- see `_SCRYPT_N`.
    """
    return hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=32,
    )


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
        # The SECRET is kept rather than only its hash, because a v2 wrap stretches
        # it against a fresh per-wrap salt and so cannot precompute one KEK.
        self._secret = master_secret
        # And the legacy KEK, because every wrap written before v2 existed is under
        # it and a deployment that could not read those could decrypt nothing.
        self._legacy_kek = hashlib.sha256(master_secret.encode("utf-8")).digest()

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
        """Wrap a DEK in the STRETCHED, versioned format (#2589).

        Every new wrap is `v2`, with a per-wrap salt and scrypt between the
        passphrase and the KEK. The legacy format is still READ (see `unwrap`)
        but never written again, so a deployment migrates simply by rotating --
        and the format itself records which it is, rather than being inferred.
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        salt = os.urandom(_SALT_LEN)
        kek = _stretch(self._secret, salt)
        nonce = os.urandom(_NONCE_LEN)
        ct = AESGCM(kek).encrypt(nonce, dek, None)
        return f"{_WRAP_V2}.{_b64e(salt)}.{_b64e(nonce)}.{_b64e(ct)}"

    async def unwrap(self, wrapped: str) -> bytes:
        """Unwrap either format.

        **Reading the legacy format is not optional and must not be removed.**
        Every DEK wrapped before this existed is `nonce.ct` under the unsalted
        `sha256(passphrase)`, and a deployment that could not read those could not
        decrypt anything it already holds -- which is unrecoverable rather than
        inconvenient. The version tag is what tells the two apart; a wrap with no
        tag is legacy by construction, because `v2` is written with one.
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        parts = wrapped.split(".")
        if parts[0] == _WRAP_V2:
            if len(parts) != 4:
                raise ValueError("malformed v2 KEK wrap")
            _, salt_b64, nonce_b64, ct_b64 = parts
            kek = _stretch(self._secret, _b64d(salt_b64))
            return AESGCM(kek).decrypt(_b64d(nonce_b64), _b64d(ct_b64), None)

        # Legacy: unsalted sha256, no stretching. GHSA-hc47-q72v-4vcm.
        nonce_b64, ct_b64 = wrapped.split(".", 1)
        return AESGCM(self._legacy_kek).decrypt(_b64d(nonce_b64), _b64d(ct_b64), None)


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
        from aiobotocore.session import get_session

        session = get_session()
        async with session.create_client("kms", region_name=self._region) as kms:
            resp = await kms.encrypt(KeyId=self._key_id, Plaintext=dek)
            return base64.b64encode(resp["CiphertextBlob"]).decode("ascii")

    async def unwrap(self, wrapped: str) -> bytes:
        from aiobotocore.session import get_session

        session = get_session()
        async with session.create_client("kms", region_name=self._region) as kms:
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
