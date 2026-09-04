"""Vault value-source resolution (#1439).

The resolver turns a stored reference into a concrete value at next_run. Its
defining property, and the one these tests exist for, is that **it fails the
run rather than delivering nothing** — the opposite of git-auth's drop-and-warn,
because a silently absent credential leaves terraform to fail somewhere
confusing or fall back to an identity nobody chose.
"""

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.config import Settings, VaultConfig
from terrapod.services import vault_source_service as vss
from terrapod.services.vault_client import VaultError
from terrapod.services.vault_source_service import (
    VaultSourceError,
    parse_reference,
    resolve_vault_variables,
)


@dataclass
class _Var:
    key: str
    value: str
    category: str = "env"
    hcl: bool = False
    sensitive: bool = True
    value_source: str = "vault"


def _ref(**kw) -> str:
    base = {"source": "vault", "mount": "kvv2", "path": "apps/netbox", "field": "apitoken"}
    base.update(kw)
    return json.dumps(base)


def _settings(instances, enabled=True) -> Settings:
    s = Settings()
    s.vault = VaultConfig(enabled=enabled, instances=instances)
    return s


_ONE = [{"name": "default", "address": "https://v:8200"}]


class TestReferenceValidation:
    """Validated at write time too, but re-checked here: a row can be written by
    an older client, restored from a backup, or edited in the database."""

    def test_a_non_json_value_is_rejected(self):
        with pytest.raises(VaultSourceError, match="not JSON"):
            parse_reference("not json at all", key="TOKEN")

    def test_a_json_scalar_is_rejected(self):
        with pytest.raises(VaultSourceError, match="not an object"):
            parse_reference('"just a string"', key="TOKEN")

    def test_missing_coordinates_are_named(self):
        with pytest.raises(VaultSourceError) as e:
            parse_reference(json.dumps({"source": "vault", "mount": "kvv2"}), key="TOKEN")
        assert "path" in str(e.value) and "field" in str(e.value)

    def test_an_unknown_engine_is_rejected(self):
        with pytest.raises(VaultSourceError, match="unknown vault engine"):
            parse_reference(_ref(engine="kv1"), key="TOKEN")

    def test_an_unsupported_method_is_rejected(self):
        with pytest.raises(VaultSourceError, match="unsupported vault method"):
            parse_reference(_ref(method="DELETE"), key="TOKEN")

    def test_a_non_object_data_is_rejected(self):
        with pytest.raises(VaultSourceError, match="`data` that is not an object"):
            parse_reference(_ref(data="oops"), key="TOKEN")


class TestInstanceSelection:
    @pytest.mark.asyncio
    async def test_a_sole_instance_needs_no_name(self):
        with patch.object(vss, "read_secret", new=AsyncMock(return_value="v")):
            out = await resolve_vault_variables([_Var("T", _ref())], _settings(_ONE))
        assert out == {"T": "v"}

    @pytest.mark.asyncio
    async def test_the_default_marked_instance_wins_when_several_exist(self):
        insts = [
            {"name": "a", "address": "https://a"},
            {"name": "b", "address": "https://b", "default": True},
        ]
        seen = {}

        async def _read(inst, **kw):
            seen["name"] = inst.name
            return "v"

        with patch.object(vss, "read_secret", new=_read):
            await resolve_vault_variables([_Var("T", _ref())], _settings(insts))
        assert seen["name"] == "b"

    @pytest.mark.asyncio
    async def test_an_ambiguous_reference_is_refused_not_guessed(self):
        """Never 'first wins': reading a credential from the wrong Vault is
        silent, and silence is the failure mode worth engineering against."""
        insts = [{"name": "a", "address": "https://a"}, {"name": "b", "address": "https://b"}]
        with pytest.raises(VaultSourceError, match="none is marked default"):
            await resolve_vault_variables([_Var("T", _ref())], _settings(insts))

    @pytest.mark.asyncio
    async def test_an_unknown_instance_name_is_refused(self):
        with pytest.raises(VaultSourceError, match="unknown vault instance 'nope'"):
            await resolve_vault_variables([_Var("T", _ref(vault="nope"))], _settings(_ONE))


class TestFailureIsFatal:
    @pytest.mark.asyncio
    async def test_a_vault_error_propagates_rather_than_dropping_the_variable(self):
        # git-auth would drop this and carry on. Here it must stop the run.
        with patch.object(vss, "read_secret", new=AsyncMock(side_effect=VaultError("denied"))):
            with pytest.raises(VaultSourceError, match="variable 'T'"):
                await resolve_vault_variables([_Var("T", _ref())], _settings(_ONE))

    @pytest.mark.asyncio
    async def test_referencing_vault_while_disabled_is_an_error(self):
        """Not a silent no-op: the operator turned the feature off with variables
        still pointing at it, and needs to know."""
        with pytest.raises(VaultSourceError, match="Vault value source is disabled"):
            await resolve_vault_variables([_Var("T", _ref())], _settings(_ONE, enabled=False))

    @pytest.mark.asyncio
    async def test_the_error_names_the_variable_so_it_can_be_found(self):
        with patch.object(vss, "read_secret", new=AsyncMock(side_effect=VaultError("boom"))):
            with pytest.raises(VaultSourceError) as e:
                await resolve_vault_variables([_Var("DB_PASSWORD", _ref())], _settings(_ONE))
        assert "DB_PASSWORD" in str(e.value)


class TestScoping:
    @pytest.mark.asyncio
    async def test_static_variables_are_left_alone(self):
        static = _Var("PLAIN", "a literal value", value_source="static")
        assert await resolve_vault_variables([static], _settings(_ONE)) == {}

    @pytest.mark.asyncio
    async def test_no_vault_variables_means_no_config_requirement(self):
        """A deployment with the feature off must not fail merely because a run
        has ordinary variables."""
        static = _Var("PLAIN", "x", value_source="static")
        assert await resolve_vault_variables([static], _settings([], enabled=False)) == {}

    @pytest.mark.asyncio
    async def test_the_reference_coordinates_reach_the_client(self):
        captured = {}

        async def _read(inst, **kw):
            captured.update(kw)
            return "v"

        ref = _ref(
            mount="aws",
            path="creds/deploy",
            field="secret_key",
            engine="dynamic",
            method="POST",
            data={"ttl": "1h"},
        )
        with patch.object(vss, "read_secret", new=_read):
            await resolve_vault_variables([_Var("T", ref)], _settings(_ONE))
        assert captured["mount"] == "aws"
        assert captured["path"] == "creds/deploy"
        assert captured["engine"] == "dynamic"
        assert captured["method"] == "POST"
        assert captured["data"] == {"ttl": "1h"}
