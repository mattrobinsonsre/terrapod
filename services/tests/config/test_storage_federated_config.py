"""The refusals a federated storage location is checked against at startup (#2590).

Four of these rules are also in `values.schema.json`, so `helm lint` catches them
first -- that half is asserted in `tests/helm/test_storage_location_schema_sync.py`,
which holds the schema and the model to the same answers. What is here is the half
only the model can decide:

* the presigned-expiry comparison, which JSON Schema cannot express because it
  relates two values;
* the two cross-block rules, which read the issuer's `enabled` and the whole
  deployment's audience catalogue.

Every one raises rather than warns, and the bar for that is deliberate: `settings =
Settings()` is evaluated at import, so a raise is a pod that will not start. Each
refusal below is a configuration that cannot work AT ALL. The things that can work
and merely might not -- the cloud unable to fetch the issuer, STS refusing the
exchange -- are left to the per-location reachability probe, which records them
against the location and leaves the pod ready (#2621).
"""

import pytest
from pydantic import ValidationError

from terrapod.config import Settings, StorageLocationConfig


def _s3(**creds) -> dict:
    body = {
        "type": "federated",
        "audience": "terrapod-storage-eu",
        "aws_role_arn": "arn:aws:iam::1:role/r",
    }
    body.update(creds)
    return {
        "backend": "s3",
        "s3": {"bucket": "b", "presigned_url_expiry_seconds": 900},
        "credentials": body,
    }


def _settings(locations: dict, **kw) -> Settings:
    base = {
        "storage": {"backend": "filesystem", "locations": locations},
        "auth": {"oidc_issuer": {"enabled": True, "public_url": "https://tp.example.com"}},
    }
    base.update(kw)
    return Settings(**base)


class TestAPresignedURLMustFitInsideTheSession:
    """An S3 presigned URL is signed WITH the session credentials, so it stops
    working when they expire however long its own expiry says -- a link that fails
    partway through a download, which an operator cannot diagnose from the symptom.
    """

    def test_an_expiry_longer_than_the_session_is_refused_on_s3(self):
        body = {
            "backend": "s3",
            "s3": {"bucket": "b", "presigned_url_expiry_seconds": 3600},
            "credentials": {
                "type": "federated",
                "audience": "a",
                "aws_role_arn": "arn:x",
                "session_duration_seconds": 1800,
            },
        }

        with pytest.raises(ValidationError, match="signed with the session credentials"):
            StorageLocationConfig.model_validate(body)

    def test_an_expiry_inside_the_session_is_accepted(self):
        assert (
            StorageLocationConfig.model_validate(
                _s3(session_duration_seconds=1800)
            ).credentials.session_duration_seconds
            == 1800
        )

    @pytest.mark.parametrize(
        "body",
        [
            {
                "backend": "azure",
                "azure": {
                    "account_name": "n",
                    "container_name": "c",
                    "presigned_url_expiry_seconds": 43200,
                },
                "credentials": {
                    "type": "federated",
                    "audience": "api://a",
                    "azure_tenant_id": "t",
                    "azure_client_id": "c",
                },
            },
            {
                "backend": "gcs",
                "gcs": {
                    "bucket": "b",
                    "service_account_email": "s@p.iam.gserviceaccount.com",
                    "presigned_url_expiry_seconds": 43200,
                },
                "credentials": {"type": "federated", "audience": "//iam/x"},
            },
        ],
        ids=["azure", "gcs"],
    )
    def test_the_other_two_clouds_have_no_such_bound(self, body):
        """Measured, not assumed, and the asymmetry is the whole of the rule.

        Azure signs a SAS with a user delegation key cached for ~23 hours and the
        SAS is redeemed against that key rather than against the AAD token; Google
        signs through IAM `signBlob` and the V4 URL is valid for its stated expiry
        regardless of the access token that signed it. So on those two a presigned
        URL outliving the hour-long session is ordinary -- and refusing it would be
        a pod that will not start on a working configuration.
        """
        assert StorageLocationConfig.model_validate(body).credentials.type == "federated"


class TestFederationNeedsTheIssuer:
    def test_a_federated_location_with_the_issuer_disabled_is_refused(self):
        with pytest.raises(ValidationError, match="auth.oidc_issuer.enabled is false"):
            Settings(storage={"backend": "filesystem", "locations": {"eu": _s3()}})

    def test_it_names_every_offending_location(self):
        """Named rather than counted: an operator with six locations should not have
        to find which two are federated."""
        with pytest.raises(ValidationError, match="eu, us"):
            Settings(
                storage={
                    "backend": "filesystem",
                    "locations": {
                        "eu": _s3(audience="a-eu"),
                        "us": {
                            **_s3(audience="a-us"),
                            "s3": {"bucket": "c", "presigned_url_expiry_seconds": 900},
                        },
                    },
                }
            )

    def test_an_unfederated_location_does_not_require_the_issuer(self):
        settings = Settings(
            storage={
                "backend": "filesystem",
                "locations": {"eu": {"backend": "s3", "s3": {"bucket": "b"}}},
            }
        )

        assert settings.auth.oidc_issuer.enabled is False


class TestTheAudienceIsTheDiscriminator:
    """The security property, enforced in configuration rather than documented.

    A run identity token and a storage token come from the same issuer and are
    signed by the same key, so `aud` is what stops one being presented where the
    other belongs. Their subjects differ too, but a cloud trust policy conditioning
    on audience alone is both legal and common -- so a shared audience is a
    configuration in which one of those policies is wrong, and it is refused.
    """

    def test_an_audience_shared_with_a_run_is_refused(self):
        with pytest.raises(ValidationError, match="also an audience in auth.oidc_issuer.audiences"):
            _settings(
                {"eu": _s3()},
                auth={
                    "oidc_issuer": {
                        "enabled": True,
                        "public_url": "https://tp.example.com",
                        "audiences": {"aws": ["sts.amazonaws.com", "terrapod-storage-eu"]},
                    }
                },
            )

    def test_two_locations_sharing_an_audience_are_refused(self):
        with pytest.raises(ValidationError, match="share credentials.audience"):
            _settings(
                {
                    "eu": _s3(),
                    "us": {
                        **_s3(),
                        "s3": {"bucket": "c", "presigned_url_expiry_seconds": 900},
                    },
                }
            )

    def test_distinct_audiences_are_accepted(self):
        settings = _settings(
            {
                "eu": _s3(audience="terrapod-storage-eu"),
                "us": {
                    **_s3(audience="terrapod-storage-us"),
                    "s3": {"bucket": "c", "presigned_url_expiry_seconds": 900},
                },
            },
            auth={
                "oidc_issuer": {
                    "enabled": True,
                    "public_url": "https://tp.example.com",
                    "audiences": {"aws": ["sts.amazonaws.com"]},
                }
            },
        )

        audiences = {n: loc.credentials.audience for n, loc in settings.storage.locations.items()}
        assert audiences == {"eu": "terrapod-storage-eu", "us": "terrapod-storage-us"}
