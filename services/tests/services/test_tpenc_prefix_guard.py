"""A user-supplied value cannot brick its own row (GHSA-7qcr-cvvg-w58f).

`is_encrypted` is a bare prefix test, and with encryption disabled `encrypt()`
is a passthrough — so a plaintext value beginning `tpenc:` was stored verbatim
and every later read of the row raised inside SQLAlchemy's result processing.
That makes the row unreadable, and since the delete path loads the row first,
undeletable through the API as well.
"""

import pytest

from terrapod.crypto import envelope
from terrapod.crypto.types import EncryptedText


class _Svc:
    def __init__(self, enabled):
        self.enabled = enabled

    def encrypt(self, plaintext):
        # Mirrors the real passthrough-when-disabled behaviour.
        return plaintext if not self.enabled else f"{envelope.MARKER}:1:1:n:{plaintext}"


def _bind(value, *, enabled, monkeypatch):
    monkeypatch.setattr("terrapod.crypto.service.get_encryption", lambda: _Svc(enabled))
    return EncryptedText().process_bind_param(value, None)


class TestWithEncryptionDisabled:
    def test_a_forged_envelope_prefix_is_refused(self, monkeypatch):
        with pytest.raises(ValueError, match="tpenc"):
            _bind("tpenc:not-really-an-envelope", enabled=False, monkeypatch=monkeypatch)

    def test_even_a_well_formed_looking_one_is_refused(self, monkeypatch):
        # It is not ours -- we did not encrypt it -- so on read it would fail to
        # decrypt just the same.
        with pytest.raises(ValueError):
            _bind("tpenc:1:1:AAAA:BBBB", enabled=False, monkeypatch=monkeypatch)

    def test_ordinary_values_are_untouched(self, monkeypatch):
        for v in ("hunter2", "", "tpenc", "TPENC:x", "prefix-tpenc:x"):
            assert _bind(v, enabled=False, monkeypatch=monkeypatch) == v

    def test_none_passes_through(self, monkeypatch):
        assert _bind(None, enabled=False, monkeypatch=monkeypatch) is None


class TestWithEncryptionEnabled:
    def test_the_same_value_is_accepted(self, monkeypatch):
        """Deliberately allowed.

        With encryption on the value is wrapped into a real envelope and reads
        back as the literal text the user wrote. Refusing it here would reject a
        legitimate value in exactly the configuration where it is safe.
        """
        out = _bind("tpenc:looks-like-one", enabled=True, monkeypatch=monkeypatch)
        assert out.startswith(f"{envelope.MARKER}:")
        assert "looks-like-one" in out


class TestItCoversEveryEncryptedColumn:
    def test_the_guard_is_on_the_column_type_not_one_router(self):
        """The reported instance was a workspace variable, but the same value in
        a VCS token or a webhook secret bricks that row identically. Guarding the
        type means every `EncryptedText` column inherits it."""
        import inspect

        src = inspect.getsource(EncryptedText.process_bind_param)
        assert "is_encrypted" in src
