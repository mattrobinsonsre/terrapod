"""Vault file delivery and one-read-per-secret resolution (#1619).

The resolver is where a file-mode variable's value is swapped for its path and
the content is split off into `vault-files`, and where variables naming the
same secret share a single Vault read. The properties these tests exist for:

- **the content never becomes a variable value** — not on success, not for an
  older caller, not in an error message or a log line;
- **fields of one secret come from one response**, so a dynamic engine's
  certificate and key (or access key and secret) belong to one credential.
"""

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from terrapod.config import Settings, VaultConfig
from terrapod.services import vault_client
from terrapod.services import vault_source_service as vss
from terrapod.services.vault_client import VaultError, VaultResponse, VaultUnavailable
from terrapod.services.vault_source_service import (
    RESERVED_FILE_KEYS,
    VaultSourceError,
    VaultTransient,
    looks_like_file_reference,
    parse_reference,
    render_file_content,
    resolve_vault_delivery,
    resolve_vault_variables,
)

SECRET = "S3CR3T-file-content-that-must-not-leak"


@dataclass
class _Var:
    key: str
    value: str
    category: str = "env"
    hcl: bool = False
    sensitive: bool = True
    value_source: str = "vault"


def _ref(**kw) -> str:
    base = {"source": "vault", "mount": "kvv2", "path": "apps/gcp", "field": "sa"}
    base.update(kw)
    return json.dumps(base)


def _settings(instances=None) -> Settings:
    s = Settings()
    s.vault = VaultConfig(
        enabled=True, instances=instances or [{"name": "default", "address": "https://v"}]
    )
    return s


def _read(value=SECRET):
    return patch.object(
        vss, "read_secret_response", new=AsyncMock(return_value=VaultResponse({"sa": value}))
    )


# ── Parsing the `file` object ─────────────────────────────────────────


class TestParseFile:
    def test_an_empty_file_object_is_accepted(self):
        assert parse_reference(_ref(file={}), key="GOOGLE_CREDS")["file"] == {}

    def test_a_named_file_is_accepted(self):
        ref = parse_reference(_ref(file={"name": "gcp/adc.json"}), key="T")
        assert ref["file"] == {"name": "gcp/adc.json"}

    def test_a_null_file_means_no_file(self):
        assert parse_reference(_ref(file=None), key="T")["file"] is None

    def test_a_home_file_is_accepted(self):
        parse_reference(_ref(file={"name": "~/.aws/credentials"}), key="T")

    @pytest.mark.parametrize("bad", [True, "gcp/adc.json", ["x"], 1])
    def test_a_file_that_is_not_an_object_is_refused(self, bad):
        with pytest.raises(VaultSourceError) as e:
            parse_reference(_ref(file=bad), key="T")
        assert str(e.value) == "variable 'T' has a vault `file` that is not an object"

    def test_an_unknown_file_key_is_refused_not_ignored(self):
        with pytest.raises(VaultSourceError) as e:
            parse_reference(_ref(file={"name": "a", "colour": "blue"}), key="T")
        assert str(e.value) == (
            "variable 'T' has a vault `file` with unknown keys: colour (supported: `name`, "
            "`template`, `format`, `fields`, `encoding`)"
        )

    def test_the_reserved_keys_are_named_in_one_place(self):
        # template, format and encoding shipped in #1648; mode stays reserved.
        assert RESERVED_FILE_KEYS == ("mode",)

    def test_the_reserved_mode_key_is_refused_as_not_yet_supported(self):
        """A newer client's instruction must fail loudly on this server, not be
        dropped and a different file written."""
        with pytest.raises(VaultSourceError) as e:
            parse_reference(_ref(file={"name": "a", "mode": "0600"}), key="T")
        assert str(e.value) == (
            "variable 'T' has a vault `file` using mode, which is reserved for a later "
            "release and not supported yet (supported: `name`, `template`, `format`, "
            "`fields`, `encoding`)"
        )

    def test_a_reserved_key_beside_supported_ones_names_only_the_reserved_one(self):
        with pytest.raises(VaultSourceError, match="using mode, which is reserved"):
            parse_reference(_ref(file={"mode": "0600", "encoding": "base64"}), key="T")

    @pytest.mark.parametrize(
        ("name", "reason"),
        [
            ("", "must not be empty"),
            ("../etc/passwd", "has a '.' or '..' path segment"),
            ("/etc/passwd", "must be a relative path, or start with ~/ for a path in the"),
            ("a//b", "has an empty path segment"),
            ("a\x00", "contains a NUL character"),
            ("a\\b", "has a path segment with characters outside [A-Za-z0-9._-]"),
            ("x" * 256, "is longer than 255 characters"),
            ("~/.ssh/id_rsa", "targets ~/.ssh, which the runner manages itself"),
            ("~/.terraformrc", "targets ~/.terraformrc, which the runner manages itself"),
            (5, "must be a string"),
        ],
    )
    def test_an_invalid_name_is_refused_naming_variable_and_name(self, name, reason):
        with pytest.raises(VaultSourceError) as e:
            parse_reference(_ref(file={"name": name}), key="T")
        assert str(e.value).startswith(f"variable 'T': vault file name {name!r} is invalid: ")
        assert reason in str(e.value)

    def test_the_default_name_is_the_key_and_is_validated_too(self):
        with pytest.raises(VaultSourceError) as e:
            parse_reference(_ref(file={}), key="my key")
        assert str(e.value) == (
            "variable 'my key': vault file name 'my key' is invalid: has a path segment "
            "with characters outside [A-Za-z0-9._-]: 'my key'"
        )


