"""Unit tests for supply-chain artifact verification (#607).

Covers the hard invariant: an externally-fetched binary or provider archive
that fails checksum or signature verification is REJECTED (fail-closed). Uses a
throwaway in-test GPG key for sign/verify roundtrips (no network), plus a smoke
check that the three image-bundled pinned keys actually parse.
"""

from __future__ import annotations

import pgpy
import pytest
from pgpy.constants import HashAlgorithm, KeyFlags, PubKeyAlgorithm

from terrapod.services import artifact_verification as av
from terrapod.services.artifact_verification import (
    VerificationError,
    parse_sha256sums,
    verify_binary,
    verify_provider,
)

# --- helpers ---------------------------------------------------------------


def _new_key() -> pgpy.PGPKey:
    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new("Test Signer", email="test@example.com")
    key.add_uid(
        uid,
        usage={KeyFlags.Sign},
        hashes=[HashAlgorithm.SHA256],
    )
    return key


def _sign(key: pgpy.PGPKey, manifest: str) -> bytes:
    return bytes(key.sign(manifest))


SHA_A = "a" * 64
SHA_B = "b" * 64


# --- parse_sha256sums ------------------------------------------------------


def test_parse_sha256sums_basic_and_star_prefix():
    text = f"{SHA_A}  terraform_1.9.8_linux_amd64.zip\n{SHA_B} *other_file\n\n"
    out = parse_sha256sums(text)
    assert out["terraform_1.9.8_linux_amd64.zip"] == SHA_A
    assert out["other_file"] == SHA_B  # leading '*' stripped


def test_parse_sha256sums_lowercases_and_skips_junk():
    out = parse_sha256sums(f"{SHA_A.upper()}  f\nnot-a-line\n")
    assert out["f"] == SHA_A  # lowercased
    assert "not-a-line" not in out


# --- pinned keys (image-bundled) parse -------------------------------------


@pytest.mark.parametrize(
    "key_file,expected_keyid",
    [
        ("hashicorp.asc", "34365D9472D7468F"),
        ("opentofu.asc", "0C0AF313E5FD9F80"),
        ("gruntwork.asc", "577774ACA847CC49"),
    ],
)
def test_pinned_keys_load_with_expected_keyids(key_file, expected_keyid):
    av._load_key.cache_clear()
    key = av._load_key(key_file)
    assert key.fingerprint.keyid == expected_keyid


# --- gpg verify primitive --------------------------------------------------


def test_verify_gpg_sync_roundtrip_and_tamper():
    key = _new_key()
    manifest = f"{SHA_A}  artifact.zip\n".encode()
    sig = _sign(key, manifest.decode())
    assert av._verify_gpg_sync(manifest, sig, key.pubkey) is True
    # tampered manifest must NOT verify against the original signature
    assert av._verify_gpg_sync(manifest + b"x", sig, key.pubkey) is False
    # a different key must NOT verify
    assert av._verify_gpg_sync(manifest, sig, _new_key().pubkey) is False


# --- verify_binary ---------------------------------------------------------


@pytest.fixture
def patched_binary(monkeypatch):
    """Patch the network + pinned-key load for verify_binary tests."""
    key = _new_key()
    artifact = "terraform_1.9.8_linux_amd64.zip"
    manifest = f"{SHA_A}  {artifact}\n{SHA_B}  terraform_1.9.8_darwin_arm64.zip\n"
    sig = _sign(key, manifest)

    async def fake_fetch(client, url):
        if url.endswith(".sig") or url.endswith(".gpgsig"):
            return sig
        return manifest.encode()

    monkeypatch.setattr(av, "_fetch_bytes", fake_fetch)
    monkeypatch.setattr(av, "_load_key", lambda _kf: key.pubkey)
    return key


async def test_verify_binary_signature_ok(patched_binary):
    # matching sha + valid signature → no raise
    await verify_binary(None, "terraform", "1.9.8", "linux", "amd64", SHA_A, level="signature")


async def test_verify_binary_checksum_mismatch_rejected(patched_binary):
    with pytest.raises(VerificationError, match="checksum mismatch"):
        await verify_binary(None, "terraform", "1.9.8", "linux", "amd64", SHA_B, level="signature")


async def test_verify_binary_missing_entry_rejected(patched_binary):
    with pytest.raises(VerificationError, match="not "):
        await verify_binary(None, "terraform", "1.9.8", "windows", "386", SHA_A, level="signature")


async def test_verify_binary_bad_signature_rejected(patched_binary, monkeypatch):
    # swap in a different verification key so the real signature won't verify
    monkeypatch.setattr(av, "_load_key", lambda _kf: _new_key().pubkey)
    with pytest.raises(VerificationError, match="signature"):
        await verify_binary(None, "terraform", "1.9.8", "linux", "amd64", SHA_A, level="signature")


