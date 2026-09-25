"""A token created at another user's endpoint is bound to THAT user (#1838).

`user_id` gated authorization and was then discarded. An admin posting to
`/users/planner/authentication-tokens` received an ordinary **admin** token,
while the path, the response's `bound-to` and the audit trail all said a token
had been issued for `planner`.

The reported consequence is the one that matters: every "this role must be
denied" test written that way passes, because the token under test is an
admin's. The boundary looks like it holds while nothing is exercising it.

`bound_to` is an EMAIL -- `dependencies.py` resolves a token's roles through it
-- while the path segment is a username, so the fix has to resolve one to the
other and refuse when it cannot. An unknown `bound_to` would mint a live token
bound to nobody.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod.api.routers import tokens as router


def _user(email="admin@example.com", roles=("admin",)):
    return SimpleNamespace(email=email, roles=list(roles), kind="interactive")


def _db(local_row=None):
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=local_row)
    db.execute = AsyncMock(return_value=result)
    return db


def _body(kind="interactive"):
    return SimpleNamespace(
        data=SimpleNamespace(
            attributes=SimpleNamespace(
                kind=kind, description="d", lifespan_hours=1, pinned_roles=None
            )
        )
    )


class TestTheTokenIsBoundToTheUserInThePath:
    async def test_an_admin_minting_for_another_user_binds_to_that_user(self):
        db = _db(local_row=SimpleNamespace(email="planner@example.com", is_active=True))
        created = SimpleNamespace(id="t1", bound_to=None, kind="interactive")

        with (
            patch.object(router, "effective_platform_roles", return_value={"admin"}),
            patch.object(
                router, "create_api_token", new=AsyncMock(return_value=(created, "raw"))
            ) as mk,
            patch.object(router, "_token_to_jsonapi", return_value={}),
        ):
            await router.create_user_token(
                user_id="planner@example.com",
                body=_body(),
                user=_user(),
                db=db,
            )

        kwargs = mk.await_args.kwargs
        assert kwargs["bound_to"] == "planner@example.com", (
            "the token must carry the named user's identity; binding it to the "
            "caller is what made every RBAC test pass against an admin token"
        )
        # The audit trail still says who actually minted it.
        assert kwargs["created_by"] == "admin@example.com"

    async def test_minting_for_yourself_is_unchanged(self):
        db = _db()
        created = SimpleNamespace(id="t1", bound_to=None, kind="interactive")
        with (
            patch.object(router, "effective_platform_roles", return_value=set()),
            patch.object(
                router, "create_api_token", new=AsyncMock(return_value=(created, "raw"))
            ) as mk,
            patch.object(router, "_token_to_jsonapi", return_value={}),
        ):
            await router.create_user_token(
                user_id="alice", body=_body(), user=_user("alice@example.com", roles=()), db=db
            )
        assert mk.await_args.kwargs["bound_to"] == "alice@example.com"

    async def test_a_non_admin_still_cannot_mint_for_someone_else(self):
        with patch.object(router, "effective_platform_roles", return_value=set()):
            with pytest.raises(HTTPException) as exc:
                await router.create_user_token(
                    user_id="bob@example.com",
                    body=_body(),
                    user=_user("alice@example.com", roles=()),
                    db=_db(),
                )
        assert exc.value.status_code == 403


class TestItRefusesToBindToNobody:
    """An unknown `bound_to` resolves to NO roles, and for an SSO identity a
    missing local row is deliberately not a rejection (#495) -- so a typo would
    mint a live token bound to nobody, whose behaviour depends on whichever
    check happens to notice first."""

    async def test_a_bare_username_is_refused_with_a_reason(self):
        """A username cannot be turned into an email for an identity with no
        local row, and guessing a domain is how you mint one for the wrong
        person."""
        with patch.object(router, "effective_platform_roles", return_value={"admin"}):
            with pytest.raises(HTTPException) as exc:
                await router.create_user_token(
                    user_id="planner", body=_body(), user=_user(), db=_db()
                )
        assert exc.value.status_code == 422
        assert "full email" in exc.value.detail

    async def test_an_unknown_email_is_refused(self):
        db = _db(local_row=None)
        with (
            patch.object(router, "effective_platform_roles", return_value={"admin"}),
            patch.object(router, "user_seen_within_window", new=AsyncMock(return_value=False)),
        ):
            with pytest.raises(HTTPException) as exc:
                await router.create_user_token(
                    user_id="ghost@example.com", body=_body(), user=_user(), db=db
                )
        assert exc.value.status_code == 404

    async def test_an_sso_identity_that_has_signed_in_is_accepted(self):
        """SSO users have no local row, so requiring one would make delegation
        impossible for exactly the deployments most likely to want it. Having
        signed in recently is the same evidence `bound_token_idle_days`
        already trusts."""
        db = _db(local_row=None)
        created = SimpleNamespace(id="t1", bound_to=None, kind="interactive")
        with (
            patch.object(router, "effective_platform_roles", return_value={"admin"}),
            patch.object(router, "user_seen_within_window", new=AsyncMock(return_value=True)),
            patch.object(
                router, "create_api_token", new=AsyncMock(return_value=(created, "raw"))
            ) as mk,
            patch.object(router, "_token_to_jsonapi", return_value={}),
        ):
            await router.create_user_token(
                user_id="sso-user@example.com", body=_body(), user=_user(), db=db
            )
        assert mk.await_args.kwargs["bound_to"] == "sso-user@example.com"

    async def test_a_deactivated_local_user_is_refused(self):
        """Binding to them would mint a token that cannot authenticate --
        `_bound_token_owner_active` rejects an inactive owner."""
        db = _db(local_row=SimpleNamespace(email="gone@example.com", is_active=False))
        with patch.object(router, "effective_platform_roles", return_value={"admin"}):
            with pytest.raises(HTTPException) as exc:
                await router.create_user_token(
                    user_id="gone@example.com", body=_body(), user=_user(), db=db
                )
        assert exc.value.status_code == 422
        assert "deactivated" in exc.value.detail
