"""The `storage-location` rule: `default` or a configured name, `default` stored as NULL."""

import pytest

from terrapod.config import S3Config, StorageBackend, StorageLocationConfig, settings
from terrapod.services.workspace_settings import validate_storage_location


@pytest.fixture(autouse=True)
def _locations(monkeypatch):
    eu = StorageLocationConfig(backend=StorageBackend.S3, s3=S3Config(bucket="eu-bucket"))
    monkeypatch.setattr(settings.storage, "locations", {"eu-west": eu})


def test_null_and_default_both_mean_the_default_location():
    assert validate_storage_location(None) is None
    assert validate_storage_location("default") is None


def test_a_configured_name_is_kept():
    assert validate_storage_location("eu-west") == "eu-west"


@pytest.mark.parametrize("raw", ["nowhere", "", "EU-WEST", " eu-west", 7, True, ["eu-west"]])
def test_anything_else_is_refused(raw):
    with pytest.raises(ValueError, match="storage-location"):
        validate_storage_location(raw)


def test_every_name_is_refused_when_none_are_configured(monkeypatch):
    monkeypatch.setattr(settings.storage, "locations", {})
    assert validate_storage_location("default") is None
    with pytest.raises(ValueError, match="storage-location"):
        validate_storage_location("eu-west")
