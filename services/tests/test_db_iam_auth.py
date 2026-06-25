"""Tests for cloud-IAM database auth (#573) — AWS RDS IAM, GCP Cloud SQL IAM,
Azure Entra.

The live connection path per cloud can only be validated against a real
IAM-enabled database (a staging smoke); these tests cover the unit logic — token
minting per cloud, target parsing, the TLS-context builder, and the do_connect
handler that supplies an awaitable token password + TLS context and dispatches by
mode — and guard that the default stays static-password auth.
"""

from __future__ import annotations

import asyncio
import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import DatabaseConfig
from terrapod.db import iam_auth


def test_auth_mode_defaults_to_password_static():
    # Static-password auth must remain the default + fully supported.
    cfg = DatabaseConfig()
    assert cfg.auth_mode == "password"
    assert cfg.ssl_root_cert == ""


def test_parse_pg_target():
    h, p, u = iam_auth.parse_pg_target("postgresql+asyncpg://terrapod@db.example.com:5432/terrapod")
    assert (h, p, u) == ("db.example.com", 5432, "terrapod")


def test_parse_pg_target_defaults_port_5432():
    h, p, u = iam_auth.parse_pg_target("postgresql+asyncpg://u@host/db")
    assert (h, p, u) == ("host", 5432, "u")


def test_parse_pg_target_percent_decodes_username():
    # GCP Cloud SQL IAM users are the SA email (contains '@', encoded as %40).
    _, _, u = iam_auth.parse_pg_target("postgresql+asyncpg://sa%40proj.iam@10.0.0.5:5432/terrapod")
    assert u == "sa@proj.iam"


def test_mint_aws_rds_token_calls_botocore(monkeypatch):
    mock_client = MagicMock()
    mock_client.generate_db_auth_token.return_value = "AWS_TOKEN"
    monkeypatch.setattr(iam_auth, "_aws_rds_client", lambda region: mock_client)

    tok = iam_auth.mint_aws_rds_token(host="h", port=5432, user="u", region="us-east-1")

    assert tok == "AWS_TOKEN"
    mock_client.generate_db_auth_token.assert_called_once_with(
        DBHostname="h", Port=5432, DBUsername="u", Region="us-east-1"
    )


# ── TLS context builder ───────────────────────────────────────────────


def test_build_ssl_context_require_disables_verification():
    ctx = iam_auth.build_ssl_context("require", "")
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_build_ssl_context_empty_defaults_to_require():
    ctx = iam_auth.build_ssl_context("", "")
    assert ctx.verify_mode == ssl.CERT_NONE


def test_build_ssl_context_verify_ca_requires_cert_not_hostname():
    ctx = iam_auth.build_ssl_context("verify-ca", "")
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_build_ssl_context_verify_full_checks_hostname():
    ctx = iam_auth.build_ssl_context("verify-full", "")
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_build_ssl_context_loads_root_cert(tmp_path):
    bogus = tmp_path / "ca.pem"
    bogus.write_text("not a real cert\n")
    # load_verify_locations on a non-PEM file raises — proves the cert path is used.
    with pytest.raises(ssl.SSLError):
        iam_auth.build_ssl_context("verify-full", str(bogus))


def test_build_ssl_context_rejects_unknown_mode():
    with pytest.raises(ValueError, match="unsupported ssl_mode"):
        iam_auth.build_ssl_context("bogus", "")


# ── do_connect dispatch per mode ──────────────────────────────────────


def _run_password(cparams: dict) -> str:
    """asyncpg invokes the callable password and awaits its result."""
    pw = cparams["password"]
    assert callable(pw)
    return asyncio.run(pw())


def test_do_connect_aws_sets_awaitable_token_and_ssl(monkeypatch):
    monkeypatch.setattr(iam_auth, "mint_aws_rds_token", lambda **_kw: "AWS")
    handler = iam_auth.make_do_connect_handler(
        auth_mode="aws_iam", host="h", port=5432, user="u", region="r", ssl_mode=""
    )
    cparams = {"password": "dummy"}
    handler(None, None, None, cparams)
    assert isinstance(cparams["ssl"], ssl.SSLContext)
    assert _run_password(cparams) == "AWS"  # overrides the URL value