class TestLooksLikeFileReference:
    @pytest.mark.parametrize(
        "raw",
        [
            _ref(file={}),
            json.dumps({"source": "vault", "file": {"name": "x"}}),
            json.dumps({"mount": "m", "path": "p", "field": "f", "file": {}}),
        ],
    )
    def test_a_reference_with_file_is_detected(self, raw):
        assert looks_like_file_reference(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "plain",
            "[1,2]",
            _ref(),  # a reference, but no file
            json.dumps({"file": "main.tf", "lines": 3}),  # ordinary JSON with a file key
            json.dumps({"source": "other", "file": {}}),
        ],
    )
    def test_anything_else_is_not(self, raw):
        assert not looks_like_file_reference(raw)


def test_the_render_seam_takes_the_reference_field_from_the_whole_response():
    """Today a file is one field; the seam receives the whole response so the
    reserved keys can later build content from more than one."""
    secret = {"certificate": "C", "private_key": "K"}
    assert render_file_content(secret, {}, "private_key", "pki/issue/x") == "K"
    with pytest.raises(VaultError, match="field 'nope' is not present at 'pki/issue/x'"):
        render_file_content(secret, {}, "nope", "pki/issue/x")


# ── Resolution: the value is the path ────────────────────────────────


class TestResolvedValueIsThePath:
    @pytest.mark.asyncio
    async def test_an_env_file_variable_carries_the_path_and_the_content_is_split_off(self):
        v = _Var("GOOGLE_APPLICATION_CREDENTIALS", _ref(file={"name": "gcp/adc.json"}))
        with _read():
            out = await resolve_vault_delivery([v], _settings())
        assert out.values == {
            "GOOGLE_APPLICATION_CREDENTIALS": "/var/run/terrapod/files/gcp/adc.json"
        }
        assert out.files == [
            {"key": "GOOGLE_APPLICATION_CREDENTIALS", "name": "gcp/adc.json", "value": SECRET}
        ]

    @pytest.mark.asyncio
    async def test_a_terraform_file_variable_carries_the_path(self):
        v = _Var("sa_file", _ref(file={}), category="terraform")
        with _read():
            out = await resolve_vault_delivery([v], _settings())
        assert out.values == {"sa_file": "/var/run/terrapod/files/sa_file"}

    @pytest.mark.asyncio
    async def test_a_home_file_resolves_under_the_runner_home(self):
        v = _Var("AWS_CREDS", _ref(file={"name": "~/.aws/credentials"}))
        with _read():
            out = await resolve_vault_delivery([v], _settings())
        assert out.values == {"AWS_CREDS": "/home/runner/.aws/credentials"}
        assert out.files[0]["name"] == "~/.aws/credentials"

    @pytest.mark.asyncio
    async def test_an_ordinary_vault_variable_beside_it_is_unchanged(self):
        vs = [_Var("TOKEN", _ref()), _Var("F", _ref(file={}))]
        with _read():
            out = await resolve_vault_delivery(vs, _settings())
        assert out.values["TOKEN"] == SECRET
        assert out.values["F"] == "/var/run/terrapod/files/F"
        assert [f["key"] for f in out.files] == ["F"]

    @pytest.mark.asyncio
    async def test_the_values_only_api_never_returns_a_files_content(self):
        """A caller that knows nothing about `vault-files` still cannot put the
        content into env or tfvars — the fail-safe for older code paths."""
        with _read():
            out = await resolve_vault_variables([_Var("F", _ref(file={}))], _settings())
        assert out == {"F": "/var/run/terrapod/files/F"}
        assert SECRET not in json.dumps(out)

    @pytest.mark.asyncio
    async def test_a_json_document_is_delivered_verbatim_as_the_file(self):
        # A fixture shaped like a GCP service-account file, which is what this
        # delivery path is for. No credential.
        # nosemgrep: generic.secrets.security.detected-google-gcm-service-account.detected-google-gcm-service-account
        doc = json.dumps({"type": "service_account", "private_key": "-----BEGIN"})
        with _read(doc):
            out = await resolve_vault_delivery([_Var("F", _ref(file={}))], _settings())
        assert out.files[0]["value"] == doc

    @pytest.mark.asyncio
    async def test_a_map_field_arrives_in_the_file_as_json(self):
        """End to end with the client's encoding: an object stored in Vault is
        a parseable JSON file, not a Python repr."""
        mock = AsyncMock(
            # nosemgrep: generic.secrets.security.detected-google-gcm-service-account.detected-google-gcm-service-account
            return_value=VaultResponse({"sa": {"type": "service_account", "ok": True}})
        )
        with patch.object(vss, "read_secret_response", new=mock):
            out = await resolve_vault_delivery([_Var("F", _ref(file={}))], _settings())
        # nosemgrep: generic.secrets.security.detected-google-gcm-service-account.detected-google-gcm-service-account
        assert json.loads(out.files[0]["value"]) == {"type": "service_account", "ok": True}

    @pytest.mark.asyncio
    async def test_a_dynamic_engine_is_passed_through_for_a_file(self):
        mock = AsyncMock(return_value=VaultResponse({"sa": SECRET}))
        v = _Var("DB", _ref(engine="dynamic", mount="database", path="creds/ro", file={}))
        with patch.object(vss, "read_secret_response", new=mock):
            out = await resolve_vault_delivery([v], _settings())
        assert mock.await_args.kwargs["engine"] == "dynamic"
        assert out.values == {"DB": "/var/run/terrapod/files/DB"}

    @pytest.mark.asyncio
    async def test_no_vault_variables_means_no_files(self):
        out = await resolve_vault_delivery([_Var("P", "x", value_source="static")], _settings())
        assert out.values == {} and out.files == []


