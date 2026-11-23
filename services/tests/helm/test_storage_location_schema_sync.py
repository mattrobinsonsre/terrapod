"""The `locations` schema in values.schema.json must match the config models (#2547).

Helm lints with the schema and startup validates with the models; each check fails if one side drifts.
"""

from __future__ import annotations

import json
import typing
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from terrapod.config import (
    _FEDERATED_FIELDS,
    _FEDERATED_REQUIRED,
    _FEDERATED_SESSION_CEILING,
    _LOCATION_NAME_RE,
    _LOCATION_REQUIRED_FIELDS,
    DEFAULT_STORAGE_LOCATION,
    AzureConfig,
    GCSConfig,
    S3Config,
    StorageBackend,
    StorageLocationConfig,
    StorageLocationCredentials,
)

_HELM_ROOT = Path("/app/helm/terrapod")
if not _HELM_ROOT.exists():  # local checkout fallback (running outside the image)
    _HELM_ROOT = Path(__file__).resolve().parents[3] / "helm" / "terrapod"


def _locations_schema() -> dict:
    schema = json.loads((_HELM_ROOT / "values.schema.json").read_text())
    return schema["$defs"]["apiConfig"]["properties"]["storage"]["properties"]["locations"]


def _location() -> dict:
    return _locations_schema()["additionalProperties"]


def _backend_rules() -> dict[str, dict]:
    """The location's `allOf` entries keyed on the backend they apply to.

    Filtered rather than taken wholesale: `allOf` also carries a rule that is not
    per-backend (an unfederated location must not set federated fields), and
    indexing every entry by `if.properties.backend.const` raised a bare `KeyError`
    the moment one arrived.
    """
    return {
        r["if"]["properties"]["backend"]["const"]: r["then"]
        for r in _location()["allOf"]
        if "backend" in r["if"].get("properties", {})
    }


def _federated_clause(then: dict) -> dict:
    """The `then` of the `credentials.type == federated` clause nested in a backend rule."""
    nested = then["allOf"]
    assert len(nested) == 1, nested
    assert nested[0]["if"]["properties"]["credentials"]["properties"]["type"] == {
        "const": "federated"
    }
    return nested[0]["then"]


class TestTheSchemaAndTheModelAgree:
    def test_the_name_pattern_is_the_models(self):
        names = _locations_schema()["propertyNames"]

        # The model uses fullmatch, so the schema's anchors are the only difference.
        assert names["pattern"] == f"^{_LOCATION_NAME_RE.pattern}$"
        assert names["not"] == {"const": DEFAULT_STORAGE_LOCATION}

    def test_the_backends_are_every_backend_but_filesystem(self):
        enum = _location()["properties"]["backend"]["enum"]

        assert sorted(enum) == sorted(b.value for b in StorageBackend if b != "filesystem")

    def test_a_location_has_exactly_the_models_fields(self):
        loc = _location()

        assert set(loc["properties"]) == set(StorageLocationConfig.model_fields)
        assert "filesystem" not in loc["properties"]
        assert loc["additionalProperties"] is False
        required = {n for n, f in StorageLocationConfig.model_fields.items() if f.is_required()}
        assert set(loc["required"]) == required

    def test_each_backend_block_has_exactly_the_models_fields(self):
        props = _location()["properties"]

        for name, model in (("s3", S3Config), ("azure", AzureConfig), ("gcs", GCSConfig)):
            assert set(props[name]["properties"]) == set(model.model_fields), name

    def test_credentials_type_enum_is_the_models_literal(self):
        creds = _location()["properties"]["credentials"]
        literal = StorageLocationCredentials.model_fields["type"].annotation

        assert creds["properties"]["type"]["enum"] == list(typing.get_args(literal))
        assert set(creds["properties"]) == set(StorageLocationCredentials.model_fields)

    def test_each_backend_requires_the_models_fields_and_no_other_block(self):
        rules = _backend_rules()

        assert sorted(rules) == sorted(_location()["properties"]["backend"]["enum"])
        for backend, then in rules.items():
            inner = then["properties"][backend]
            assert then["required"] == [backend]
            assert inner["required"] == list(_LOCATION_REQUIRED_FIELDS[StorageBackend(backend)])
            assert all(inner["properties"][f] == {"minLength": 1} for f in inner["required"])
            assert {c["required"][0] for c in then["not"]["anyOf"]} == set(rules) - {backend}


