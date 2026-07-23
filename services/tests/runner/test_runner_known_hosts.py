"""Verify the SaaS git host keys baked into the runner image (#1028).

The runner image ships github.com + gitlab.com SSH host keys in
``/etc/ssh/ssh_known_hosts`` (from the version-controlled ``saas_known_hosts``
file, COPYed by ``docker/Dockerfile.runner``) so ``git::ssh://`` module fetches
to those SaaS hosts verify out of the box — the operator need not supply
``known_hosts`` for them. The keys are baked as **static literals**, never
``ssh-keyscan``ned (which is trust-on-first-use).

This test re-derives each baked key's SHA256 fingerprint and asserts it matches
the provider's **published** value, so a corrupted / mistyped / stale baked key
fails CI loudly instead of silently pinning the wrong host key.

Authoritative sources for the expected fingerprints:
  * github.com — https://api.github.com/meta (``ssh_keys``)
  * gitlab.com — GitLab published SSH host-key fingerprints (docs.gitlab.com)
"""

from __future__ import annotations

import base64
import hashlib
from importlib.resources import files

# (host, keytype) -> published SHA256 fingerprint (no "SHA256:" prefix, no "="
# base64 padding — the form `ssh-keygen -l` emits and `_fingerprint` produces).
_EXPECTED = {
    ("github.com", "ssh-ed25519"): "+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU",
    ("github.com", "ecdsa-sha2-nistp256"): "p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM",
    ("github.com", "ssh-rsa"): "uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s",
    ("gitlab.com", "ssh-ed25519"): "eUXGGm1YGsMAS7vkcx6JOJdOGHPem5gQp4taiCfCLB8",
    ("gitlab.com", "ssh-rsa"): "ROQFvPThGrW4RuWLoL9tq9I9zJ42fK4XywyRtbOz/EQ",
    ("gitlab.com", "ecdsa-sha2-nistp256"): "HbW3g8zUjNSksFbqTiUWPWg2Bq1x8xdGUrliXFzSnUw",
}


def _fingerprint(blob: str) -> str:
    """SHA256 fingerprint of a base64 SSH key blob — identical to what
    ``ssh-keygen -lf`` prints (sha256 of the decoded key, base64, unpadded)."""
    return base64.b64encode(hashlib.sha256(base64.b64decode(blob)).digest()).decode().rstrip("=")


def _baked() -> dict[tuple[str, str], str]:
    text = files("terrapod.runner").joinpath("saas_known_hosts").read_text(encoding="utf-8")
    out: dict[tuple[str, str], str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        host, keytype, blob = line.split(" ", 2)
        assert (host, keytype) not in out, f"duplicate baked key for {host} {keytype}"
        out[(host, keytype)] = _fingerprint(blob)
    return out


def test_baked_known_hosts_match_published_fingerprints():
    """Every baked key re-derives to its provider-published fingerprint, and the
    baked set is exactly the expected set — no missing, no extra, no drift."""
    baked = _baked()
    # Exact set equality first — catches a missing OR an unexpected extra host/type.
    assert set(baked) == set(_EXPECTED)
    # Then the fingerprint of each baked key must match the published value.
    for hostkey, expected_fp in _EXPECTED.items():
        assert baked[hostkey] == expected_fp, (
            f"{hostkey[0]} {hostkey[1]}: baked key fingerprints to SHA256:{baked[hostkey]} "
            f"but the published value is SHA256:{expected_fp} — the baked host key is wrong/stale"
        )


def test_both_saas_hosts_are_covered():
    """Both SaaS hosts must be present (the whole point is out-of-box ssh:// to
    github.com AND gitlab.com), each with its modern ed25519 key."""
    baked = _baked()
    assert ("github.com", "ssh-ed25519") in baked
    assert ("gitlab.com", "ssh-ed25519") in baked
