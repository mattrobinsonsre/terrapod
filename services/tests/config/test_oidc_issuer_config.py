"""`OIDCIssuerConfig` and the issuer-URL validator (#2046, #2051).

Every case here is a startup-time refusal. That is deliberate rather than a
warning or an auto-disable: only a deployment that has enabled the issuer can
reach any of it, so the operator is someone actively working on this feature and
a named failure at boot costs them a minute. The alternative is a deployment that
comes up, publishes a trust root no cloud will fetch, and fails at the cloud's
token exchange -- in the cloud, with nothing wrong on the Terrapod side to look
at.
"""

import pytest
from pydantic import ValidationError

from terrapod.config import OIDCIssuerConfig, Settings


def _settings(*, issuer=None, webhook="", external=""):
    return Settings(
        auth={"oidc_issuer": issuer if issuer is not None else {}},
        public_webhook_url=webhook,
        external_url=external,
    )


class TestTheIssuerURLIsValidatedAtStartup:
    def test_a_disabled_issuer_is_not_checked_at_all(self):
        """The overwhelmingly common case. An `external_url` that happens to be
        http is nobody's problem until the issuer is turned on, and refusing it
        would make this validator break deployments that do not use the
        feature."""
        _settings(issuer={"enabled": False, "public_url": "http://anything"})

    def test_an_absolute_https_url_is_accepted(self):
        _settings(issuer={"enabled": True, "public_url": "https://terrapod.example.com"})

    def test_an_http_issuer_is_refused(self):
        """`webhookIngress.tls=false` produces exactly this, and the chart
        renders it without complaint. AWS, Azure and GCP all refuse to fetch an
        http issuer's discovery document."""
        with pytest.raises(ValidationError, match="not an absolute https URL"):
            _settings(issuer={"enabled": True, "public_url": "http://terrapod.example.com"})

    def test_a_scheme_less_hostname_is_refused(self):
        with pytest.raises(ValidationError, match="not an absolute https URL"):
            _settings(issuer={"enabled": True, "public_url": "terrapod.example.com"})

    def test_a_query_string_is_refused(self):
        """The cloud appends `/.well-known/openid-configuration` to the issuer,
        which a query string would land in the middle of."""
        with pytest.raises(ValidationError, match="query string or fragment"):
            _settings(issuer={"enabled": True, "public_url": "https://x.example.com?a=1"})

    def test_a_fragment_is_refused(self):
        with pytest.raises(ValidationError, match="query string or fragment"):
            _settings(issuer={"enabled": True, "public_url": "https://x.example.com#f"})

    def test_nothing_resolving_is_still_refused_with_its_own_message(self):
        """The original guard, kept: all three empty publishes `{"issuer": ""}`
        and mints every token with `iss: ""`."""
        with pytest.raises(ValidationError, match="no issuer URL resolves"):
            _settings(issuer={"enabled": True})


class TestTheValidatorFollowsTheSAMEFALLBACKCHAINASIssuerURL:
    """`issuer_url()` falls back public_url -> public_webhook_url ->
    external_url, and what a cloud matches is whatever came out of that. So the
    validation is of the RESOLVED value, not of each setting: validating only
    `public_url` would pass a deployment whose issuer is an http webhook URL,
    which is the configuration the chart actually produces.
    """

    def test_an_http_webhook_url_is_refused_when_it_is_what_resolves(self):
        with pytest.raises(ValidationError, match="not an absolute https URL"):
            _settings(issuer={"enabled": True}, webhook="http://hooks.example.com")

    def test_an_http_external_url_is_refused_when_it_is_what_resolves(self):
        with pytest.raises(ValidationError, match="not an absolute https URL"):
            _settings(issuer={"enabled": True}, external="http://terrapod.example.com")

    def test_an_http_fallback_is_NOT_refused_when_public_url_shadows_it(self):
        """The contrast that makes the two tests above mean something. A
        deployment reached over http internally and configured with an explicit
        https issuer is correct, and a validator that checked each setting
        rather than the resolved one would refuse it."""
        _settings(
            issuer={"enabled": True, "public_url": "https://issuer.example.com"},
            webhook="http://hooks.internal",
            external="http://terrapod.internal",
        )

    def test_the_source_is_named_in_the_message(self):
        """Three settings can supply the issuer, so an error that does not say
        which one did sends the operator to edit the wrong key."""
        with pytest.raises(ValidationError, match="public_webhook_url"):
            _settings(issuer={"enabled": True}, webhook="http://hooks.example.com")


