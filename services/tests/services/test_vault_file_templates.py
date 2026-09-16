"""Templated, whole-secret and base64 Vault files through the resolver (#1648).

`test_vault_render.py` pins the renderer itself; these drive
`resolve_vault_delivery` so the validation, the single shared read, the lease
plumbing and the caps are exercised together. The properties:

- `field`, `file.template` and `file.format` are mutually exclusive, and each
  shape error is refused when the reference is parsed (a 422 on write);
- several variables and a template over one secret make exactly one read;
- a template sees `_lease.*` but never the lease id;
- base64 decodes to text or fails the run clearly;
- the per-file cap applies after rendering, and the run-wide cap sums files;
- no error carries a value.
"""

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from terrapod.config import Settings, VaultConfig
from terrapod.runner.vault_files import MAX_FILE_BYTES, MAX_TOTAL_FILE_BYTES
from terrapod.services import vault_client
from terrapod.services import vault_source_service as vss
from terrapod.services.vault_client import VaultLease, VaultResponse
from terrapod.services.vault_source_service import (
    VaultSourceError,
    looks_like_file_reference,
    parse_reference,
    resolve_vault_delivery,
)

SECRET = "S3CR3T-template-value-must-not-leak"
LEASE_ID = "aws/creds/deploy/LEASE-ID-MUST-NOT-LEAK"


@dataclass
class _Var:
    key: str
    value: str
    category: str = "env"
    hcl: bool = False
    sensitive: bool = True
    value_source: str = "vault"


def _ref(**kw) -> str:
    base = {"source": "vault", "mount": "kvv2", "path": "apps/x"}
    base.update(kw)
    return json.dumps(base)


def _settings() -> Settings:
    s = Settings()
    s.vault = VaultConfig(enabled=True, instances=[{"name": "default", "address": "https://v"}])
    return s


def _mock(data: dict, lease: VaultLease | None = None) -> AsyncMock:
    return AsyncMock(return_value=VaultResponse(data=data, lease=lease))


async def _resolve(variables, mock):
    with patch.object(vss, "read_secret_response", new=mock):
        return await resolve_vault_delivery(variables, _settings())


# ── Parsing: what is refused on write ─────────────────────────────────


class TestParse:
    def test_a_template_needs_no_field(self):
        ref = parse_reference(_ref(file={"template": "{{ a }}"}), key="F")
        assert "field" not in ref

    def test_a_format_needs_no_field(self):
        parse_reference(_ref(file={"format": "json"}), key="F")

    def test_without_a_template_or_format_the_field_is_still_required(self):
        with pytest.raises(VaultSourceError, match="missing: field"):
            parse_reference(_ref(file={"name": "a"}), key="F")
        with pytest.raises(VaultSourceError, match="missing: field"):
            parse_reference(_ref(), key="F")

    @pytest.mark.parametrize(
        ("extra", "file", "named"),
        [
            ({"field": "a"}, {"template": "{{ a }}"}, "`field` and `file.template`"),
            ({"field": "a"}, {"format": "json"}, "`field` and `file.format`"),
            ({}, {"template": "{{ a }}", "format": "env"}, "`file.template` and `file.format`"),
            (
                {"field": "a"},
                {"template": "{{ a }}", "format": "env"},
                "`field` and `file.template` and `file.format`",
            ),
        ],
    )
    def test_field_template_and_format_are_mutually_exclusive(self, extra, file, named):
        with pytest.raises(VaultSourceError) as e:
            parse_reference(_ref(**extra, file=file), key="F")
        assert str(e.value).startswith(f"variable 'F': {named} cannot be combined")

    def test_a_template_syntax_error_is_refused_naming_the_variable(self):
        with pytest.raises(VaultSourceError) as e:
            parse_reference(_ref(file={"template": "{{ a | upper }}"}), key="F")
        assert str(e.value).startswith(
            "variable 'F': vault file template tag {{ a | upper }} uses unknown filter 'upper'"
        )

    def test_an_unclosed_tag_is_refused(self):
        with pytest.raises(VaultSourceError, match="has a '\\{\\{' with no closing"):
            parse_reference(_ref(file={"template": "[x]\n{{ a"}), key="F")

    def test_an_oversized_template_is_refused(self):
        with pytest.raises(VaultSourceError, match="over the 16 KiB limit for a template"):
            parse_reference(_ref(file={"template": "x" * (16 * 1024 + 1)}), key="F")

    def test_a_template_that_is_not_a_string_is_refused(self):
        with pytest.raises(VaultSourceError, match="template must be a string"):
            parse_reference(_ref(file={"template": ["{{ a }}"]}), key="F")

    def test_an_unknown_format_is_refused(self):
        with pytest.raises(VaultSourceError, match="format 'ini' is not supported"):
            parse_reference(_ref(file={"format": "ini"}), key="F")

    def test_fields_without_a_format_is_refused(self):
        with pytest.raises(VaultSourceError, match="`fields` selects keys for `file.format`"):
            parse_reference(_ref(field="a", file={"fields": ["a"]}), key="F")

    @pytest.mark.parametrize("fields", [[], "a", ["a", ""], ["a", 1], {"a": 1}])
    def test_fields_must_be_a_non_empty_list_of_names(self, fields):
        with pytest.raises(VaultSourceError, match="non-empty list of field names"):
            parse_reference(_ref(file={"format": "json", "fields": fields}), key="F")

    def test_fields_may_not_repeat(self):
        with pytest.raises(VaultSourceError, match="names a field twice"):
            parse_reference(_ref(file={"format": "env", "fields": ["a", "a"]}), key="F")

    def test_an_unknown_encoding_is_refused(self):
        with pytest.raises(VaultSourceError, match="encoding 'hex' is not supported"):
            parse_reference(_ref(field="a", file={"encoding": "hex"}), key="F")

    @pytest.mark.parametrize("file", [{"template": "{{ a }}"}, {"format": "json"}])
    def test_encoding_applies_to_a_single_field_only(self, file):
        with pytest.raises(VaultSourceError, match="`encoding` decodes the one `field`"):
            parse_reference(_ref(file={**file, "encoding": "base64"}), key="F")

    def test_a_templated_reference_with_no_field_is_still_detected_as_a_file_reference(self):
        """So `file` on a static source is still refused for the new shapes."""
        assert looks_like_file_reference(
            json.dumps({"mount": "m", "path": "p", "file": {"template": "{{ a }}"}})
        )


