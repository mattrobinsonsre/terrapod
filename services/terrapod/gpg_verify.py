"""Dependency-free OpenPGP verification primitives (#607).

Pure functions shared by the API's ``services.artifact_verification`` and the
runner's binary-verification phase. This module imports ONLY ``pgpy`` — no
``config``, no ``http_retry`` — so the lean runner image can ship it without
pulling the API's settings/HTTP stack.

The runner and the API each resolve their own pinned-key directory and do their
own fetching; the cryptographic core lives here.
"""

from __future__ import annotations

import warnings

import pgpy
from pgpy.constants import SignatureType

# pgpy prints static `UserWarning` TODO banners on EVERY ``key.verify()`` —
# "Self-sigs verification is not yet working", "Revocation checks are not yet
# implemented", "Flags (s.a. `disabled`) checks are not yet implemented". They
# are not per-verification signals (they print identically on a good or bad
# signature), so they're pure log noise in the API + runner. Suppress pgpy's
# UserWarnings here (the one module that touches pgpy). This does NOT weaken
# verification: a bad/wrong-key signature still fails closed.
#
# The self-sig and disabled-flag gaps don't apply to Terrapod's trust model
# (we anchor on the registered/pinned public key, not on self-certs). The
# *revocation* gap DID have security weight — pgpy parses revocation signatures
# but does not act on them, so a self-revoked key's signatures would still
# verify. We close that gap ourselves in ``is_revoked`` below (pure pgpy, no
# gpg binary / subprocess) and fail closed on a revoked key (#640).
warnings.filterwarnings("ignore", category=UserWarning, module=r"pgpy")


def is_revoked(key: pgpy.PGPKey) -> bool:
    """Return True if ``key`` carries a valid self-revocation signature.

    pgpy parses key-revocation signatures but does not honor them during
    ``verify`` (#640). We check them ourselves: a key is revoked if it has at
    least one ``KeyRevocation`` (0x20) signature that cryptographically
    verifies as a self-signature by the key itself — i.e. the key's owner
    revoked it. A forged/cross-key revocation packet does NOT verify as a
    self-signature and is ignored, so this never false-positives a legitimate
    key; a genuine self-revocation always trips it.

    Pure pgpy — no gpg binary, no subprocess. Synchronous/CPU-bound; callers in
    async contexts must dispatch to a thread.
    """
    try:
        for sig in key.revocation_signatures:
            if getattr(sig, "type", None) != SignatureType.KeyRevocation:
                continue
            try:
                if key.verify(key, sig):
                    return True
            except Exception:
                # A candidate revocation that won't verify as a self-signature
                # is not a real revocation — skip it, don't block the key.
                continue
    except Exception:
        # Can't enumerate revocations → don't over-reject; the caller's normal
        # signature verification still fails closed on a bad/wrong-key sig.
        return False
    return False


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse a ``SHA256SUMS`` manifest into ``{filename: sha256_hex}``.

    Each line is ``<hex>␠␠<filename>`` (GNU coreutils style); any run of
    whitespace is tolerated and a leading ``*`` (binary-mode marker) on the
    name is stripped. Blank/short lines are skipped. Digests are lowercased.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest, name = parts[0], parts[-1]
        out[name.lstrip("*")] = digest.lower()
    return out


def load_key(path: str) -> pgpy.PGPKey:
    """Load an ASCII-armored public key from ``path``."""
    key, _ = pgpy.PGPKey.from_file(path)
    return key


def load_key_from_armor(armor: str) -> pgpy.PGPKey:
    """Load an ASCII-armored public key from a string (operator-supplied key)."""
    key, _ = pgpy.PGPKey.from_blob(armor)
    return key


def verify_detached(manifest: bytes, signature: bytes, key: pgpy.PGPKey) -> bool:
    """Verify a detached OpenPGP signature over ``manifest`` using ``key``.

    Returns True only on a cryptographically valid signature by ``key``. Any
    parse/verify error is treated as "not verified" (False) — fail-closed.
    Synchronous/CPU-bound; callers in async contexts must dispatch to a thread.
    """
    try:
        if is_revoked(key):
            return False
        sig = pgpy.PGPSignature.from_blob(signature)
        return bool(key.verify(manifest, sig))
    except Exception:
        return False
