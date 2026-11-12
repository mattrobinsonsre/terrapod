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
        rules = {r["if"]["properties"]["backend"]["const"]: r["then"] for r in _location()["allOf"]}

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
]


@pytest.mark.parametrize("body", _BODIES)
def test_the_schema_and_the_model_accept_the_same_locations(body):
    schema_ok = jsonschema.Draft7Validator(_location()).is_valid(body)
    try:
        StorageLocationConfig.model_validate(body)
        model_ok = True
    except ValidationError:
        model_ok = False

    assert schema_ok == model_ok, body