class TestClaimTimeRefusals:
    """Everything refused on write is refused again when a run is claimed: a row
    can predate the rule, come from a restore, or be edited in the database."""

    @pytest.mark.asyncio
    async def test_two_variables_at_one_path_fail_before_any_vault_read(self):
        mock = AsyncMock(return_value=VaultResponse({"sa": SECRET}))
        vs = [
            _Var("A", _ref(file={"name": "gcp/adc.json"})),
            _Var("B", _ref(file={"name": "gcp/adc.json"})),
        ]
        with (
            patch.object(vss, "read_secret_response", new=mock),
            pytest.raises(VaultSourceError) as e,
        ):
            await resolve_vault_delivery(vs, _settings())
        assert str(e.value) == (
            "variables 'A' and 'B' both deliver an OpenBao/Vault file to "
            "/var/run/terrapod/files/gcp/adc.json"
        )
        # A run that is going to fail must not mint dynamic credentials first.
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_file_where_another_needs_a_directory_fails(self):
        vs = [_Var("A", _ref(file={"name": "a"})), _Var("B", _ref(file={"name": "a/b"}))]
        with _read(), pytest.raises(VaultSourceError, match="needs as a directory"):
            await resolve_vault_delivery(vs, _settings())

    @pytest.mark.asyncio
    async def test_a_defaulted_name_can_collide_with_an_explicit_one(self):
        vs = [_Var("creds", _ref(file={})), _Var("OTHER", _ref(file={"name": "creds"}))]
        with _read(), pytest.raises(VaultSourceError, match="both deliver an OpenBao/Vault file"):
            await resolve_vault_delivery(vs, _settings())

    @pytest.mark.asyncio
    async def test_the_same_name_under_home_and_files_is_not_a_collision(self):
        vs = [_Var("A", _ref(file={"name": "x/y"})), _Var("B", _ref(file={"name": "~/x/y"}))]
        with _read():
            out = await resolve_vault_delivery(vs, _settings())
        assert out.values == {"A": "/var/run/terrapod/files/x/y", "B": "/home/runner/x/y"}

    @pytest.mark.asyncio
    async def test_hcl_with_a_file_fails_the_run(self):
        v = _Var("F", _ref(file={}), category="terraform", hcl=True)
        with _read(), pytest.raises(VaultSourceError) as e:
            await resolve_vault_delivery([v], _settings())
        assert str(e.value) == (
            "variable 'F' uses vault file delivery with hcl enabled; its value is the "
            "file's path, which is not an HCL expression"
        )

    @pytest.mark.asyncio
    async def test_an_invalid_stored_name_fails_the_run(self):
        v = _Var("F", _ref(file={"name": "~/.ssh/id_rsa"}))
        with _read(), pytest.raises(VaultSourceError, match="which the runner manages itself"):
            await resolve_vault_delivery([v], _settings())

    @pytest.mark.asyncio
    async def test_a_stored_reserved_key_fails_the_run(self):
        v = _Var("F", _ref(file={"mode": "0600"}))
        with _read(), pytest.raises(VaultSourceError, match="reserved for a later release"):
            await resolve_vault_delivery([v], _settings())


