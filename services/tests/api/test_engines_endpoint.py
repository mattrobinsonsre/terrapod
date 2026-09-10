"""`GET /api/v1/engines` lists what this deployment enables (#1555).

The UI offers an engine only when more than one is on, so the endpoint's answer
decides whether a Terraform-only deployment sees any engine UI at all. Both
directions are pinned: Pulumi present when enabled, absent when not.
"""

from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from tests.api.test_workspaces import _AUTH, _BASE, _make_app, _user


async def _engines(enabled: tuple[str, ...]) -> list[str]:
    app, _ = _make_app(_user())
    with patch("terrapod.engines.known_engines", return_value=enabled):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.get("/api/v1/engines", headers=_AUTH)
    assert r.status_code == 200, r.text
    return [d["id"] for d in r.json()["data"]]


async def test_lists_every_enabled_engine():
    assert await _engines(("pulumi", "terraform")) == ["pulumi", "terraform"]


async def test_a_gated_off_engine_is_absent():
    assert await _engines(("terraform",)) == ["terraform"]


async def test_the_real_gate_always_includes_terraform():
    """Without the patch: whatever the config, Terraform is never optional."""
    app, _ = _make_app(_user())
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
        r = await c.get("/api/v1/engines", headers=_AUTH)
    assert "terraform" in [d["id"] for d in r.json()["data"]]
