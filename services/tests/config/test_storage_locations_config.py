"""Named storage locations in `storage.locations` (#2547); none configured = today's behaviour."""

import pytest
from pydantic import ValidationError

from terrapod.config import Settings, StorageBackend, StorageConfig, StorageLocationConfig


def _two_locations() -> dict:
    return {
        "backend": "s3",
        "s3": {"bucket": "main"},
        "locations": {
            "eu-west": {
                "backend": "s3",
                "s3": {"bucket": "eu", "region": "eu-west-1", "presigned_url_expiry_seconds": 600},
            },
            "archive": {
                "backend": "gcs",
                "gcs": {"bucket": "arch", "project_id": "p"},
                "credentials": {"type": "default"},
            },
        },
    }


class TestLocationsParse:
    def test_no_locations_is_the_default(self):
        assert StorageConfig().locations == {}
        assert Settings().storage.locations == {}

    def test_two_locations_parse(self):
        config = StorageConfig.model_validate(_two_locations())

        assert set(config.locations) == {"eu-west", "archive"}
        eu = config.locations["eu-west"]
        assert isinstance(eu, StorageLocationConfig)
        assert eu.backend == StorageBackend.S3
        assert eu.s3.bucket == "eu"
        assert eu.s3.presigned_url_expiry_seconds == 600
        assert config.locations["archive"].gcs.project_id == "p"
        assert config.s3.bucket == "main"

    def test_a_location_has_its_own_expiry_default(self):
        config = StorageConfig.model_validate(
            {
                "locations": {
                    "a": {"backend": "azure", "azure": {"account_name": "n", "container_name": "c"}}
                }
            }
        )

        assert config.locations["a"].azure.presigned_url_expiry_seconds == 3600

    def test_credentials_default_to_the_default_type(self):
        config = StorageConfig.model_validate(
            {"locations": {"a": {"backend": "s3", "s3": {"bucket": "b"}}}}
        )

        assert config.locations["a"].credentials.type == "default"

    def test_locations_parse_through_settings(self):
        settings = Settings.model_validate({"storage": _two_locations()})

        assert settings.storage.locations["eu-west"].s3.region == "eu-west-1"


class TestLocationsAreRefused:
    @pytest.mark.parametrize("name", ["a" * 32, "a", "eu-west-1", "0", "a-b-c"])
    def test_valid_names_are_accepted(self, name):
        config = StorageConfig.model_validate(
            {"locations": {name: {"backend": "s3", "s3": {"bucket": "b"}}}}
        )

        assert name in config.locations

    def test_the_name_default_is_reserved(self):
        with pytest.raises(ValidationError, match="default"):
            StorageConfig.model_validate(
                {"locations": {"default": {"backend": "s3", "s3": {"bucket": "b"}}}}
            )

    @pytest.mark.parametrize("name", ["Upper", "under_score", "", "a" * 33, "dot.ted", "sp ace"])
    def test_an_invalid_name_is_refused(self, name):
        with pytest.raises(ValidationError, match="location name"):
            StorageConfig.model_validate(
                {"locations": {name: {"backend": "s3", "s3": {"bucket": "b"}}}}
            )

    def test_a_filesystem_location_is_refused(self):
        with pytest.raises(ValidationError, match="filesystem"):
            StorageConfig.model_validate({"locations": {"local": {"backend": "filesystem"}}})

    def test_a_location_needs_a_backend(self):
        with pytest.raises(ValidationError, match="backend"):
            StorageConfig.model_validate({"locations": {"a": {"s3": {"bucket": "b"}}}})

    def test_a_credentials_type_other_than_default_is_refused(self):
        with pytest.raises(ValidationError, match="credentials"):
            StorageConfig.model_validate(
                {
                    "locations": {
                        "a": {
                            "backend": "s3",
                            "s3": {"bucket": "b"},
                            "credentials": {"type": "assume-role"},
                        }
                    }
                }
            )


def _loc(**body) -> dict:
    return {"locations": {"a": body}}


