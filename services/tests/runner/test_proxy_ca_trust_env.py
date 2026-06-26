"""#592 — source-introspection invariant: no httpx client opts out of env trust.

httpx defaults `trust_env=True`, so every client already honours the standard
proxy + CA env vars (HTTP_PROXY/HTTPS_PROXY/NO_PROXY and
SSL_CERT_FILE/REQUESTS_CA_BUNDLE) that the chart injects for the forward-proxy +
custom-CA feature. The one way to silently break that feature is to construct a
client with `trust_env=False`. This test reads the implementation and fails
loudly if any source file does so, so the invariant can't regress unnoticed.

If a specific client genuinely must bypass env (rare — e.g. a localhost-only
health probe that must never be proxied), add it to ALLOWED with a rationale.
"""

from __future__ import annotations

import pathlib

# (file_suffix, reason) pairs explicitly permitted to set trust_env=False.
ALLOWED: set[str] = set()


def _src_root() -> pathlib.Path:
    # services/tests/runner/<this> → services/terrapod
    return pathlib.Path(__file__).resolve().parents[2] / "terrapod"


def test_no_httpx_client_disables_trust_env() -> None:
    root = _src_root()
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "trust_env" not in text:
            continue
        # Tolerate whitespace variants: trust_env = False / trust_env=False.
        normalised = text.replace(" ", "")
        if "trust_env=False" in normalised and str(path) not in ALLOWED:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, (
        "httpx clients must not set trust_env=False — it disables the #592 "
        f"forward-proxy + custom-CA env vars. Offenders: {offenders}"
    )
