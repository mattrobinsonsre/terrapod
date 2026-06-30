"""Envelope format + the local AES-256-GCM data path.

A stored ciphertext is a self-describing, URL-safe string:

    tpenc:1:<dek_version>:<nonce_b64>:<ciphertext_b64>

- ``tpenc`` marks an application-encrypted value (anything without this prefix is
  treated as legacy plaintext and returned as-is on read — so enabling/disabling
  is a well-defined migration, not a hard cutover).
- ``1`` is the envelope format version.
- ``dek_version`` selects which data-encryption key (DEK) decrypts it — so DEK
  rotation and mixed-state during a migration are well-defined.

The DEK never appears in the envelope; it is held wrapped by the KEK provider and
cached (unwrapped) in memory after a one-time startup unwrap. These functions are
pure, synchronous, and operate on small secrets — safe on the async hot path
(no network, no large buffers).
"""

import base64

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MARKER = "tpenc"
FORMAT_VERSION = "1"
_NONCE_LEN = 12  # AES-GCM standard nonce length
DEK_LEN = 32  # AES-256


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def is_encrypted(value: str) -> bool:
    """True if ``value`` is an application-encrypted envelope (vs legacy plaintext)."""
    return value.startswith(MARKER + ":")


def encrypt(plaintext: str, dek: bytes, dek_version: int, *, nonce: bytes | None = None) -> str:
    """Encrypt ``plaintext`` with ``dek`` (AES-256-GCM) into an envelope string."""
    if len(dek) != DEK_LEN:
        raise ValueError(f"DEK must be {DEK_LEN} bytes, got {len(dek)}")
    import os

    n = nonce if nonce is not None else os.urandom(_NONCE_LEN)
    ct = AESGCM(dek).encrypt(n, plaintext.encode("utf-8"), None)
    return f"{MARKER}:{FORMAT_VERSION}:{dek_version}:{_b64e(n)}:{_b64e(ct)}"


def parse_version(envelope: str) -> int:
    """Return the DEK version an envelope was encrypted under."""
    parts = envelope.split(":", 4)
    if len(parts) != 5 or parts[0] != MARKER:
        raise ValueError("not a tpenc envelope")
    return int(parts[2])


def decrypt(envelope: str, dek: bytes) -> str:
    """Decrypt a ``tpenc`` envelope with the matching ``dek``."""
    parts = envelope.split(":", 4)
    if len(parts) != 5 or parts[0] != MARKER:
        raise ValueError("not a tpenc envelope")
    _, fmt, _ver, nonce_b64, ct_b64 = parts
    if fmt != FORMAT_VERSION:
        raise ValueError(f"unsupported envelope format version {fmt!r}")
    pt = AESGCM(dek).decrypt(_b64d(nonce_b64), _b64d(ct_b64), None)
    return pt.decode("utf-8")


def new_dek() -> bytes:
    """Generate a fresh 32-byte data-encryption key."""
    import os

    return os.urandom(DEK_LEN)
