"""Claims-to-roles mapper for SSO providers.

Matches IDP claims against provider config rules. Claims can be strings
or lists. Returns deduplicated role names.
"""

from typing import Any

from terrapod.config import ClaimsToRolesMapping
from terrapod.logging_config import get_logger

logger = get_logger(__name__)


def map_claims_to_roles(
    claims: dict[str, Any],
    rules: list[ClaimsToRolesMapping],
) -> list[str]:
    """Map IDP claims to Terrapod role names.

    For each rule, checks if the claim exists in the claims dict and
    if the rule's value matches. Supports both string and list claim values.

    Args:
        claims: Raw claims from the IDP (ID token or SAML attributes).
        rules: Claims-to-roles mapping rules from provider config.

    Returns:
        Deduplicated, sorted list of role names.
    """
    matched_roles: set[str] = set()

    for rule in rules:
        claim_value = claims.get(rule.claim)
        if claim_value is None:
            continue

        if _matches(claim_value, rule.value):
            matched_roles.update(rule.roles)
            logger.debug(
                "Claims rule matched",
                claim=rule.claim,
                value=rule.value,
                roles=rule.roles,
            )

    return sorted(matched_roles)


def _matches(claim_value: Any, rule_value: str) -> bool:
    """Check if a claim value matches a rule value.

    Supports:
    - String equality: claim_value == rule_value
    - List membership: rule_value in claim_value (when claim is a list)
    """
    if isinstance(claim_value, str):
        return claim_value == rule_value
    if isinstance(claim_value, list):
        return rule_value in claim_value
    return False
