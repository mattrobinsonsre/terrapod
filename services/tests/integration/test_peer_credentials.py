"""The peer link authenticates against config, not a stored row (#1171).

The credential is declared in the chart in full, so config is the only thing
that can be authoritative about it. These pin the three properties that
replaces a database row with:

- a declared credential authenticates, with no CLI step and nothing persisted;
- the token it issues is a peer identity, not a reuse of the runner path;
- rotation is just editing the values, and the previous secret dies with it.

The refusal paths (unknown id, wrong secret, unpaired node, missing halves) need
no database and are pinned in `tests/api/test_oauth_client_credentials.py`.

The last of those is why the row had to go: when the expected value lives in two
places, rotating one and not the other is a silent failure mode. There is now
only one place.
"""

from terrapod.config import HAConfig, HAPeerConfig, HAPeerInboundConfig


def _ha(client_id: str = "", client_secret: str = "", name: str = "") -> HAConfig:
    """The real config object rather than a stub — the grant reads it directly,
    and a SimpleNamespace would only prove the patch worked."""
    return HAConfig(
        peer=HAPeerConfig(
            inbound=HAPeerInboundConfig(client_id=client_id, client_secret=client_secret, name=name)
        )
    )


def _grant(client_id: str, client_secret: str) -> dict:
    return {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }


class TestDeclaredCredential:
    async def test_a_config_declared_credential_authenticates(self, app, client, monkeypatch):
        """The assertion the feature lives or dies on: a link set up purely from
        the chart works, with nothing minted and nothing stored."""
        monkeypatch.setattr(
            "terrapod.api.routers.oauth.settings.ha", _ha("peer-b", "s3cret", "Node B")
        )

        resp = await client.post("/oauth/token", data=_grant("peer-b", "s3cret"))

        assert resp.status_code == 200, resp.text
        assert resp.json()["access_token"]
        assert resp.json()["token_type"] == "bearer"

    async def test_the_issued_token_is_a_peer_not_a_runner(self, app, client, monkeypatch):
        """A peer may read resolved sensitive variables. That must not widen what
        a runner can reach, nor leave an audit unable to tell the two apart."""
        from sqlalchemy import select

        from terrapod.db.models import APIToken
        from terrapod.db.session import get_db_session

        monkeypatch.setattr("terrapod.api.routers.oauth.settings.ha", _ha("peer-b", "s3cret"))
        await client.post("/oauth/token", data=_grant("peer-b", "s3cret"))

        async with get_db_session() as db:
            tokens = (await db.scalars(select(APIToken))).all()
        assert [t.kind for t in tokens] == ["peer"]


class TestRotation:
    async def test_editing_the_config_rotates_immediately(self, app, client, monkeypatch):
        """No stored copy means no way for the two to drift apart — the whole
        reason the persisted row was removed."""
        monkeypatch.setattr("terrapod.api.routers.oauth.settings.ha", _ha("peer-b", "old"))
        assert (await client.post("/oauth/token", data=_grant("peer-b", "old"))).status_code == 200

        monkeypatch.setattr("terrapod.api.routers.oauth.settings.ha", _ha("peer-b", "new"))

        assert (await client.post("/oauth/token", data=_grant("peer-b", "new"))).status_code == 200
        assert (await client.post("/oauth/token", data=_grant("peer-b", "old"))).status_code == 401
