"""Runner-side executable verification tests (#607).

Proves the runner refuses to run a terraform/tofu/terragrunt binary that fails
checksum or signature verification (fail-closed), and that the material source
follows the binary source (cache vs upstream URLs). Uses a synthetic in-test
GPG key + monkeypatched fetch — no network, no dependency on the image-baked
pinned keys (which live at a runtime path).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pgpy
import pytest
from pgpy.constants import HashAlgorithm, KeyFlags, PubKeyAlgorithm

from terrapod.runner.phases import binary_verify as bv
from terrapod.runner.phases.binary_verify import (
    ExecutableVerificationError,
    verify_executable,
)
from terrapod.runner.runner_config import RunnerConfig


def _cfg(level: str = "signature") -> RunnerConfig:
    return RunnerConfig.from_env(
        env={
            "TP_API_URL": "https://tp.example",
            "TP_AUTH_TOKEN": "tok",
            "TP_RUN_ID": "r1",
            "TP_BACKEND": "terraform",
            "TP_VERSION": "1.9.8",
            "TP_VERIFY_BINARIES": level,
        }
    )


def _new_key() -> pgpy.PGPKey:
    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    key.add_uid(
        pgpy.PGPUID.new("Test", email="t@example.com"),
        usage={KeyFlags.Sign},
        hashes=[HashAlgorithm.SHA256],
    )
    return key


@pytest.fixture
def artifact(tmp_path: Path) -> tuple[Path, str]:
    p = tmp_path / "terraform_1.9.8.zip"
    p.write_bytes(b"PK\x03\x04 fake zip bytes")
    return p, hashlib.sha256(p.read_bytes()).hexdigest()


def _patch(monkeypatch, manifest: str, key: pgpy.PGPKey) -> None:
    sig = bytes(key.sign(manifest))

    def fake_get(client, url, headers, retries, delay):
        return sig if (url.endswith(".sig") or url.endswith(".gpgsig")) else manifest.encode()

    monkeypatch.setattr(bv, "_get", fake_get)
    monkeypatch.setattr(bv, "load_key", lambda _p: key.pubkey)


def test_verify_ok_signature(monkeypatch, artifact):
    path, sha = artifact
    cfg = _cfg("signature")
    manifest = f"{sha}  terraform_1.9.8_{cfg.os}_{cfg.arch}.zip\n"
    _patch(monkeypatch, manifest, _new_key())
    verify_executable(cfg, "terraform", "1.9.8", path, from_cache=True, client=None)  # no raise


def test_verify_checksum_mismatch_rejected(monkeypatch, artifact):
    path, _sha = artifact
    cfg = _cfg("signature")
    manifest = f"{'0' * 64}  terraform_1.9.8_{cfg.os}_{cfg.arch}.zip\n"
    _patch(monkeypatch, manifest, _new_key())
    with pytest.raises(ExecutableVerificationError, match="checksum mismatch"):
        verify_executable(cfg, "terraform", "1.9.8", path, from_cache=True, client=None)


def test_verify_bad_signature_rejected(monkeypatch, artifact):
    path, sha = artifact
    cfg = _cfg("signature")
    manifest = f"{sha}  terraform_1.9.8_{cfg.os}_{cfg.arch}.zip\n"
    signer = _new_key()
    sig = bytes(signer.sign(manifest))

    def fake_get(client, url, headers, retries, delay):
        return sig if url.endswith((".sig", ".gpgsig")) else manifest.encode()

    monkeypatch.setattr(bv, "_get", fake_get)
    # verify against a DIFFERENT key than the one that signed
    monkeypatch.setattr(bv, "load_key", lambda _p: _new_key().pubkey)
    with pytest.raises(ExecutableVerificationError, match="signature"):
        verify_executable(cfg, "terraform", "1.9.8", path, from_cache=True, client=None)


def test_verify_off_is_noop(artifact):
    path, _sha = artifact
    verify_executable(_cfg("off"), "terraform", "1.9.8", path, from_cache=True, client=None)


def test_checksum_level_skips_signature(monkeypatch, artifact):
    path, sha = artifact
    cfg = _cfg("checksum")
    manifest = f"{sha}  terraform_1.9.8_{cfg.os}_{cfg.arch}.zip\n"
    # wrong key would fail a sig check, but checksum level never checks the sig
    _patch(monkeypatch, manifest, _new_key())
    verify_executable(cfg, "terraform", "1.9.8", path, from_cache=True, client=None)


def test_url_source_follows_binary_source():
    cfg = _cfg()
    cache_sums, cache_sig = bv._cache_sums_urls(cfg, "terraform", "1.9.8")
    assert cache_sums.startswith("https://tp.example/api/terrapod/v1/binary-cache/")
    up_sums, up_sig = bv._upstream_sums_urls("terraform", "1.9.8")
    assert up_sums.startswith("https://releases.hashicorp.com/")
    assert bv._upstream_sums_urls("tofu", "1.12.3")[1].endswith(".gpgsig")
    assert bv._upstream_sums_urls("terragrunt", "1.0.8")[0].endswith("/SHA256SUMS")


def test_key_for_tool_env_override(monkeypatch):
    # job_template injects TP_SIGNING_KEY_<TOOL>; the runner must use it over
    # the bundled key, so the same operator-controlled trust set applies.
    k = _new_key()
    monkeypatch.setenv("TP_SIGNING_KEY_TERRAFORM", str(k.pubkey))
    assert bv._key_for_tool("terraform").fingerprint.keyid == k.fingerprint.keyid


def test_artifact_names():
    assert (
        bv._artifact_name("terraform", "1.9.8", "linux", "amd64")
        == "terraform_1.9.8_linux_amd64.zip"
    )
    assert bv._artifact_name("tofu", "1.12.3", "linux", "arm64") == "tofu_1.12.3_linux_arm64.zip"
    assert bv._artifact_name("terragrunt", "1.0.8", "darwin", "arm64") == "terragrunt_darwin_arm64"
