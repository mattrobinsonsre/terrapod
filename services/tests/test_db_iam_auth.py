"""Tests for cloud-IAM database auth (#573) — AWS RDS IAM, GCP Cloud SQL IAM,
Azure Entra.

The live connection path per cloud can only be validated against a real
IAM-enabled database (a staging smoke); these tests cover the unit logic — token
minting per cloud, target parsing, and the do_connect handler that injects the
token + TLS and dispatches by mode — and guard that the default stays
static-password auth.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from terrapod.config import DatabaseConfig
from terrapod.db import iam_auth


def test_auth_mode_defaults_to_password_static():
    # Static-password auth must remain the default + fully supported.
    assert DatabaseConfig().auth_mode == "password"


def test_parse_pg_target():
    h, p, u = iam_auth.parse_pg_target("postgresql+asyncpg://terrapod@db.example.com:5432/terrapod")
    assert (h, p, u) == ("db.example.com", 5432, "terrapod")


def test_parse_pg_target_defaults_port_5432():
    h, p, u = iam_auth.parse_pg_target("postgresql+asyncpg://u@host/db")
    assert (h, p, u) == ("host", 5432, "u")


def test_mint_aws_rds_token_calls_botocore(monkeypatch):
    mock_client = MagicMock()
    mock_client.generate_db_auth_token.return_value = "AWS_TOKEN"
    monkeypatch.setattr(iam_auth, "_aws_rds_client", lambda region: mock_client)

    tok = iam_auth.mint_aws_rds_token(host="h", port=5432, user="u", region="us-east-1")

    assert tok == "AWS_TOKEN"
    mock_client.generate_db_auth_token.assert_called_once_with(
        DBHostname="h", Port=5432, DBUsername="u", Region="us-east-1"
    )


# ── do_connect dispatch per mode ──────────────────────────────────────


def test_do_connect_aws_injects_token_and_tls(monkeypatch):
    monkeypatch.setattr(iam_auth, "mint_aws_rds_token", lambda **_kw: "AWS")
    handler = iam_auth.make_do_connect_handler(
        auth_mode="aws_iam", host="h", port=5432, user="u", region="r", ssl_mode=""
    )
    cparams = {"password": "dummy"}
    handler(None, None, None, cparams)
    assert cparams["password"] == "AWS"  # overrides the URL value
    assert cparams["ssl"] == "require"  # TLS forced


def test_do_connect_gcp_uses_access_token(monkeypatch):
    monkeypatch.setattr(iam_auth, "mint_gcp_access_token", lambda: "GCP")
    handler = iam_auth.make_do_connect_handler(
        auth_mode="gcp_iam", host="h", port=5432, user="u", region="", ssl_mode="verify-ca"
    )
    cparams: dict = {}
    handler(None, None, None, cparams)
    assert cparams["password"] == "GCP"
    assert cparams["ssl"] == "verify-ca"


def test_do_connect_azure_uses_entra_token(monkeypatch):
    monkeypatch.setattr(iam_auth, "mint_azure_ad_token", lambda: "AZURE")
    handler = iam_auth.make_do_connect_handler(
        auth_mode="azure_ad", host="h", port=5432, user="u", region="", ssl_mode=""
    )
    cparams: dict = {}
    handler(None, None, None, cparams)
    assert cparams["password"] == "AZURE"
    assert cparams["ssl"] == "require"


def test_do_connect_honors_explicit_ssl_mode(monkeypatch):
    monkeypatch.setattr(iam_auth, "mint_aws_rds_token", lambda **_kw: "T")
    handler = iam_auth.make_do_connect_handler(
        auth_mode="aws_iam", host="h", port=5432, user="u", region="r", ssl_mode="verify-full"
    )
    cparams: dict = {}
    handler(None, None, None, cparams)
    assert cparams["ssl"] == "verify-full"


def test_do_connect_mints_fresh_token_each_connection(monkeypatch):
    tokens = iter(["t1", "t2"])
    monkeypatch.setattr(iam_auth, "mint_aws_rds_token", lambda **_kw: next(tokens))
    handler = iam_auth.make_do_connect_handler(
        auth_mode="aws_iam", host="h", port=5432, user="u", region="r", ssl_mode=""
    )
    c1: dict = {}
    c2: dict = {}
    handler(None, None, None, c1)
    handler(None, None, None, c2)
    assert c1["password"] == "t1"
    assert c2["password"] == "t2"


def test_unsupported_mode_raises():
    with pytest.raises(ValueError, match="unsupported IAM"):
        iam_auth.make_do_connect_handler(
            auth_mode="nope", host="h", port=5432, user="u", region="", ssl_mode=""
        )