class TestSizeCap:
    @pytest.mark.asyncio
    async def test_exactly_256_kib_is_accepted(self):
        value = "x" * (256 * 1024)
        with _read(value):
            out = await resolve_vault_delivery([_Var("F", _ref(file={}))], _settings())
        assert out.files[0]["value"] == value

    @pytest.mark.asyncio
    async def test_one_byte_over_fails_with_a_message_carrying_no_content(self):
        value = "Q" * (256 * 1024 + 1)
        with _read(value), pytest.raises(VaultSourceError) as e:
            await resolve_vault_delivery([_Var("F", _ref(file={}))], _settings())
        assert str(e.value) == (
            "variable 'F': the OpenBao/Vault value is 262145 bytes, over the 256 KiB limit for a file"
        )
        assert "QQ" not in str(e.value)

    @pytest.mark.asyncio
    async def test_the_cap_counts_bytes_not_characters(self):
        value = "é" * (128 * 1024) + "é"  # 2 bytes each: 262146 bytes
        with _read(value), pytest.raises(VaultSourceError, match="262146 bytes"):
            await resolve_vault_delivery([_Var("F", _ref(file={}))], _settings())

    @pytest.mark.asyncio
    async def test_the_cap_does_not_apply_to_an_ordinary_variable(self):
        value = "x" * (256 * 1024 + 1)
        with _read(value):
            out = await resolve_vault_delivery([_Var("T", _ref())], _settings())
        assert out.values["T"] == value