async def test_verify_binary_checksum_level_skips_signature(patched_binary, monkeypatch):
    # even with a wrong key, checksum-only mode passes because it never checks the sig
    monkeypatch.setattr(av, "_load_key", lambda _kf: _new_key().pubkey)
    await verify_binary(None, "terraform", "1.9.8", "linux", "amd64", SHA_A, level="checksum")


async def test_verify_binary_off_is_noop():
    # no patching, no network — off short-circuits
    await verify_binary(None, "terraform", "1.9.8", "linux", "amd64", "deadbeef", level="off")


# --- verify_provider -------------------------------------------------------


def _download_info(key: pgpy.PGPKey, shasum: str, manifest: str) -> dict:
    return {
        "shasum": shasum,
        "shasums_url": "https://reg/SHA256SUMS",
        "shasums_signature_url": "https://reg/SHA256SUMS.sig",
        "signing_keys": {"gpg_public_keys": [{"ascii_armor": str(key.pubkey)}]},
    }


@pytest.fixture
def patched_provider(monkeypatch):
    key = _new_key()
    manifest = f"{SHA_A}  terraform-provider-aws_5.0.0_linux_amd64.zip\n"
    sig = _sign(key, manifest)

    async def fake_fetch(client, url):
        if "signature" in url or url.endswith(".sig"):
            return sig
        return manifest.encode()

    monkeypatch.setattr(av, "_fetch_bytes", fake_fetch)
    return key, manifest


async def test_verify_provider_signature_ok(patched_provider):
    key, manifest = patched_provider
    info = _download_info(key, SHA_A, manifest)
    await verify_provider(None, info, SHA_A, level="signature")


async def test_verify_provider_checksum_mismatch_rejected(patched_provider):
    key, manifest = patched_provider
    info = _download_info(key, SHA_A, manifest)
    with pytest.raises(VerificationError, match="checksum mismatch"):
        await verify_provider(None, info, SHA_B, level="signature")


async def test_verify_provider_no_advertised_shasum_rejected():
    with pytest.raises(VerificationError, match="no shasum"):
        await verify_provider(None, {"shasum": ""}, SHA_A, level="checksum")


async def test_verify_provider_bad_signature_rejected(patched_provider):
    key, manifest = patched_provider
    # advertise a DIFFERENT key than the one that signed → signature fails
    info = _download_info(_new_key(), SHA_A, manifest)
    with pytest.raises(VerificationError, match="did not verify"):
        await verify_provider(None, info, SHA_A, level="signature")


async def test_verify_provider_advertised_shasum_absent_from_manifest(patched_provider):
    key, manifest = patched_provider
    # advertised shasum is SHA_B, but the signed manifest only lists SHA_A
    info = _download_info(key, SHA_B, manifest)
    with pytest.raises(VerificationError):
        await verify_provider(None, info, SHA_B, level="signature")


async def test_verify_provider_off_is_noop():
    await verify_provider(None, {}, "deadbeef", level="off")


# --- allow_unsigned (opt-in graceful degrade) ------------------------------


async def test_verify_provider_unsigned_rejected_by_default():
    # No signature material; default allow_unsigned=False → fail closed.
    info = {"shasum": SHA_A}  # shasum only, no shasums_url/sig/signing_keys
    with pytest.raises(VerificationError, match="lacked shasums"):
        await verify_provider(None, info, SHA_A, level="signature")


async def test_verify_provider_unsigned_allowed_degrades_to_checksum():
    # allow_unsigned=True → degrade to the shasum check (which still ran) instead
    # of rejecting. Matching shasum → no raise.
    info = {"shasum": SHA_A}
    await verify_provider(None, info, SHA_A, level="signature", allow_unsigned=True)


async def test_verify_provider_unsigned_allowed_still_checks_shasum():
    # allow_unsigned does NOT skip the checksum — a mismatch still fails closed.
    info = {"shasum": SHA_A}
    with pytest.raises(VerificationError, match="checksum mismatch"):
        await verify_provider(None, info, SHA_B, level="signature", allow_unsigned=True)


# --- operator key override (binary_cache.signing_keys) ---------------------


def test_key_for_tool_uses_config_override(monkeypatch):
    override_key = _new_key()
    monkeypatch.setattr(
        av.settings.registry.binary_cache,
        "signing_keys",
        {"terraform": str(override_key.pubkey)},
    )
    resolved = av._key_for_tool("terraform")
    assert resolved.fingerprint.keyid == override_key.fingerprint.keyid


def test_key_for_tool_falls_back_to_bundled(monkeypatch):
    monkeypatch.setattr(av.settings.registry.binary_cache, "signing_keys", {})
    # bundled HashiCorp key
    assert av._key_for_tool("terraform").fingerprint.keyid == "34365D9472D7468F"
