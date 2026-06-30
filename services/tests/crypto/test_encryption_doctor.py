"""Tests for the encryption_doctor CLI exit-code contract (#553 audit).

The doctor is the operator's "are we still recoverable?" drill — its value is the
non-zero exit on failure (so a CronJob/CI gate can act on it). verify_live itself
is unit-tested in test_crypto.py; here we assert the CLI wrapper honours the
exit-code contract.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from terrapod.cli import encryption_doctor as doc


def _patch_db(monkeypatch):
    monkeypatch.setattr(doc, "init_db", AsyncMock())
    monkeypatch.setattr(doc, "close_db", AsyncMock())

    @asynccontextmanager
    async def _session():
        yield AsyncMock()

    monkeypatch.setattr(doc, "get_db_session", _session)


@pytest.mark.asyncio
async def test_doctor_exits_nonzero_on_undecryptable(monkeypatch):
    _patch_db(monkeypatch)
    monkeypatch.setattr(
        doc, "verify_live", AsyncMock(return_value={"ok": False, "failures": ["v1: unwrap failed"]})
    )
    with pytest.raises(SystemExit) as exc:
        await doc.doctor()
    assert exc.value.code == 1


@pytest.mark.asyncio
async def test_doctor_passes_when_all_decryptable(monkeypatch):
    _patch_db(monkeypatch)
    monkeypatch.setattr(
        doc, "verify_live", AsyncMock(return_value={"ok": True, "checked_versions": [1, 2]})
    )
    # No SystemExit on success.
    await doc.doctor()
