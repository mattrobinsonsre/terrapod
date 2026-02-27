"""Tests for claims-to-roles mapper."""

from terrapod.auth.claims_mapper import map_claims_to_roles
from terrapod.config import ClaimsToRolesMapping


class TestMapClaimsToRoles:
    def test_string_claim_match(self):
        claims = {"department": "engineering"}
        rules = [
            ClaimsToRolesMapping(claim="department", value="engineering", roles=["dev"]),
        ]
        assert map_claims_to_roles(claims, rules) == ["dev"]

    def test_string_claim_no_match(self):
        claims = {"department": "marketing"}
        rules = [
            ClaimsToRolesMapping(claim="department", value="engineering", roles=["dev"]),
        ]
        assert map_claims_to_roles(claims, rules) == []

    def test_list_claim_match(self):
        claims = {"groups": ["engineering", "devops", "on-call"]}
        rules = [
            ClaimsToRolesMapping(claim="groups", value="devops", roles=["admin"]),
        ]
        assert map_claims_to_roles(claims, rules) == ["admin"]

    def test_list_claim_no_match(self):
        claims = {"groups": ["engineering", "devops"]}
        rules = [
            ClaimsToRolesMapping(claim="groups", value="security", roles=["audit"]),
        ]
        assert map_claims_to_roles(claims, rules) == []

    def test_missing_claim_skipped(self):
        claims = {"name": "Test User"}
        rules = [
            ClaimsToRolesMapping(claim="groups", value="admin", roles=["admin"]),
        ]
        assert map_claims_to_roles(claims, rules) == []

    def test_multiple_rules_deduplicated(self):
        claims = {"groups": ["eng", "devops"], "department": "engineering"}
        rules = [
            ClaimsToRolesMapping(claim="groups", value="eng", roles=["dev", "viewer"]),
            ClaimsToRolesMapping(claim="groups", value="devops", roles=["admin", "dev"]),
            ClaimsToRolesMapping(claim="department", value="engineering", roles=["viewer"]),
        ]
        result = map_claims_to_roles(claims, rules)
        # Should be deduplicated and sorted
        assert result == ["admin", "dev", "viewer"]

    def test_empty_rules(self):
        claims = {"groups": ["admin"]}
        assert map_claims_to_roles(claims, []) == []

    def test_empty_claims(self):
        rules = [
            ClaimsToRolesMapping(claim="groups", value="admin", roles=["admin"]),
        ]
        assert map_claims_to_roles({}, rules) == []

    def test_non_string_non_list_claim_ignored(self):
        claims = {"count": 42}
        rules = [
            ClaimsToRolesMapping(claim="count", value="42", roles=["admin"]),
        ]
        assert map_claims_to_roles(claims, rules) == []
