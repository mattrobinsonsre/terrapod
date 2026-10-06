"""`validate_oidc_audiences` — the sole server-side authority for the attribute.

It shipped with no tests at any tier, which mattered more than the count
suggests: it is the only thing standing behind four write paths (workspace
create and update, bulk update, and the autodiscovery rule template), and every
other consumer defers to it in prose rather than re-checking. The provider's
validator says "the server's refusal is the authority"; the web helper says "the
server is the authority"; the MCP tool descriptions say "refused (422)". Delete
any branch below and all of that silently became untrue.

Each test names the reason the branch exists, because several of them look like
fussiness and are not — the empty-list refusal and the byte-for-byte storage in
particular are load-bearing for the two-level merge and for the Terraform
provider respectively.
"""

from __future__ import annotations

import pytest

from terrapod.services.workspace_settings import (
    MAX_OIDC_AUDIENCE_LEN,
    MAX_OIDC_AUDIENCES_PER_TARGET,
    MAX_OIDC_TARGET_LEN,
    MAX_OIDC_TARGETS,
    validate_oidc_audiences,
)

AWS = "sts.amazonaws.com"


class TestWhatIsAccepted:
    def test_none_is_the_unset_case_and_becomes_an_empty_map(self):
        """Not a refusal: `None` is the attribute being absent, which the house
        pattern in this module treats as "unset" rather than as bad input."""
        assert validate_oidc_audiences(None) == {}

    def test_an_empty_map_is_valid_and_is_the_common_case(self):
        """It means "take the deployment catalogue as it stands", NOT "mint
        nothing" — the distinction the whole two-level merge rests on."""
        assert validate_oidc_audiences({}) == {}

    @pytest.mark.parametrize("key", ["aws", "azurerm", "vault", "aws.west", "vault.eu"])
    def test_the_documented_key_forms_are_accepted(self, key):
        assert validate_oidc_audiences({key: [AWS]}) == {key: [AWS]}

    def test_values_are_stored_byte_for_byte(self):
        """Never normalised. The Terraform provider writes the server's response
        back into state, so lower-casing or trimming here would make every plan
        disagree with its own apply — the defect that bit
        `terrapod_vcs_connection` in v1.9.0."""
        odd = "  HTTPS://Example.COM/Path  "
        assert validate_oidc_audiences({"vault": [odd]}) == {"vault": [odd]}

    def test_the_limits_are_boundaries_not_approximations(self):
        at_cap = {f"p{i}": [AWS] for i in range(MAX_OIDC_TARGETS)}
        assert validate_oidc_audiences(at_cap) == at_cap
        many = {"aws": [f"a{i}" for i in range(MAX_OIDC_AUDIENCES_PER_TARGET)]}
        assert validate_oidc_audiences(many) == many
        long_key = {"k" * MAX_OIDC_TARGET_LEN: [AWS]}
        assert validate_oidc_audiences(long_key) == long_key
        long_aud = {"aws": ["a" * MAX_OIDC_AUDIENCE_LEN]}
        assert validate_oidc_audiences(long_aud) == long_aud


class TestWhatIsRefused:
    @pytest.mark.parametrize("raw", [[], "aws", 7])
    def test_a_non_mapping_is_refused(self, raw):
        with pytest.raises(ValueError, match="must be an object"):
            validate_oidc_audiences(raw)

    def test_more_providers_than_the_cap(self):
        with pytest.raises(ValueError, match="at most"):
            validate_oidc_audiences({f"p{i}": [AWS] for i in range(MAX_OIDC_TARGETS + 1)})

    @pytest.mark.parametrize("key", [7, None, ()])
    def test_a_non_string_key(self, key):
        with pytest.raises(ValueError, match="must be a provider name"):
            validate_oidc_audiences({key: [AWS]})

    @pytest.mark.parametrize("key", ["", "   "])
    def test_a_blank_key(self, key):
        with pytest.raises(ValueError, match="cannot be blank"):
            validate_oidc_audiences({key: [AWS]})

    def test_an_over_long_key(self):
        with pytest.raises(ValueError, match="characters or fewer"):
            validate_oidc_audiences({"k" * (MAX_OIDC_TARGET_LEN + 1): [AWS]})

    @pytest.mark.parametrize("key", ["aws west", "aws\tw", "aws\nw"])
    def test_whitespace_in_a_key(self, key):
        with pytest.raises(ValueError, match="whitespace"):
            validate_oidc_audiences({key: [AWS]})

    def test_more_than_one_dot(self):
        """`provider.alias` has exactly one; a second is a typo, not a deeper
        namespace."""
        with pytest.raises(ValueError, match="more than one"):
            validate_oidc_audiences({"aws.west.extra": [AWS]})

    @pytest.mark.parametrize("key", [".aws", "aws."])
    def test_a_leading_or_trailing_dot(self, key):
        with pytest.raises(ValueError, match="start or end"):
            validate_oidc_audiences({key: [AWS]})

    @pytest.mark.parametrize("key", ["foo/bar", "/etc/evil", "a\\b"])
    def test_a_path_significant_key(self, key):
        """This key becomes a DIRECTORY NAME under the runner's token dir, so a
        separator is the one case where the deliberately-conservative stance
        above does not hold."""
        with pytest.raises(ValueError, match="cannot contain"):
            validate_oidc_audiences({key: [AWS]})

    @pytest.mark.parametrize("value", ["aws", 7, None, {"a": 1}])
    def test_a_non_list_value(self, value):
        with pytest.raises(ValueError, match="must be a list"):
            validate_oidc_audiences({"aws": value})

    def test_an_explicitly_empty_list(self):
        """Refused because it means neither "override" nor "remove". Accepting
        it would let an operator believe they had suppressed a target when the
        catalogue still supplies it."""
        with pytest.raises(ValueError, match="cannot be an empty list"):
            validate_oidc_audiences({"aws": []})

    def test_more_audiences_than_the_cap(self):
        with pytest.raises(ValueError, match="at most"):
            validate_oidc_audiences(
                {"aws": [f"a{i}" for i in range(MAX_OIDC_AUDIENCES_PER_TARGET + 1)]}
            )

    @pytest.mark.parametrize("entry", [7, None, []])
    def test_a_non_string_audience(self, entry):
        with pytest.raises(ValueError, match="must be a string"):
            validate_oidc_audiences({"aws": [entry]})

    @pytest.mark.parametrize("entry", ["", "   "])
    def test_a_blank_audience(self, entry):
        with pytest.raises(ValueError, match="cannot be blank"):
            validate_oidc_audiences({"aws": [entry]})

    def test_an_over_long_audience(self):
        with pytest.raises(ValueError, match="must be"):
            validate_oidc_audiences({"aws": ["a" * (MAX_OIDC_AUDIENCE_LEN + 1)]})

    def test_a_duplicate_audience(self):
        """Refused rather than de-duplicated, because silently dropping one
        would make the stored value differ from what was sent — the same
        round-trip problem as normalising."""
        with pytest.raises(ValueError, match="twice"):
            validate_oidc_audiences({"aws": [AWS, AWS]})
