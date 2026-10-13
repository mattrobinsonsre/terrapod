"""The mint, end to end, with a real runner token and real signing (#2050).

Every other test of this endpoint patches `sign_identity_token` and asserts on
the claim dict it captured. That is the right tier for the claim logic and it
leaves the whole credential path unexercised: the token is verified by a stub,
the signature is never produced, and nothing checks that what a cloud receives
can actually be verified against what the issuer publishes.

So this one does the parts those cannot:

* a **real runner token**, verified by `get_current_user`'s own path, carrying a
  real phase -- which is how the `phase` claim and the phased subject come to be
  what they are;
* **real signing keys**, resolved from the database by `init_oidc_signing`;
* a **real RS256 signature**, verified against the published JWKS by `kid`,
  exactly as the cloud will.

What it buys over the services-tier tests is a single property that none of them
can state: the token a run is handed is one the issuer's own published key set
verifies. A mismatch there -- a signing key that is not in the JWKS, a `kid` that
does not match, an algorithm disagreement -- fails nothing else in the suite and
breaks every federated run in the deployment.
"""

import uuid

import jwt
import pytest
from sqlalchemy import select, text

from terrapod.auth import oidc_signing
from terrapod.auth.runner_tokens import generate_runner_token
from terrapod.config import settings
from terrapod.db.models import Run, now_utc
from terrapod.db.session import get_db_session
from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"
MINT = "/api/terrapod/v1/runs/{run_id}/cloud-identity-tokens"

AWS = "sts.amazonaws.example"
ISSUER = "https://issuer.example.test"


@pytest.fixture
async def issuer(monkeypatch):
    """A published issuer with a real key resolved from the database."""
    async with get_db_session() as session:
        await session.execute(text("DELETE FROM oidc_signing_keys"))
        await session.commit()
    oidc_signing._reset_for_tests()

    monkeypatch.setattr(settings.auth.oidc_issuer, "enabled", True)
    monkeypatch.setattr(settings.auth.oidc_issuer, "public_url", ISSUER)

    async with get_db_session() as session:
        await oidc_signing.init_oidc_signing(session)
    yield
    oidc_signing._reset_for_tests()


