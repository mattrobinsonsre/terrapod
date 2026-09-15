"""Every Vault read is audited, and no audit row carries a value (#1651).

The resolver records one `VaultReadRecord` per read it attempts — including the
one that fails — before raising, and `vault_read_audit_entries` turns them into
`vault.read` rows. The properties:

- each outcome (ok, denied, missing, transient, error) is recorded from the
  exception's type, not guessed from a message;
- a shared read that serves several variables is one record naming them all;
- a claim refused before any read records nothing;
- a record has no attribute that could hold a value, and the row builder never
  touches one (pinned by source introspection).
"""

import ast
import dataclasses
import inspect
import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from terrapod.config import Settings, VaultConfig, VaultInstanceConfig
from terrapod.services import audit_service, vault_client
from terrapod.services import vault_source_service as vss
from terrapod.services.vault_client import (
    VaultDenied,
    VaultError,
    VaultNotFound,
    VaultResponse,
    VaultUnavailable,
    read_secret_response,
    reset_token_cache,
)
from terrapod.services.vault_source_service import (
    READ_OUTCOMES,
    VaultReadRecord,
    VaultSourceError,
    resolve_vault_delivery,
    vault_read_audit_entries,
)

SECRET = "S3CR3T-audit-value-must-not-leak"


@dataclass
class _Var:
    key: str
    value: str
    category: str = "env"
    hcl: bool = False
    sensitive: bool = True
    value_source: str = "vault"


def _ref(**kw) -> str:
    base = {"source": "vault", "mount": "kvv2", "path": "apps/x", "field": "token"}
    base.update(kw)
    return json.dumps(base)


def _settings() -> Settings:
    s = Settings()
    s.vault = VaultConfig(enabled=True, instances=[{"name": "default", "address": "https://v"}])
    return s


async def _resolve(variables, mock) -> tuple[list[VaultReadRecord], Exception | None]:
    reads: list[VaultReadRecord] = []
    err = None
    with patch.object(vss, "read_secret_response", new=mock):
        try:
            await resolve_vault_delivery(variables, _settings(), reads=reads)
        except VaultSourceError as e:
            err = e
    return reads, err


# ── The resolver records each read ────────────────────────────────────


class TestRecords:
    @pytest.mark.asyncio
    async def test_a_good_read_is_ok_and_names_every_variable_that_shared_it(self):
        mock = AsyncMock(return_value=VaultResponse({"token": SECRET, "user": "u"}))
        reads, err = await _resolve(
            [_Var("TOKEN", _ref()), _Var("USER", _ref(field="user", mount="/kvv2/"))], mock
        )
        assert err is None
        assert reads == [
            VaultReadRecord(
                keys=("TOKEN", "USER"),
                instance="default",
                mount="kvv2",
                path="apps/x",
                engine="kv2",
                outcome="ok",
            )
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc", "outcome"),
        [
            (VaultDenied("denied"), "denied"),
            (VaultNotFound("missing"), "missing"),
            (VaultUnavailable("HTTP 503"), "transient"),
            (VaultError("illegal character"), "error"),
            (RuntimeError("unexpected"), "error"),
        ],
    )
    async def test_a_failed_read_is_recorded_with_its_outcome_before_raising(self, exc, outcome):
        reads, err = await _resolve([_Var("T", _ref(engine="dynamic"))], AsyncMock(side_effect=exc))
        assert err is not None
        assert [(r.keys, r.engine, r.outcome) for r in reads] == [(("T",), "dynamic", outcome)]

    @pytest.mark.asyncio
    async def test_a_failure_stops_the_reads_that_would_have_followed(self):
        mock = AsyncMock(side_effect=VaultDenied("denied"))
        reads, _ = await _resolve([_Var("A", _ref()), _Var("B", _ref(path="apps/y"))], mock)
        assert [r.keys for r in reads] == [("A",)]
        assert mock.await_count == 1

    @pytest.mark.asyncio
    async def test_a_field_missing_from_a_good_read_is_still_recorded_as_ok(self):
        reads, err = await _resolve(
            [_Var("T", _ref(field="nope"))], AsyncMock(return_value=VaultResponse({"token": "x"}))
        )
        assert err is not None
        assert [r.outcome for r in reads] == ["ok"]

    @pytest.mark.asyncio
    async def test_a_claim_refused_before_any_read_records_nothing(self):
        mock = AsyncMock(return_value=VaultResponse({"token": "x"}))
        reads, err = await _resolve(
            [_Var("A", _ref(file={"name": "f"})), _Var("B", _ref(path="p", file={"name": "f"}))],
            mock,
        )
        assert err is not None and reads == []
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_without_a_collector_resolution_is_unchanged(self):
        mock = AsyncMock(return_value=VaultResponse({"token": "v"}))
        with patch.object(vss, "read_secret_response", new=mock):
            out = await resolve_vault_delivery([_Var("T", _ref())], _settings())
        assert out.values == {"T": "v"}


# ── The rows ──────────────────────────────────────────────────────────


