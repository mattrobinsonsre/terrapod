"""Warm-ahead submission, status and refusal (#1420).

The properties worth pinning are the ones an operator relies on when they are
about to seal a network and cannot easily undo it: a sealed node says *why* it
cannot warm, a submission comes back with something to poll, and the report is
per item rather than a verdict.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.config import settings
from terrapod.services.warm_ahead import ItemOutcome, WarmItem, WarmJob

_BASE = "http://test"


@pytest.fixture(autouse=True)
def _restore():
    before = settings.registry.cache_only
    yield
    settings.registry.cache_only = before


def _app():
    app = create_app()
    app.dependency_overrides[require_admin] = lambda: AuthenticatedUser(
        email="admin@example.com",
        display_name="Admin",
        roles=["admin"],
        provider_name="local",
        auth_method="session",
    )
    return app


class TestSubmitting:
    @patch("terrapod.api.routers.cache_warm.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.services.warm_ahead.create_job", new_callable=AsyncMock)
    async def test_it_returns_a_job_to_poll(self, create_job, enqueue) -> None:
        """202 with an id, not a result — the work outlives the request."""
        create_job.return_value = WarmJob(
            job_id="warm-abc", status="queued", total=2, submitted_at="2026-01-01T00:00:00Z"
        )
        body = {
            "packages": [
                {"ecosystem": "pypi", "name": "requests"},
                {"ecosystem": "npm", "name": "left-pad", "version": "1.3.0"},
            ]
        }
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            r = await c.post("/api/terrapod/v1/admin/package-cache/warm", json=body)

        assert r.status_code == 202
        assert r.json()["data"]["id"] == "warm-abc"
        enqueue.assert_awaited_once()

    async def test_an_empty_submission_is_refused(self) -> None:
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            r = await c.post("/api/terrapod/v1/admin/package-cache/warm", json={"packages": []})
        assert r.status_code == 422

    async def test_an_unknown_ecosystem_is_refused(self) -> None:
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            r = await c.post(
                "/api/terrapod/v1/admin/package-cache/warm",
                json={"packages": [{"ecosystem": "cargo", "name": "serde"}]},
            )
        assert r.status_code == 422

    @patch("terrapod.api.routers.cache_warm.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.services.warm_ahead.create_job", new_callable=AsyncMock)
    async def test_an_oversized_submission_is_refused(self, create_job, enqueue) -> None:
        """A paste meant for a different box, caught at the door."""
        body = {"packages": [{"ecosystem": "pypi", "name": f"p{i}"} for i in range(501)]}
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            r = await c.post("/api/terrapod/v1/admin/package-cache/warm", json=body)
        assert r.status_code == 422
        enqueue.assert_not_awaited()


class TestSealed:
    """The refusal that matters most, because of when it is read.

    A sealed node reporting "0 succeeded" would read as a list of missing
    packages rather than a configuration that forbids fetching, and send the
    operator looking upstream for a problem on this side.
    """

    async def test_packages_are_refused_with_the_reason(self) -> None:
        settings.registry.cache_only = True
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            r = await c.post(
                "/api/terrapod/v1/admin/package-cache/warm",
                json={"packages": [{"ecosystem": "pypi", "name": "requests"}]},
            )
        assert r.status_code == 409
        assert "sealed" in r.json()["detail"].lower()

    async def test_images_are_refused_too(self) -> None:
        settings.registry.cache_only = True
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            r = await c.post("/api/terrapod/v1/admin/oci/warm", json={"images": ["quay.io/x/y:1"]})
        assert r.status_code == 409


class TestImageReferenceParsing:
    """A registry host may carry a port, which looks exactly like a tag."""

    @patch("terrapod.api.routers.cache_warm.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.services.warm_ahead.create_job", new_callable=AsyncMock)
    @pytest.mark.parametrize(
        ("reference", "name", "tag"),
        [
            ("quay.io/ansible/awx-ee:24.6.1", "quay.io/ansible/awx-ee", "24.6.1"),
            ("quay.io/ansible/awx-ee", "quay.io/ansible/awx-ee", "latest"),
            ("registry.local:5000/team/app", "registry.local:5000/team/app", "latest"),
            ("registry.local:5000/team/app:v2", "registry.local:5000/team/app", "v2"),
        ],
    )
    async def test_it_splits_correctly(
        self, create_job, enqueue, reference: str, name: str, tag: str
    ) -> None:
        create_job.return_value = WarmJob(
            job_id="warm-x", status="queued", total=1, submitted_at="2026-01-01T00:00:00Z"
        )
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            await c.post("/api/terrapod/v1/admin/oci/warm", json={"images": [reference]})

        sent = enqueue.await_args[0][1]["items"][0]
        assert (sent["name"], sent["version"]) == (name, tag)


class TestStatus:
    @patch("terrapod.services.warm_ahead.get_job", new_callable=AsyncMock)
    async def test_it_reports_each_item(self, get_job) -> None:
        """ "Failed" over twenty packages is not something anyone can act on."""
        get_job.return_value = WarmJob(
            job_id="warm-abc",
            status="finished",
            total=2,
            submitted_at="2026-01-01T00:00:00Z",
            completed=2,
            outcomes=[
                ItemOutcome(ecosystem="pypi", ref="requests", ok=True, files=4),
                ItemOutcome(ecosystem="npm", ref="nope", ok=False, detail="404 from upstream"),
            ],
        )
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            r = await c.get("/api/terrapod/v1/admin/warm-jobs/warm-abc")

        attrs = r.json()["data"]["attributes"]
        assert (attrs["succeeded"], attrs["failed"]) == (1, 1)
        assert attrs["outcomes"][1]["detail"] == "404 from upstream"

    @patch("terrapod.services.warm_ahead.get_job", new_callable=AsyncMock)
    async def test_an_unknown_job_is_404(self, get_job) -> None:
        get_job.return_value = None
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            r = await c.get("/api/terrapod/v1/admin/warm-jobs/warm-nope")
        assert r.status_code == 404


class TestOneFailureDoesNotStopTheRest:
    """The report is the deliverable.

    A run that aborts on the first 404 tells the operator about one gap when they
    asked about all of them — and they are about to seal.
    """

    @patch("terrapod.services.warm_ahead._save", new_callable=AsyncMock)
    @patch("terrapod.services.warm_ahead.get_job", new_callable=AsyncMock)
    @patch("terrapod.services.warm_ahead._warm_one", new_callable=AsyncMock)
    async def test_every_item_is_attempted(self, warm_one, get_job, save) -> None:
        job = WarmJob(
            job_id="warm-abc", status="queued", total=3, submitted_at="2026-01-01T00:00:00Z"
        )
        get_job.return_value = job
        warm_one.side_effect = [
            ItemOutcome(ecosystem="pypi", ref="a", ok=True),
            RuntimeError("upstream exploded"),
            ItemOutcome(ecosystem="pypi", ref="c", ok=True),
        ]
        items = [WarmItem(ecosystem="pypi", name=n) for n in ("a", "b", "c")]

        from terrapod.services.warm_ahead import run_job

        with patch("terrapod.db.session.get_db_session"), patch("terrapod.storage.get_storage"):
            await run_job("warm-abc", items)

        assert warm_one.await_count == 3
        assert job.completed == 3
        assert job.succeeded == 2 and job.failed == 1
        assert "upstream exploded" in job.outcomes[1].detail
        assert job.status == "finished"