_BODIES = [
    {"backend": "s3", "s3": {"bucket": "b"}},
    {"backend": "azure", "azure": {"account_name": "n", "container_name": "c"}},
    {"backend": "gcs", "gcs": {"bucket": "b", "project_id": "p"}},
    {"backend": "s3"},
    {"backend": "s3", "s3": {"region": "eu-west-1"}},
    {"backend": "s3", "s3": {"bucket": ""}},
    {"backend": "azure", "azure": {"container_name": "c"}},
    {"backend": "azure", "s3": {"bucket": "b"}},
    {"backend": "s3", "s3": {"bucket": "b"}, "gcs": {"bucket": "g"}},
    {"backend": "s3", "s3": {"bucket": "b"}, "credentials": {"role_arn": "x"}},
    # Federated (#2590). Every rule below is one both sides can express; the
    # presigned-expiry-against-session comparison is model-only, so no body here
    # sets an expiry that exceeds its session.
    {
        "backend": "s3",
        "s3": {"bucket": "b", "presigned_url_expiry_seconds": 900},
        "credentials": {
            "type": "federated",
            "audience": "tp-eu",
            "aws_role_arn": "arn:aws:iam::1:role/r",
            "session_duration_seconds": 1800,
        },
    },
    {"backend": "s3", "s3": {"bucket": "b"}, "credentials": {"type": "federated"}},
    {
        "backend": "s3",
        "s3": {"bucket": "b"},
        "credentials": {"type": "federated", "aws_role_arn": "arn:x"},
    },
    {"backend": "s3", "s3": {"bucket": "b"}, "credentials": {"type": "federated", "audience": "a"}},
    {
        "backend": "s3",
        "s3": {"bucket": "b"},
        "credentials": {"type": "federated", "audience": "", "aws_role_arn": "arn:x"},
    },
    {
        "backend": "s3",
        "s3": {"bucket": "b"},
        "credentials": {
            "type": "federated",
            "audience": "a",
            "aws_role_arn": "arn:x",
            "azure_client_id": "c",
        },
    },
    {"backend": "s3", "s3": {"bucket": "b"}, "credentials": {"aws_role_arn": "arn:x"}},
    {"backend": "s3", "s3": {"bucket": "b"}, "credentials": {"type": "default", "audience": "a"}},
    {
        "backend": "azure",
        "azure": {"account_name": "n", "container_name": "c"},
        "credentials": {
            "type": "federated",
            "audience": "api://a",
            "azure_tenant_id": "t",
            "azure_client_id": "c",
        },
    },
    {
        "backend": "azure",
        "azure": {"account_name": "n", "container_name": "c"},
        "credentials": {"type": "federated", "audience": "api://a", "azure_tenant_id": "t"},
    },
    {
        "backend": "azure",
        "azure": {"account_name": "n", "container_name": "c"},
        "credentials": {
            "type": "federated",
            "audience": "api://a",
            "azure_tenant_id": "t",
            "azure_client_id": "c",
            "session_duration_seconds": 7200,
        },
    },
    {
        "backend": "gcs",
        "gcs": {"bucket": "b", "service_account_email": "s@p.iam.gserviceaccount.com"},
        "credentials": {"type": "federated", "audience": "//iam.googleapis.com/x"},
    },
    {
        "backend": "gcs",
        "gcs": {"bucket": "b"},
        "credentials": {"type": "federated", "audience": "//iam.googleapis.com/x"},
    },
]


class TestTheFederatedRulesAreTheModels:
    """`credentials.type: federated` adds rules, and the schema carries the ones it can (#2590).

    Four of the model's five are expressible in JSON Schema and are asserted here,
    derived from the model's own tables rather than from a copy of them. The fifth
    is NOT, and that is a documented gap rather than an oversight: `helm lint`
    cannot compare `s3.presigned_url_expiry_seconds` against
    `credentials.session_duration_seconds`, because JSON Schema has no way to relate
    two values. That one is refused by the model alone, so it is met at startup
    rather than at `helm lint` -- which is why no body below exercises it, and why
    `test_the_schema_and_the_model_accept_the_same_locations` would fail if one did.
    """

    def test_each_backend_requires_the_models_federated_credential_fields(self):
        for backend, then in _backend_rules().items():
            creds = _federated_clause(then)["properties"]["credentials"]
            # `audience` is required for every backend -- it is the discriminator --
            # and is NOT in the model's per-backend table, so it is named here.
            wanted = {"audience", *_FEDERATED_REQUIRED[StorageBackend(backend)]}

            assert set(creds["required"]) == wanted, backend
            assert all(creds["properties"][f] == {"minLength": 1} for f in wanted), backend

    def test_a_federated_gcs_location_needs_the_signing_service_account(self):
        # The one required field that lives on the BACKEND block rather than on
        # `credentials`: the exchange impersonates the account that signs this
        # location's URLs, and they have to be the same account.
        then = _federated_clause(_backend_rules()["gcs"])

        assert then["required"] == ["gcs"]
        assert then["properties"]["gcs"]["required"] == ["service_account_email"]

    def test_the_session_ceiling_is_the_models(self):
        for backend, then in _backend_rules().items():
            creds = _federated_clause(then)["properties"]["credentials"]
            ceiling = _FEDERATED_SESSION_CEILING[StorageBackend(backend)]
            schema_max = creds["properties"].get("session_duration_seconds", {}).get("maximum")

            # A ceiling equal to the field's own `le`-free upper bound is not
            # expressed, so the clouds that cap at an hour are, and s3 is not.
            if ceiling == 43200:
                assert schema_max is None, backend
            else:
                assert schema_max == ceiling, backend

    def test_each_backend_refuses_every_other_clouds_federated_fields(self):
        for backend, then in _backend_rules().items():
            creds = _federated_clause(then)["properties"]["credentials"]
            refused = {c["required"][0] for c in creds["not"]["anyOf"]}
            others = {
                f
                for other, fields in _FEDERATED_FIELDS.items()
                if other != StorageBackend(backend)
                for f in fields
            }

            assert refused == others, backend

    def test_an_unfederated_location_refuses_every_federated_field(self):
        # The one rule that is not per-backend: a role ARN on a `default` location
        # is something half-configured, so it is refused rather than ignored.
        stray = [
            r
            for r in _location()["allOf"]
            if r is not None and "backend" not in r["if"].get("properties", {})
        ]

        assert len(stray) == 1
        refused = {
            c["required"][0] for c in stray[0]["then"]["properties"]["credentials"]["not"]["anyOf"]
        }
        every = {f for fields in _FEDERATED_FIELDS.values() for f in fields} | {"audience"}

        assert refused == every


@pytest.mark.parametrize("body", _BODIES)
def test_the_schema_and_the_model_accept_the_same_locations(body):
    schema_ok = jsonschema.Draft7Validator(_location()).is_valid(body)
    try:
        StorageLocationConfig.model_validate(body)
        model_ok = True
    except ValidationError:
        model_ok = False

    assert schema_ok == model_ok, body
