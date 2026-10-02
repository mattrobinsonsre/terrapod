"""Deriving Terrapod role names from an identity provider's groups.

Shared by every SSO connector, because the mapping is a property of what an IdP
group *means* rather than of which protocol carried it — and because having it in
one connector and not the other is how `GHSA-22vg-4g2w-7w34` happened: the OIDC
connector applied `role_prefixes` and the SAML connector did not apply it at all.

The two rules here are deliberately separate, and only the second protects the
common case:

- `roles_from_idp_groups` makes `role_prefixes` a **filter**. It used to strip a
  matching prefix and pass everything else through unchanged, which means an
  operator who wrote `role_prefixes: ["terrapod-"]` to scope which of their groups
  Terrapod listens to got the opposite: `terrapod-admin` became `admin`, and a
  completely unrelated group called `admin` *also* became `admin`.
- `PLATFORM_ROLE_NAMES` are refused from this source entirely (see
  `sso_service`). That is the rule that matters, because `role_prefixes` is empty
  by default: in a deployment that never configured it, filtering does nothing and
  any group named `admin` is still a group named `admin`.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


def roles_from_idp_groups(
    groups: list[str], prefixes: list[str], *, provider: str = ""
) -> list[str]:
    """Map an IdP's group names to Terrapod role names.

    With `prefixes` configured, a group must carry one of them to be considered at
    all, and the prefix is removed: `["terrapod-"]` turns `terrapod-platform` into
    `platform` and discards `platform` and `sales-admin` alike. With `prefixes`
    empty every group is passed through unchanged, which is the documented
    behaviour for an IdP whose groups are already named as Terrapod roles.

    Order and duplicates are preserved as given; de-duplication happens where the
    roles are unioned into a set.
    """
    if not prefixes:
        return list(groups)

    kept: list[str] = []
    dropped: list[str] = []
    for g in groups:
        for prefix in prefixes:
            if g.startswith(prefix):
                kept.append(g[len(prefix) :])
                break
        else:
            # No prefix matched. Previously this appended `g` unchanged, so a
            # group named exactly like a role granted that role.
            dropped.append(g)

    if dropped:
        logger.info(
            "Dropped IdP groups that match no configured role prefix",
            provider=provider,
            dropped=sorted(set(dropped)),
            prefixes=prefixes,
        )
    return kept