class TestEntries:
    def test_one_row_per_record_with_the_documented_shape(self):
        reads = [
            VaultReadRecord(("A", "B"), "default", "aws", "creds/deploy", "dynamic", "ok"),
            VaultReadRecord(("C",), "second", "kvv2", "apps/x", "kv2", "denied"),
        ]
        rows = vault_read_audit_entries(run_id="0190-abc", phase="apply", reads=reads)
        assert [{k: v for k, v in r.items() if k != "detail"} for r in rows] == [
            {
                "action": "vault.read",
                "actor_type": "system",
                "origin": "system",
                "resource_type": "runs",
                "resource_id": "run-0190-abc",
                "status_code": 200,
            },
            {
                "action": "vault.read",
                "actor_type": "system",
                "origin": "system",
                "resource_type": "runs",
                "resource_id": "run-0190-abc",
                "status_code": 403,
            },
        ]
        assert json.loads(rows[0]["detail"]) == {
            "keys": ["A", "B"],
            "instance": "default",
            "mount": "aws",
            "path": "creds/deploy",
            "engine": "dynamic",
            "phase": "apply",
            "outcome": "ok",
        }

    @pytest.mark.parametrize(
        ("outcome", "status"),
        [("ok", 200), ("denied", 403), ("missing", 404), ("transient", 503), ("error", 500)],
    )
    def test_each_outcome_has_a_status(self, outcome, status):
        rec = VaultReadRecord(("A",), "default", "m", "p", "kv2", outcome)
        assert (
            vault_read_audit_entries(run_id="r", phase="plan", reads=[rec])[0]["status_code"]
            == status
        )

    def test_the_outcomes_are_named_in_one_place(self):
        assert READ_OUTCOMES == ("ok", "denied", "missing", "transient", "error")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("side_effect", [None, VaultDenied("d"), VaultUnavailable("u")])
    async def test_no_row_carries_the_value_whatever_the_outcome(self, side_effect):
        mock = AsyncMock(
            return_value=VaultResponse({"token": SECRET, "k": SECRET}), side_effect=side_effect
        )
        reads, _ = await _resolve(
            [
                _Var("T", _ref()),
                _Var("F", _ref(field=None, file={"template": "{{ token }}{{ k }}"})),
            ],
            mock,
        )
        rows = vault_read_audit_entries(run_id="r", phase="plan", reads=reads)
        assert rows
        assert SECRET not in json.dumps(rows)


class TestNoValueCanReachARow:
    """Source-level: the record has nowhere to put a value, and the builder
    reads nothing but the record's names and coordinates."""

    def test_the_record_has_only_names_and_coordinates(self):
        assert [f.name for f in dataclasses.fields(VaultReadRecord)] == [
            "keys",
            "instance",
            "mount",
            "path",
            "engine",
            "outcome",
        ]

    def test_the_row_builder_touches_no_value(self):
        tree = ast.parse(inspect.getsource(vault_read_audit_entries))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
        }
        assert not names & {"value", "values", "data", "secret", "response", "files", "lease"}
        # It reads the record's fields and nothing else off a record.
        record_attrs = {
            n.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "r"
        }
        assert record_attrs <= {f.name for f in dataclasses.fields(VaultReadRecord)}


# ── The batch helper ──────────────────────────────────────────────────


class TestAddAuditEvents:
    def test_it_stages_every_row_in_one_call_and_does_not_commit(self):
        db = MagicMock()
        db.commit = AsyncMock()
        n = audit_service.add_audit_events(
            db,
            vault_read_audit_entries(
                run_id="r",
                phase="plan",
                reads=[
                    VaultReadRecord(("A",), "default", "m", "p", "kv2", "ok"),
                    VaultReadRecord(("B",), "default", "m", "q", "kv2", "missing"),
                ],
            ),
        )
        assert n == 2
        db.add_all.assert_called_once()
        rows = db.add_all.call_args.args[0]
        assert [(r.action, r.status_code) for r in rows] == [
            ("vault.read", 200),
            ("vault.read", 404),
        ]
        assert all(r.id is not None for r in rows)
        db.commit.assert_not_awaited()

    def test_nothing_to_stage_touches_nothing(self):
        db = MagicMock()
        assert audit_service.add_audit_events(db, []) == 0
        db.add_all.assert_not_called()


# ── The client says what kind of refusal it was ───────────────────────


def _serving(status: int, body: dict | None = None):
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body or {"errors": []})

    reset_token_cache()
    return patch.object(
        vault_client.httpx,
        "AsyncClient",
        lambda *a, **kw: real(transport=httpx.MockTransport(handler)),
    )


def _inst(**kw) -> VaultInstanceConfig:
    base = {
        "name": "default",
        "address": "https://vault.test:8200",
        "auth": {"method": "token", "mount": "token", "role": "n/a"},
    }
    base.update(kw)
    return VaultInstanceConfig(**base)


class TestTypedRefusals:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "exc"),
        [(403, VaultDenied), (404, VaultNotFound), (503, VaultUnavailable)],
    )
    async def test_a_read_status_maps_to_its_type(self, status, exc):
        with _serving(status), pytest.raises(exc):
            await read_secret_response(_inst(), mount="kvv2", path="a", static_token="t")

    @pytest.mark.asyncio
    async def test_a_transient_status_is_never_denied(self):
        with _serving(503), pytest.raises(VaultError) as e:
            await read_secret_response(_inst(), mount="kvv2", path="a", static_token="t")
        assert not isinstance(e.value, VaultDenied)

    @pytest.mark.asyncio
    async def test_a_refused_login_is_denied(self):
        inst = _inst(auth={"method": "approle", "mount": "approle", "role": "r"})
        with _serving(400), pytest.raises(VaultDenied, match="Vault login failed"):
            await read_secret_response(inst, mount="kvv2", path="a", static_token="secret-id")

    @pytest.mark.asyncio
    async def test_the_allow_list_is_a_denial(self):
        with pytest.raises(VaultDenied, match="not in the allow-list"):
            await read_secret_response(
                _inst(paths=["secret/apps"]), mount="kvv2", path="a", static_token="t"
            )
