"""`GET /api/v1/engines` lists every engine this build can run (#1555, #1986).

The UI offers an engine picker only when more than one is listed, so this
endpoint's answer decides what a deployment sees. It used to be filtered by an
on/off switch, and both directions were pinned here — present when enabled,
absent when not.

**That switch is withdrawn (#1986): the platform should just work.** So the
property under test inverts. There is no configuration that removes an engine
from this list, and a terraform/tofu-only deployment pays nothing for the others
because it simply never names one — not because it switched them off. The test
that asserted an engine could be absent is gone with the mechanism, replaced by
one proving nothing can hide it.
"""

from httpx import ASGITransport, AsyncClient

from terrapod.engines import known_engines
from tests.api.test_workspaces import _AUTH, _BASE, _make_app, _user


async def _engines() -> list[str]:
    app, _ = _make_app(_user())
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
        r = await c.get("/api/v1/engines", headers=_AUTH)
    assert r.status_code == 200, r.text
    return [d["id"] for d in r.json()["data"]]


async def test_it_lists_every_engine_the_build_contains():
    assert await _engines() == sorted(known_engines())


async def test_terraform_is_never_absent():
    """Whatever else changes, Terraform is not optional — it is what Terrapod is."""
    assert "terraform" in await _engines()


async def test_no_setting_shrinks_the_list():
    """Derived from the registry, so a reintroduced filter fails here.

    Reading the registry directly rather than naming engines: a filter added
    anywhere between it and the response would show up as a shortfall whichever
    engines happen to be registered at the time, and a test naming "pulumi"
    would silently stop covering a third engine added later.
    """
    from terrapod.engines import _REGISTRY

    assert set(await _engines()) == set(_REGISTRY), (
        "the endpoint dropped an engine the build contains — there is no on/off "
        "switch any more (#1986), so something is filtering what should not be"
    )