def test_do_connect_gcp_uses_access_token(monkeypatch):
    monkeypatch.setattr(iam_auth, "mint_gcp_access_token", lambda: "GCP")
    handler = iam_auth.make_do_connect_handler(
        auth_mode="gcp_iam",
        host="h",
        port=5432,
        user="u",
        region="",
        ssl_mode="verify-ca",
    )
    cparams: dict = {}
    handler(None, None, None, cparams)
    assert _run_password(cparams) == "GCP"
    assert cparams["ssl"].verify_mode == ssl.CERT_REQUIRED


def test_do_connect_azure_uses_entra_token(monkeypatch):
    monkeypatch.setattr(iam_auth, "mint_azure_ad_token", lambda: "AZURE")
    handler = iam_auth.make_do_connect_handler(
        auth_mode="azure_ad", host="h", port=5432, user="u", region="", ssl_mode=""
    )
    cparams: dict = {}
    handler(None, None, None, cparams)
    assert _run_password(cparams) == "AZURE"


def test_do_connect_mints_fresh_token_each_call(monkeypatch):
    tokens = iter(["t1", "t2"])
    monkeypatch.setattr(iam_auth, "mint_aws_rds_token", lambda **_kw: next(tokens))
    handler = iam_auth.make_do_connect_handler(
        auth_mode="aws_iam", host="h", port=5432, user="u", region="r", ssl_mode=""
    )
    c1: dict = {}
    c2: dict = {}
    handler(None, None, None, c1)
    handler(None, None, None, c2)
    assert _run_password(c1) == "t1"
    assert _run_password(c2) == "t2"


def test_unsupported_mode_raises():
    with pytest.raises(ValueError, match="unsupported IAM"):
        iam_auth.make_do_connect_handler(
            auth_mode="nope", host="h", port=5432, user="u", region="", ssl_mode=""
        )


# ── session wiring ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_db_registers_listener_only_for_iam_mode():
    """The do_connect listener is attached for IAM modes, not for 'password'."""
    from terrapod.db import session as db_session

    async def _check(auth_mode: str, *, expect_listener: bool):
        cfg = DatabaseConfig(auth_mode=auth_mode)
        fake_engine = MagicMock()
        with (
            patch.object(db_session, "settings") as fake_settings,
            patch.object(db_session, "create_async_engine", return_value=fake_engine),
            patch.object(db_session, "async_sessionmaker"),
            patch.object(db_session, "event") as fake_event,
        ):
            fake_settings.database = cfg
            fake_settings.debug = False
            fake_settings.database_url = (
                "postgresql+asyncpg://terrapod@db.example.com:5432/terrapod"
            )
            # The startup SELECT 1 uses an async context manager on the engine.
            fake_engine.begin.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            fake_engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)
            await db_session.init_db()
            if expect_listener:
                fake_event.listen.assert_called_once()
                assert fake_event.listen.call_args.args[1] == "do_connect"
            else:
                fake_event.listen.assert_not_called()

    await _check("password", expect_listener=False)
    await _check("aws_iam", expect_listener=True)


def test_register_engine_iam_auth_env_gated(monkeypatch):
    """The shared helper (migrations + bootstrap Jobs) registers do_connect only
    for an IAM TP_DB_AUTH_MODE, and is a no-op for the default password mode."""
    import sqlalchemy

    calls = []
    monkeypatch.setattr(sqlalchemy.event, "listen", lambda *a, **_k: calls.append(a))
    engine = MagicMock()
    url = "postgresql+asyncpg://terrapod@db.example.com:5432/terrapod"

    monkeypatch.delenv("TP_DB_AUTH_MODE", raising=False)
    assert iam_auth.register_engine_iam_auth(engine, url) is False
    assert calls == []

    monkeypatch.setenv("TP_DB_AUTH_MODE", "aws_iam")
    assert iam_auth.register_engine_iam_auth(engine, url) is True
    assert len(calls) == 1
    assert calls[0][0] is engine  # the sync_engine we passed
    assert calls[0][1] == "do_connect"
