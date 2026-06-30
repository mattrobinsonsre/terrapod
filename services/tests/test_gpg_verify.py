"""Tests for the shared OpenPGP verification core (gpg_verify)."""

import inspect

from terrapod import gpg_verify


def test_gpg_verify_installs_pgpy_warning_filter():
    """gpg_verify must suppress pgpy's static UserWarning TODO banners
    (self-sigs / revocation / flags) so they don't flood the API + runner logs
    on every verify. Source-introspection guard (pytest manages warnings.filters
    per-test, so a runtime-filter check is unreliable): if the filterwarnings
    call is removed, this fails. See #640 for the revocation caveat it documents."""
    src = inspect.getsource(gpg_verify)
    assert "filterwarnings" in src, "gpg_verify must install a warnings filter for pgpy"
    assert "category=UserWarning" in src and '"pgpy"' in src, (
        "the filter must be scoped to pgpy UserWarnings, not a blanket silence"
    )


def test_parse_sha256sums_tolerates_formatting():
    out = gpg_verify.parse_sha256sums(
        "ABC123  file.zip\n"  # uppercase digest, two-space sep
        "def456  *other.bin\n"  # binary-mode marker (*) on the filename
        "\n"  # blank line
        "garbage\n"  # short line, skipped
    )
    assert out == {"file.zip": "abc123", "other.bin": "def456"}