# ── One read ──────────────────────────────────────────────────────────


AWS_TEMPLATE = (
    "[default]\naws_access_key_id = {{ access_key }}\naws_secret_access_key = {{ secret_key }}\n"
)
AWS_BASE = {"engine": "dynamic", "mount": "aws", "path": "creds/deploy"}


class TestOneRead:
    @pytest.mark.asyncio
    async def test_three_variables_and_a_template_over_one_secret_make_one_read(self):
        mock = _mock({"access_key": "AK-1", "secret_key": "SK-1"})
        out = await _resolve(
            [
                _Var("AWS_ACCESS_KEY_ID", _ref(**AWS_BASE, field="access_key")),
                _Var("AWS_SECRET_ACCESS_KEY", _ref(**AWS_BASE, field="secret_key")),
                _Var("AWS_KEY_FILE", _ref(**AWS_BASE, field="secret_key", file={"name": "k"})),
                _Var(
                    "AWS_SHARED_CREDENTIALS_FILE",
                    _ref(**AWS_BASE, file={"name": "~/.aws/credentials", "template": AWS_TEMPLATE}),
                ),
            ],
            mock,
        )
        assert mock.await_count == 1
        assert out.values["AWS_ACCESS_KEY_ID"] == "AK-1"
        assert out.values["AWS_SECRET_ACCESS_KEY"] == "SK-1"
        assert out.values["AWS_SHARED_CREDENTIALS_FILE"] == "/home/runner/.aws/credentials"
        files = {f["key"]: f["value"] for f in out.files}
        assert files["AWS_SHARED_CREDENTIALS_FILE"] == (
            "[default]\naws_access_key_id = AK-1\naws_secret_access_key = SK-1\n"
        )
        assert files["AWS_KEY_FILE"] == "SK-1"

    @pytest.mark.asyncio
    async def test_the_count_is_of_real_http_requests(self, monkeypatch):
        """Through the real client: one request is one minted credential."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "lease_id": LEASE_ID,
                    "lease_duration": 900,
                    "renewable": True,
                    "data": {"access_key": "AK-2", "secret_key": "SK-2"},
                },
            )

        real = httpx.AsyncClient
        monkeypatch.setattr(
            vault_client.httpx,
            "AsyncClient",
            lambda *a, **kw: real(transport=httpx.MockTransport(handler)),
        )
        monkeypatch.setenv("TERRAPOD_VAULT_DEFAULT_SECRET", "static-token")
        vault_client.reset_token_cache()
        s = Settings()
        s.vault = VaultConfig(
            enabled=True,
            instances=[
                {
                    "name": "default",
                    "address": "https://vault.test:8200",
                    "auth": {"method": "token", "mount": "token", "role": "n/a"},
                }
            ],
        )
        variables = [
            _Var("AWS_ACCESS_KEY_ID", _ref(**AWS_BASE, field="access_key")),
            _Var("AWS_SECRET_ACCESS_KEY", _ref(**AWS_BASE, field="secret_key")),
            _Var("AWS_KEY_FILE", _ref(**AWS_BASE, field="secret_key", file={"name": "k"})),
            _Var(
                "CREDS",
                _ref(**AWS_BASE, file={"template": AWS_TEMPLATE + "# ttl {{ _lease.ttl }}\n"}),
            ),
        ]
        out = await resolve_vault_delivery(variables, s)
        vault_client.reset_token_cache()
        assert [r.url.path for r in seen] == ["/v1/aws/creds/deploy"]
        creds = next(f["value"] for f in out.files if f["key"] == "CREDS")
        assert "aws_access_key_id = AK-2" in creds and "# ttl 900" in creds
        assert LEASE_ID not in json.dumps({"values": out.values, "files": out.files})


# ── Lease metadata ────────────────────────────────────────────────────


def _lease() -> VaultLease:
    return VaultLease(
        duration=3600,
        renewable=True,
        received_at=datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC),
        lease_id=LEASE_ID,
    )


class TestLease:
    @pytest.mark.asyncio
    async def test_a_template_reads_the_lease_metadata(self):
        tpl = "{{ _lease.ttl }} {{ _lease.renewable }} {{ _lease.expires_at }}"
        out = await _resolve([_Var("F", _ref(file={"template": tpl}))], _mock({"a": "b"}, _lease()))
        assert out.files[0]["value"] == "3600 true 2026-09-15T13:00:00Z"

    def test_the_lease_id_cannot_be_named(self):
        with pytest.raises(VaultSourceError, match="_lease offers _lease.ttl"):
            parse_reference(_ref(file={"template": "{{ _lease.lease_id }}"}), key="F")

    @pytest.mark.asyncio
    async def test_the_lease_id_is_not_in_the_metadata_or_any_repr(self):
        lease = _lease()
        assert set(lease.template_metadata()) == {"ttl", "renewable", "expires_at"}
        assert LEASE_ID not in repr(lease)
        resp = VaultResponse(data={"k": SECRET}, lease=lease)
        assert LEASE_ID not in repr(resp) and SECRET not in repr(resp)

    @pytest.mark.asyncio
    async def test_a_lease_tag_on_a_secret_with_no_lease_fails_the_run(self):
        with pytest.raises(VaultSourceError) as e:
            await _resolve(
                [_Var("F", _ref(file={"template": "{{ _lease.ttl }}"}))], _mock({"a": SECRET})
            )
        assert str(e.value).startswith("variable 'F': tag {{ _lease.ttl }} reads the lease")


# ── Rendering through the resolver ────────────────────────────────────


class TestRendering:
    @pytest.mark.asyncio
    async def test_a_format_json_file_holds_the_whole_secret(self):
        data = {"type": "service_account", "project_id": "p", "private_key": SECRET}
        out = await _resolve([_Var("SA", _ref(file={"format": "json"}))], _mock(data))
        assert json.loads(out.files[0]["value"]) == data
        assert out.values["SA"] == "/var/run/terrapod/files/SA"

    @pytest.mark.asyncio
    async def test_a_format_env_file_with_a_fields_subset(self):
        data = {"DB_USER": "u", "DB_PASS": 'p"$', "OTHER": SECRET}
        out = await _resolve(
            [_Var("DB_ENV", _ref(file={"format": "env", "fields": ["DB_USER", "DB_PASS"]}))],
            _mock(data),
        )
        assert out.files[0]["value"] == 'DB_USER="u"\nDB_PASS="p\\"\\$"\n'

    @pytest.mark.asyncio
    async def test_a_value_containing_a_tag_is_not_expanded(self):
        data = {"a": "{{ b }}", "b": SECRET}
        out = await _resolve([_Var("F", _ref(file={"template": "<{{ a }}>"}))], _mock(data))
        assert out.files[0]["value"] == "<{{ b }}>"

    @pytest.mark.asyncio
    async def test_an_unknown_name_fails_the_run_naming_variable_and_tag_not_value(self):
        with pytest.raises(VaultSourceError) as e:
            await _resolve(
                [_Var("CREDS", _ref(file={"template": "{{ acess_key }}"}))],
                _mock({"access_key": SECRET}),
            )
        assert str(e.value) == (
            "variable 'CREDS': tag {{ acess_key }} names 'acess_key', which is not in the "
            "secret (available: access_key)"
        )
        assert SECRET not in str(e.value)


# ── base64 ─────────────────────────────────────────────────────────────


class TestBase64:
    @pytest.mark.asyncio
    async def test_a_base64_field_is_decoded_into_the_file(self):
        doc = json.dumps({"type": "service_account", "private_key": SECRET})
        enc = base64.b64encode(doc.encode()).decode()
        out = await _resolve(
            [_Var("GCP", _ref(field="private_key_data", file={"encoding": "base64"}))],
            _mock({"private_key_data": enc}),
        )
        assert out.files[0]["value"] == doc

    @pytest.mark.asyncio
    async def test_non_utf8_bytes_fail_the_run_as_binary(self):
        enc = base64.b64encode(b"\x00\xff\xfe" + SECRET.encode()).decode()
        with pytest.raises(VaultSourceError) as e:
            await _resolve(
                [_Var("GCP", _ref(field="k", file={"encoding": "base64"}))], _mock({"k": enc})
            )
        assert str(e.value) == (
            "variable 'GCP': field 'k' at 'kvv2/apps/x' decodes to bytes that are not UTF-8 "
            "text; binary files are not supported yet"
        )
        assert enc not in str(e.value)

    @pytest.mark.asyncio
    async def test_invalid_base64_fails_the_run(self):
        with pytest.raises(
            VaultSourceError, match="field 'k' at 'kvv2/apps/x' is not valid base64"
        ):
            await _resolve(
                [_Var("F", _ref(field="k", file={"encoding": "base64"}))], _mock({"k": SECRET})
            )

    @pytest.mark.asyncio
    async def test_a_map_field_cannot_be_base64_decoded(self):
        with pytest.raises(VaultSourceError, match="is not a string, so it cannot be"):
            await _resolve(
                [_Var("F", _ref(field="k", file={"encoding": "base64"}))], _mock({"k": {"a": 1}})
            )

    @pytest.mark.asyncio
    async def test_a_missing_field_with_encoding_names_what_is_there(self):
        with pytest.raises(VaultSourceError, match="field 'k' is not present"):
            await _resolve(
                [_Var("F", _ref(field="k", file={"encoding": "base64"}))], _mock({"j": "x"})
            )


# ── Caps ──────────────────────────────────────────────────────────────


class TestCaps:
    @pytest.mark.asyncio
    async def test_the_per_file_cap_applies_after_rendering(self):
        """A small template can still expand past the cap."""
        tpl = "{{ a }}" * 3
        with pytest.raises(VaultSourceError) as e:
            await _resolve(
                [_Var("F", _ref(file={"template": tpl}))],
                _mock({"a": "Z" * (MAX_FILE_BYTES // 3 + 1)}),
            )
        assert "over the 256 KiB limit for a file" in str(e.value)

    @pytest.mark.asyncio
    async def test_the_per_file_cap_applies_after_decoding(self):
        enc = base64.b64encode(b"Z" * (MAX_FILE_BYTES + 1)).decode()
        with pytest.raises(VaultSourceError, match="over the 256 KiB limit for a file"):
            await _resolve(
                [_Var("F", _ref(field="k", file={"encoding": "base64"}))], _mock({"k": enc})
            )

    def _three_full_files(self):
        return [_Var(f"F{i}", _ref(field=f"f{i}", file={"name": f"f{i}"})) for i in range(3)], {
            f"f{i}": "Z" * MAX_FILE_BYTES for i in range(3)
        }

    @pytest.mark.asyncio
    async def test_files_up_to_the_run_wide_cap_are_accepted(self):
        variables, data = self._three_full_files()
        assert 3 * MAX_FILE_BYTES == MAX_TOTAL_FILE_BYTES
        out = await _resolve(variables, _mock(data))
        assert sum(len(f["value"]) for f in out.files) == MAX_TOTAL_FILE_BYTES

    @pytest.mark.asyncio
    async def test_one_byte_over_the_run_wide_cap_fails_naming_the_variable(self):
        variables, data = self._three_full_files()
        variables.append(_Var("F3", _ref(field="f3", file={"name": "f3"})))
        data["f3"] = SECRET[:1]
        with pytest.raises(VaultSourceError) as e:
            await _resolve(variables, _mock(data))
        assert str(e.value) == (
            f"variable 'F3': the OpenBao/Vault files for this run come to {MAX_TOTAL_FILE_BYTES + 1} "
            "bytes with this one, over the 768 KiB limit for all files in a run"
        )

    @pytest.mark.asyncio
    async def test_the_run_wide_cap_sums_across_separate_reads(self):
        """Different secrets are different reads, and still one Secret."""
        variables = [
            _Var(f"F{i}", _ref(path=f"apps/{i}", field="v", file={"name": f"f{i}"}))
            for i in range(4)
        ]
        mock = _mock({"v": "Z" * MAX_FILE_BYTES})
        with pytest.raises(VaultSourceError, match="over the 768 KiB limit for all files"):
            await _resolve(variables, mock)
        assert mock.await_count == 4

    @pytest.mark.asyncio
    async def test_ordinary_variables_do_not_count_towards_the_file_cap(self):
        variables, data = self._three_full_files()
        variables.append(_Var("TOKEN", _ref(field="token")))
        data["token"] = "Z" * MAX_FILE_BYTES
        out = await _resolve(variables, _mock(data))
        assert out.values["TOKEN"] == "Z" * MAX_FILE_BYTES