class TestFailureSemanticsInFileMode:
    @pytest.mark.asyncio
    async def test_a_4xx_errors_the_run(self):
        mock = AsyncMock(side_effect=VaultError("Vault denied 'kvv2/apps/gcp'"))
        with (
            patch.object(vss, "read_secret_response", new=mock),
            pytest.raises(VaultSourceError) as e,
        ):
            await resolve_vault_delivery([_Var("F", _ref(file={}))], _settings())
        assert not isinstance(e.value, VaultTransient)
        assert str(e.value) == "variable 'F': Vault denied 'kvv2/apps/gcp'"

    @pytest.mark.asyncio
    async def test_an_unreachable_or_5xx_vault_requeues(self):
        mock = AsyncMock(side_effect=VaultUnavailable("HTTP 503"))
        with patch.object(vss, "read_secret_response", new=mock), pytest.raises(VaultTransient):
            await resolve_vault_delivery([_Var("F", _ref(file={}))], _settings())

    @pytest.mark.asyncio
    async def test_a_missing_field_errors_the_run_naming_what_is_there(self):
        mock = AsyncMock(return_value=VaultResponse({"other": SECRET}))
        with (
            patch.object(vss, "read_secret_response", new=mock),
            pytest.raises(VaultSourceError) as e,
        ):
            await resolve_vault_delivery([_Var("F", _ref(file={}))], _settings())
        assert str(e.value) == (
            "variable 'F': field 'sa' is not present at 'kvv2/apps/gcp' (available: other)"
        )
        assert SECRET not in str(e.value)


# ── One read per secret ───────────────────────────────────────────────


def _pki(field: str, **kw) -> str:
    base = {
        "engine": "dynamic",
        "method": "POST",
        "mount": "pki",
        "path": "issue/web",
        "data": {"common_name": "a.example.test", "ttl": "1h"},
        "field": field,
    }
    base.update(kw)
    return _ref(**base)


