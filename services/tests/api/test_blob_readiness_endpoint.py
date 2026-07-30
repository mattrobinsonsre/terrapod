"""The blob-readiness endpoint (#1147, #1151).

Separate from `/ha/status` deliberately: status is answered from local state in
milliseconds and an operator refreshes it freely, while this one makes real round
trips to the object store. Putting it inline would make the cheap endpoint
expensive and tempt callers to poll it.

The response shape carries the honesty the service is careful about — `sampled`,
plus per-class `checked` against `total-rows` — so nobody can read a clean sample
as a clean estate. #1151 added the other half of it: once a class can be turned
off, or can be one no row guarantees, the report has to say which classes it
actually covered. That is what these assert.
"""

from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from terrapod.api.dependencies import AuthenticatedUser, require_admin_or_audit
from terrapod.api.routers.ha import router
from terrapod.services import blob_readiness


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/terrapod/v1")
    app.dependency_overrides[require_admin_or_audit] = lambda: AuthenticatedUser(
        email="admin@example.com",
        display_name="Admin",
        provider_name="local",
        roles=["admin"],
        auth_method="session",
    )
    return app


async def _get(path: str):
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


BASE = "/api/terrapod/v1/ha/blob-readiness"


def _result(**kw):
    defaults = {
        "classes": [
            blob_readiness.ClassReadiness(
                name="state",
                tier=blob_readiness.IRREPLACEABLE,
                total_rows=40_000,
                checked=25,
                missing=2,
                missing_examples=["state/ws-1/sv-9.tfstate"],
                complete=False,
            )
        ],
        "sampled": True,
        "duration_ms": 42,
    }
    defaults.update(kw)
    return blob_readiness.BlobReadiness(**defaults)


class TestTheResponseCannotBeMisread:
    async def test_a_sample_is_labelled_as_one(self):
        with patch.object(blob_readiness, "check", new=AsyncMock(return_value=_result())):
            body = (await _get(BASE)).json()["data"]["attributes"]

        assert body["sampled"] is True
        assert body["classes"][0]["checked"] == 25
        assert body["classes"][0]["total-rows"] == 40_000
        assert body["classes"][0]["complete"] is False

    async def test_what_should_stop_a_failover_is_named(self):
        """An operator reads this while deciding whether to move DNS. Leaving them
        to derive it from a list of classes is how it gets read wrong."""
        with patch.object(blob_readiness, "check", new=AsyncMock(return_value=_result())):
            body = (await _get(BASE)).json()["data"]["attributes"]

        assert body["irreplaceable-missing"] == ["state"]
        assert body["missing-total"] == 2

    async def test_an_unreachable_store_says_so_rather_than_erroring(self):
        with patch.object(
            blob_readiness,
            "check",
            new=AsyncMock(
                return_value=blob_readiness.BlobReadiness(unavailable_reason="no storage")
            ),
        ):
            resp = await _get(BASE)

        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["unavailable-reason"] == "no storage"

    async def test_the_tier_is_reported_per_class(self):
        """Whether a missing class matters depends on the deployment — a cold
        cache is fine unless the node is sealed. The tier keeps that the
        operator's call rather than the endpoint's."""
        with patch.object(blob_readiness, "check", new=AsyncMock(return_value=_result())):
            body = (await _get(BASE)).json()["data"]["attributes"]

        assert body["classes"][0]["tier"] == "irreplaceable"


class TestQueryParameters:
    async def test_full_is_passed_through_and_defaults_off(self):
        """Full verification is thousands of round trips, so it has to be asked
        for explicitly."""
        checker = AsyncMock(return_value=_result(sampled=False))
        with patch.object(blob_readiness, "check", new=checker):
            await _get(BASE)
            await _get(f"{BASE}?full=true")

        assert checker.await_args_list[0].kwargs["full"] is False
        assert checker.await_args_list[1].kwargs["full"] is True

    async def test_the_sample_size_is_overridable(self):
        checker = AsyncMock(return_value=_result())
        with patch.object(blob_readiness, "check", new=checker):
            await _get(f"{BASE}?sample=200")

        assert checker.await_args.kwargs["sample"] == 200


class TestItIsNotOnTheCheapEndpoint:
    def test_status_and_blob_readiness_are_separate_routes(self):
        """Folding this into `/status` would make the endpoint an operator
        refreshes freely do real object-store I/O every time."""
        paths = {r.path for r in router.routes}

        assert "/ha/status" in paths
        assert "/ha/blob-readiness" in paths

    def test_it_is_admin_or_audit_gated(self):
        for route in router.routes:
            if route.path != "/ha/blob-readiness":
                continue
            names = [d.call.__name__ for d in route.dependant.dependencies if d.call]
            assert "require_admin_or_audit" in names
            return
        raise AssertionError("route not found")


class TestTheReportSaysWhatItDidNotCover:
    """The counterpart that makes `irreplaceable-missing` trustworthy. A class
    that is off, or that no row guarantees, produces zero missing objects — which
    is indistinguishable from a pass unless the response names it."""

    async def test_unchecked_irreplaceable_classes_are_named(self):
        result = _result(
            classes=[
                blob_readiness.ClassReadiness(
                    name="state",
                    tier=blob_readiness.IRREPLACEABLE,
                    total_rows=10,
                    checked=10,
                    missing=0,
                    complete=True,
                ),
                blob_readiness.ClassReadiness(
                    name="cost_pricesheet",
                    tier=blob_readiness.IRREPLACEABLE,
                    mode="verify",
                    verifiable=False,
                    note="a single cache object with no row behind it",
                ),
            ]
        )
        with patch.object(blob_readiness, "check", new=AsyncMock(return_value=result)):
            body = (await _get(BASE)).json()["data"]["attributes"]

        assert body["irreplaceable-missing"] == []
        assert body["irreplaceable-unchecked"] == ["cost_pricesheet"]

    async def test_each_class_reports_its_mode(self):
        """Answers 'why is this class empty' without a second look at the config."""
        result = _result(
            classes=[
                blob_readiness.ClassReadiness(
                    name="run_logs",
                    tier=blob_readiness.HISTORY,
                    mode="off",
                    note="configured off, so nothing was checked",
                )
            ]
        )
        with patch.object(blob_readiness, "check", new=AsyncMock(return_value=result)):
            cls = (await _get(BASE)).json()["data"]["attributes"]["classes"][0]

        assert cls["mode"] == "off"
        assert cls["verifiable"] is True
        assert cls["note"] == "configured off, so nothing was checked"

    async def test_a_copy_only_class_carries_its_reason(self):
        result = _result(
            classes=[
                blob_readiness.ClassReadiness(
                    name="vcs_archives",
                    tier=blob_readiness.REDERIVABLE,
                    verifiable=False,
                    note="a content-addressed cache with no table behind it",
                )
            ]
        )
        with patch.object(blob_readiness, "check", new=AsyncMock(return_value=result)):
            cls = (await _get(BASE)).json()["data"]["attributes"]["classes"][0]

        assert cls["verifiable"] is False
        assert cls["note"]