class TestTheTrailingSlashRule:
    """A trailing slash is ACCEPTED, and warned about at startup instead.

    Supersedes a version of this class that refused it. The reasoning it was
    refused for is still true and still asserted: `issuer_url()` strips the
    slash, OIDC issuer matching is exact, so a cloud configured with
    `https://x/` rejects a token claiming `https://x`, and the operator reads
    the setting they wrote rather than the string we mint.

    But the penalty was wrong, for the reason the old class already gave about
    the two FALLBACK settings -- that refusing a slash there "would break
    deployments over a cosmetic difference in a value the strip already
    handles". That applies with more force here, not less: `settings =
    Settings()` is evaluated at `terrapod.config` import, so a raise is not a
    refused feature but an API pod that will not start. `https://host/` is an
    ordinary thing to paste and was a WORKING configuration, so a patch release
    that refuses it is a hard-down upgrade for a value the code then strips
    anyway.

    The operator still has to be told, because their cloud-side trust policy is
    the half that is actually wrong -- see
    `oidc_signing.warn_about_issuer_url_cosmetics`, covered in
    `tests/auth/test_oidc_signing.py`.
    """

    def test_a_trailing_slash_on_public_url_is_accepted(self):
        _settings(issuer={"enabled": True, "public_url": "https://x.example.com/"})

    def test_a_trailing_slash_on_a_FALLBACK_is_tolerated(self):
        """Unchanged: these serve the webhook receiver and link generation as
        well, so they were never candidates for refusal."""
        _settings(issuer={"enabled": True}, webhook="https://hooks.example.com/")
        _settings(issuer={"enabled": True}, external="https://terrapod.example.com/")

    def test_a_path_without_a_trailing_slash_is_accepted(self):
        """An issuer may carry a path -- a deployment behind a shared hostname
        serves the two `/.well-known` documents under it."""
        _settings(issuer={"enabled": True, "public_url": "https://x.example.com/terrapod"})

    def test_what_genuinely_cannot_WORK_is_still_refused(self):
        """The line the downgrade must not cross.

        A non-https or scheme-less issuer cannot be fetched by AWS, Azure or
        GCP at all, so federation is broken however the cloud is configured --
        that is a permanent configuration error and stays fatal. Only the
        cosmetic case moved.
        """
        with pytest.raises(ValidationError, match="absolute https URL"):
            _settings(issuer={"enabled": True, "public_url": "http://x.example.com"})
        with pytest.raises(ValidationError, match="absolute https URL"):
            _settings(issuer={"enabled": True, "public_url": "x.example.com"})


class TestTheSubjectFormat:
    """Only two forms exist, and one of them is a migration path out of
    v1.10.0's name-only subject. A free-text field here would mean a typo
    silently selecting the id-bearing default -- which is the safe direction,
    but leaves the operator believing they staged a migration they did not."""

    def test_the_default_carries_the_workspace_id(self):
        assert OIDCIssuerConfig().subject_format == "name-and-id"

    def test_the_legacy_form_is_accepted(self):
        assert OIDCIssuerConfig(subject_format="name").subject_format == "name"

    @pytest.mark.parametrize("bad", ["id", "name_and_id", "NAME", "", "workspace"])
    def test_anything_else_is_refused(self, bad):
        with pytest.raises(ValidationError, match="subject_format must be one of"):
            OIDCIssuerConfig(subject_format=bad)