class TestOneReadPerSecret:
    @pytest.mark.asyncio
    async def test_two_variables_on_one_dynamic_secret_share_one_read(self):
        """The case that motivated it: a certificate and its key must come from
        one issue. The body is equal after key order is canonicalised."""
        mock = AsyncMock(
            return_value=VaultResponse({"certificate": "CERT-1", "private_key": "KEY-1"})
        )
        vs = [
            _Var("TLS_CERT", _pki("certificate")),
            _Var(
                "TLS_KEY", _pki("private_key", data={"ttl": "1h", "common_name": "a.example.test"})
            ),
        ]
        with patch.object(vss, "read_secret_response", new=mock):
            out = await resolve_vault_delivery(vs, _settings())
        assert mock.await_count == 1
        assert out.values == {"TLS_CERT": "CERT-1", "TLS_KEY": "KEY-1"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "other",
        [
            _pki("private_key", path="issue/api"),  # different path
            _pki("private_key", mount="pki-int"),  # different mount
            _pki("private_key", method="GET"),  # different method
            _pki("private_key", data={"common_name": "b.example.test", "ttl": "1h"}),
            _pki("private_key", vault="second"),  # different instance
            _ref(engine="kv2", mount="pki", path="issue/web", field="private_key"),  # engine
        ],
    )
    async def test_different_requests_are_not_merged(self, other):
        mock = AsyncMock(return_value=VaultResponse({"certificate": "C", "private_key": "K"}))
        insts = [
            {"name": "default", "address": "https://v", "default": True},
            {"name": "second", "address": "https://w"},
        ]
        vs = [_Var("A", _pki("certificate")), _Var("B", other)]
        with patch.object(vss, "read_secret_response", new=mock):
            await resolve_vault_delivery(vs, _settings(insts))
        assert mock.await_count == 2

    @pytest.mark.asyncio
    async def test_a_kv2_secret_is_read_once_for_several_fields(self):
        mock = AsyncMock(return_value=VaultResponse({"user": "u", "pass": "p"}))
        vs = [_Var("U", _ref(field="user")), _Var("P", _ref(field="pass"))]
        with patch.object(vss, "read_secret_response", new=mock):
            out = await resolve_vault_delivery(vs, _settings())
        assert mock.await_count == 1
        assert out.values == {"U": "u", "P": "p"}

    @pytest.mark.asyncio
    async def test_kv2_ignores_method_and_data_so_they_cannot_split_a_read(self):
        """The client sends a kv-v2 read as a plain GET whatever the reference
        says, so those differences are not different requests."""
        mock = AsyncMock(return_value=VaultResponse({"user": "u", "pass": "p"}))
        vs = [
            _Var("U", _ref(field="user")),
            _Var("P", _ref(field="pass", method="POST", data={"x": 1})),
        ]
        with patch.object(vss, "read_secret_response", new=mock):
            await resolve_vault_delivery(vs, _settings())
        assert mock.await_count == 1

    @pytest.mark.asyncio
    async def test_a_get_never_sends_data_so_data_cannot_split_it(self):
        mock = AsyncMock(return_value=VaultResponse({"access_key": "AK", "secret_key": "SK"}))
        base = {"engine": "dynamic", "mount": "aws", "path": "creds/deploy"}
        vs = [
            _Var("AK", _ref(**base, field="access_key")),
            _Var("SK", _ref(**base, field="secret_key", data={"ignored": True})),
        ]
        with patch.object(vss, "read_secret_response", new=mock):
            await resolve_vault_delivery(vs, _settings())
        assert mock.await_count == 1

    @pytest.mark.asyncio
    async def test_slashes_around_mount_and_path_cannot_split_a_read(self):
        mock = AsyncMock(return_value=VaultResponse({"user": "u", "pass": "p"}))
        vs = [
            _Var("U", _ref(field="user")),
            _Var("P", _ref(field="pass", mount="/kvv2/", path="/apps/gcp/")),
        ]
        with patch.object(vss, "read_secret_response", new=mock):
            await resolve_vault_delivery(vs, _settings())
        assert mock.await_count == 1

    @pytest.mark.asyncio
    async def test_a_file_variable_and_an_env_variable_share_the_read(self):
        mock = AsyncMock(
            return_value=VaultResponse({"certificate": "CERT-1", "private_key": "KEY-1"})
        )
        vs = [
            _Var("TLS_CERT", _pki("certificate")),
            _Var("TLS_KEY_FILE", _pki("private_key", file={"name": "tls/key.pem"})),
        ]
        with patch.object(vss, "read_secret_response", new=mock):
            out = await resolve_vault_delivery(vs, _settings())
        assert mock.await_count == 1
        assert out.values == {
            "TLS_CERT": "CERT-1",
            "TLS_KEY_FILE": "/var/run/terrapod/files/tls/key.pem",
        }
        assert out.files == [{"key": "TLS_KEY_FILE", "name": "tls/key.pem", "value": "KEY-1"}]

    @pytest.mark.asyncio
    async def test_a_failed_shared_read_errors_every_variable_on_it(self):
        mock = AsyncMock(side_effect=VaultError("Vault denied 'pki/issue/web'"))
        vs = [_Var("TLS_CERT", _pki("certificate")), _Var("TLS_KEY", _pki("private_key"))]
        with (
            patch.object(vss, "read_secret_response", new=mock),
            pytest.raises(VaultSourceError) as e,
        ):
            await resolve_vault_delivery(vs, _settings())
        assert not isinstance(e.value, VaultTransient)
        assert str(e.value) == "variables 'TLS_CERT', 'TLS_KEY': Vault denied 'pki/issue/web'"
        assert mock.await_count == 1

    @pytest.mark.asyncio
    async def test_an_unavailable_shared_read_requeues_naming_every_variable(self):
        mock = AsyncMock(side_effect=VaultUnavailable("HTTP 503"))
        vs = [_Var("TLS_CERT", _pki("certificate")), _Var("TLS_KEY", _pki("private_key"))]
        with (
            patch.object(vss, "read_secret_response", new=mock),
            pytest.raises(VaultTransient) as e,
        ):
            await resolve_vault_delivery(vs, _settings())
        assert str(e.value) == "variables 'TLS_CERT', 'TLS_KEY': HTTP 503"

    @pytest.mark.asyncio
    async def test_a_field_missing_from_the_shared_response_names_only_its_variable(self):
        mock = AsyncMock(return_value=VaultResponse({"certificate": "C"}))
        vs = [_Var("TLS_CERT", _pki("certificate")), _Var("TLS_KEY", _pki("private_key"))]
        with (
            patch.object(vss, "read_secret_response", new=mock),
            pytest.raises(VaultSourceError) as e,
        ):
            await resolve_vault_delivery(vs, _settings())
        assert str(e.value).startswith("variable 'TLS_KEY': field 'private_key' is not present")

    @pytest.mark.asyncio
    async def test_exactly_one_http_request_reaches_vault(self, monkeypatch):
        """Through the real client, so the count is of requests Vault would
        see — each of which would mint a separate dynamic credential."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"data": {"access_key": "AK-1", "secret_key": "SK-1"}})

        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            vault_client.httpx,
            "AsyncClient",
            lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
        )
        monkeypatch.setenv("TERRAPOD_VAULT_DEFAULT_SECRET", "static-token")
        vault_client.reset_token_cache()
        insts = [
            {
                "name": "default",
                "address": "https://vault.test:8200",
                "auth": {"method": "token", "mount": "token", "role": "n/a"},
            }
        ]
        base = {"engine": "dynamic", "mount": "aws", "path": "creds/deploy"}
        vs = [
            _Var("AWS_ACCESS_KEY_ID", _ref(**base, field="access_key")),
            _Var("AWS_SECRET_ACCESS_KEY", _ref(**base, field="secret_key")),
            _Var("AWS_KEY_FILE", _ref(**base, field="secret_key", file={"name": "aws/sk"})),
        ]
        out = await resolve_vault_delivery(vs, _settings(insts))
        vault_client.reset_token_cache()
        assert [r.url.path for r in seen] == ["/v1/aws/creds/deploy"]
        assert out.values["AWS_ACCESS_KEY_ID"] == "AK-1"
        assert out.values["AWS_SECRET_ACCESS_KEY"] == "SK-1"
        assert out.files[0]["value"] == "SK-1"


class TestNothingLeaksIntoLogs:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "side_effect",
        [
            None,  # success
            VaultError("denied"),
            VaultUnavailable("503"),
        ],
    )
    async def test_the_resolver_never_logs_a_value(self, side_effect):
        mock = AsyncMock(return_value=VaultResponse({"sa": SECRET}), side_effect=side_effect)
        with (
            patch.object(vss, "read_secret_response", new=mock),
            patch.object(vss, "logger") as log,
        ):
            try:
                await resolve_vault_delivery([_Var("F", _ref(file={}))], _settings())
            except VaultSourceError:
                pass
        assert SECRET not in str(log.mock_calls)

    @pytest.mark.asyncio
    async def test_an_oversized_value_is_not_logged_either(self):
        with _read(SECRET * 10000), patch.object(vss, "logger") as log:
            with pytest.raises(VaultSourceError):
                await resolve_vault_delivery([_Var("F", _ref(file={}))], _settings())
        assert SECRET not in str(log.mock_calls)