class TestALocationsBackendBlockIsValidated:
    @pytest.mark.parametrize(
        "body",
        [
            {"backend": "s3", "s3": {"bucket": "b"}},
            {"backend": "azure", "azure": {"account_name": "n", "container_name": "c"}},
            {"backend": "gcs", "gcs": {"bucket": "b"}},
        ],
    )
    def test_a_complete_block_is_accepted(self, body):
        assert StorageConfig.model_validate(_loc(**body)).locations["a"].backend == body["backend"]

    @pytest.mark.parametrize(
        ("body", "missing"),
        [
            ({"backend": "s3"}, "s3.bucket"),
            ({"backend": "s3", "s3": {"region": "eu-west-1"}}, "s3.bucket"),
            ({"backend": "gcs", "gcs": {"bucket": ""}}, "gcs.bucket"),
            ({"backend": "azure", "azure": {"container_name": "c"}}, "azure.account_name"),
            ({"backend": "azure", "azure": {"account_name": "n"}}, "azure.container_name"),
        ],
    )
    def test_a_missing_required_field_is_refused(self, body, missing):
        with pytest.raises(ValidationError, match=missing):
            StorageConfig.model_validate(_loc(**body))

    def test_the_wrong_block_is_refused(self):
        with pytest.raises(ValidationError, match="must not set s3"):
            StorageConfig.model_validate(_loc(backend="azure", s3={"bucket": "b"}))

    def test_an_extra_block_beside_the_right_one_is_refused(self):
        body = {"backend": "s3", "s3": {"bucket": "b"}, "gcs": {"bucket": "g"}}
        with pytest.raises(ValidationError, match="must not set gcs"):
            StorageConfig.model_validate(_loc(**body))


class TestUnknownKeysAreRefused:
    def test_an_unknown_location_key_is_refused(self):
        with pytest.raises(ValidationError, match="role_arn"):
            StorageConfig.model_validate(_loc(backend="s3", s3={"bucket": "b"}, role_arn="x"))

    def test_an_unknown_credentials_key_is_refused(self):
        body = {"backend": "s3", "s3": {"bucket": "b"}, "credentials": {"role_arn": "x"}}
        with pytest.raises(ValidationError, match="role_arn"):
            StorageConfig.model_validate(_loc(**body))

    def test_the_shared_backend_blocks_keep_ignoring_unknown_keys(self):
        config = StorageConfig.model_validate({"s3": {"bucket": "b", "stray": 1}})
        assert config.s3.bucket == "b"


class TestTwoStoresCannotPointAtTheSamePlace:
    def test_a_location_equal_to_the_default_is_refused(self):
        body = {"backend": "s3", "s3": {"bucket": "main", "prefix": "/tp/"}}
        with pytest.raises(ValidationError, match="same place"):
            StorageConfig.model_validate(
                {"backend": "s3", "s3": {"bucket": "main", "prefix": "tp"}, **_loc(**body)}
            )

    def test_two_equal_locations_are_refused(self):
        azure = {"backend": "azure", "azure": {"account_name": "n", "container_name": "c"}}
        with pytest.raises(ValidationError, match="same place"):
            StorageConfig.model_validate({"locations": {"a": azure, "b": azure}})

    @pytest.mark.parametrize(
        "other",
        [
            {"bucket": "main", "prefix": "eu"},
            {"bucket": "main", "endpoint_url": "http://minio:9000"},
            {"bucket": "eu"},
        ],
    )
    def test_a_different_bucket_prefix_or_endpoint_is_accepted(self, other):
        config = StorageConfig.model_validate(
            {"backend": "s3", "s3": {"bucket": "main"}, **_loc(backend="s3", s3=other)}
        )
        assert "a" in config.locations

    def test_the_same_bucket_name_on_another_backend_is_accepted(self):
        config = StorageConfig.model_validate(
            {
                "backend": "s3",
                "s3": {"bucket": "main"},
                **_loc(backend="gcs", gcs={"bucket": "main"}),
            }
        )
        assert "a" in config.locations