async def _workspace(client, name, audiences):
    resp = await client.post(
        WS_ENDPOINT,
        json={
            "data": {
                "type": "workspaces",
                "attributes": {"name": name, "oidc-audiences": audiences},
            }
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    ws_id = resp.json()["data"]["id"]

    cv = await client.post(
        f"/api/v2/workspaces/{ws_id}/configuration-versions",
        json={
            "data": {
                "type": "configuration-versions",
                "attributes": {"auto-queue-runs": False},
            }
        },
        headers=AUTH,
    )
    assert cv.status_code == 201, cv.text
    up = await client.put(
        cv.json()["data"]["attributes"]["upload-url"],
        content=b"placeholder-tarball-for-tests",
        headers={"Content-Type": "application/x-tar"},
    )
    assert up.status_code in (200, 204), up.text
    return ws_id


async def _run(client, ws_id, *, status="applying"):
    resp = await client.post(
        "/api/v2/runs",
        json={
            "data": {
                "type": "runs",
                "attributes": {"message": "end-to-end identity"},
                "relationships": {"workspace": {"data": {"id": ws_id, "type": "workspaces"}}},
            }
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["data"]["id"]

    async with get_db_session() as session:
        run = (
            await session.execute(
                select(Run).where(Run.id == uuid.UUID(run_id.removeprefix("run-")))
            )
        ).scalar_one()
        run.status = status
        run.plan_finished_at = now_utc()
        await session.commit()
    return run_id


class TestTheMintedTokenVerifiesAgainstThePublishedJWKS:
    async def test_a_real_runner_token_mints_a_token_a_cloud_can_verify(self, app, client, issuer):
        """The whole credential path, with nothing stubbed.

        Verified rather than merely decoded, and verified by looking the `kid`
        up in the published set -- which is what a cloud does, and the only way
        to catch a signing key that is not in the JWKS it serves.
        """
        set_auth(app, admin_user())
        ws_id = await _workspace(client, "e2e-identity", {"aws": [AWS]})
        run_id = await _run(client, ws_id)

        token = generate_runner_token(
            uuid.UUID(run_id.removeprefix("run-")), ttl=300, phase="apply"
        )
        resp = await client.post(
            MINT.format(run_id=run_id),
            json={"providers": ["aws"], "discovery": "ok"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["phase"] == "apply", (
            "the phase did not survive the real token, so the identity carries "
            "an unphased subject -- the form a trust policy has to exclude"
        )

        jwt_str = body["tokens"][0]["token"]
        header = jwt.get_unverified_header(jwt_str)
        assert header["alg"] == "RS256"

        published = {k["kid"]: k for k in oidc_signing.get_jwks()["keys"]}
        assert header["kid"] in published, (
            f"signed with {header['kid']}, which the issuer does not publish -- "
            f"it publishes {sorted(published)}. No cloud could verify this token."
        )

        claims = jwt.decode(
            jwt_str,
            jwt.PyJWK(published[header["kid"]]).key,
            algorithms=["RS256"],
            audience=AWS,
            issuer=ISSUER,
        )

        assert claims["phase"] == "apply"
        assert claims["workspace"] == "e2e-identity"
        assert claims["sub"] == f"workspace:e2e-identity@{claims['workspace_id']}:phase:apply"
        assert claims["run_id"] == run_id.removeprefix("run-")
        assert claims["terrapod_organization"] == "default"
        assert claims["exp"] > claims["iat"]

    async def test_a_PLAN_phase_token_mints_the_plan_subject(self, app, client, issuer):
        """The contrast that makes the phase assertions mean something: the same
        path with the other phase must produce a different subject, or a policy
        bound to `:phase:apply` would be satisfied by a pull-request plan."""
        set_auth(app, admin_user())
        ws_id = await _workspace(client, "e2e-identity-plan", {"aws": [AWS]})
        run_id = await _run(client, ws_id, status="planning")

        token = generate_runner_token(uuid.UUID(run_id.removeprefix("run-")), ttl=300, phase="plan")
        resp = await client.post(
            MINT.format(run_id=run_id),
            json={"providers": ["aws"], "discovery": "ok"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

        jwt_str = resp.json()["tokens"][0]["token"]
        published = {k["kid"]: k for k in oidc_signing.get_jwks()["keys"]}
        header = jwt.get_unverified_header(jwt_str)
        claims = jwt.decode(
            jwt_str,
            jwt.PyJWK(published[header["kid"]]).key,
            algorithms=["RS256"],
            audience=AWS,
            issuer=ISSUER,
        )
        assert claims["phase"] == "plan"
        assert claims["sub"].endswith(":phase:plan")

    async def test_the_minted_iss_is_what_the_issuer_PUBLISHES(self, app, client, issuer):
        """Three values have to agree and only one of them is under test
        anywhere else. `jwt.decode(issuer=...)` above already enforces it, but
        asserted directly so a failure says which value moved."""
        from terrapod.api.routers.oidc_issuer import issuer_url

        set_auth(app, admin_user())
        ws_id = await _workspace(client, "e2e-identity-iss", {"aws": [AWS]})
        run_id = await _run(client, ws_id)

        token = generate_runner_token(
            uuid.UUID(run_id.removeprefix("run-")), ttl=300, phase="apply"
        )
        resp = await client.post(
            MINT.format(run_id=run_id),
            json={"providers": ["aws"], "discovery": "ok"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        # Unverified on purpose: the assertions below are about the CLAIMS
        # Terrapod put in the token, and the signature is covered by the
        # verifying decodes earlier in this file. Verifying again here would
        # test PyJWT rather than the mint.
        # nosemgrep: python.jwt.security.unverified-jwt-decode.unverified-jwt-decode
        claims = jwt.decode(resp.json()["tokens"][0]["token"], options={"verify_signature": False})
        assert claims["iss"] == issuer_url() == ISSUER
