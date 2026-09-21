"""The four credential-less endpoints take a capability, not a guessable id.

GHSA-r9v9-24fv-jxm2 / GHSA-63m3-56rj-qqfh. Each of these is reached by a client
that sends no `Authorization` header, so each used to treat the resource UUID in
the path as though it were a secret. It is not: run and configuration-version
ids appear in UI links, audit rows, notification payloads and PR comments.

These tests drive the real routes through the ASGI app rather than calling the
helper, because the bug was never in the helper — it was in which endpoints
remembered to use one.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.auth import capability_urls as cu
from terrapod.db.session import get_db

_BASE = "http://test"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _ca():
    """A deterministic CA, so minting works without a database."""
    from terrapod.auth.ca import CertificateAuthority

    key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    real = CertificateAuthority.generate()
    ca = CertificateAuthority(ca_cert=real.ca_cert, ca_key=key)
    with patch("terrapod.auth.ca.get_ca", return_value=ca):
        yield


@pytest.fixture(autouse=True)
def _no_lifespan():
    with (
        patch("terrapod.api.app.init_storage", new_callable=AsyncMock),
        patch("terrapod.api.app.init_redis"),
        patch("terrapod.api.app.init_db"),
    ):
        yield


def _app(mock_db=None):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: mock_db or AsyncMock()
    return app


async def _get(app, path):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=_BASE) as c:
        return await c.get(path)


async def _put(app, path, content=b"x"):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=_BASE) as c:
        return await c.put(path, content=content)


class TestABareIdNoLongerReadsALog:
    """The vulnerability itself: a run id, unauthenticated, read the log."""

    async def test_a_bare_run_id_without_a_credential_is_refused(self):
        run_id = str(uuid.uuid4())
        app = _app()
        resp = await _get(app, f"/api/v2/plans/{run_id}/log")
        assert resp.status_code == 401

    async def test_the_same_for_the_apply_log(self):
        run_id = str(uuid.uuid4())
        app = _app()
        resp = await _get(app, f"/api/v2/applies/{run_id}/log")
        assert resp.status_code == 401

    async def test_the_same_on_the_canonical_tfe_prefix(self):
        # The alias is what the CLI happens to use today; both are mounted, and
        # a check that covers only one prefix is the shape of bug the prefix
        # convention exists to prevent.
        run_id = str(uuid.uuid4())
        app = _app()
        resp = await _get(app, f"/api/tfe/v2/plans/{run_id}/log")
        assert resp.status_code == 401

    async def test_a_forged_capability_is_not_retried_as_an_id(self):
        # It must 404, never fall through to the authenticated path and 401 —
        # and never be served.
        cap = cu.mint(cu.KIND_PLAN_LOG, str(uuid.uuid4()))
        body, _, _ = cap[len("cap-") :].partition(".")
        app = _app()
        resp = await _get(app, f"/api/v2/plans/cap-{body}.forged/log")
        assert resp.status_code == 404

    async def test_a_capability_for_the_other_phase_is_refused(self):
        run_id = str(uuid.uuid4())
        cap = cu.mint(cu.KIND_PLAN_LOG, run_id)
        app = _app()
        resp = await _get(app, f"/api/v2/applies/{cap}/log")
        assert resp.status_code == 404


class TestAValidCapabilityStillServes:
    """The CLI and the web log viewer both poll with no credential."""

    async def test_a_plan_capability_reaches_the_log_lookup(self):
        run_id = uuid.uuid4()
        run = MagicMock()
        run.id = run_id
        run.workspace_id = uuid.uuid4()
        run.status = "planned"
        cap = cu.mint(cu.KIND_PLAN_LOG, str(run_id))
        app = _app()
        with (
            patch(
                "terrapod.api.routers.runs.run_service.get_run",
                new=AsyncMock(return_value=run),
            ),
            patch("terrapod.api.routers.runs._serve_log", new=AsyncMock()) as serve,
        ):
            serve.return_value = MagicMock(status_code=200)
            await _get(app, f"/api/v2/plans/{cap}/log")
        # It got as far as serving, which is what the credential-less client
        # needs — and it resolved the right run.
        assert serve.await_count == 1
        assert serve.await_args.kwargs["run"] is run


class TestTheMintedUrlsCarryACapability:
    """The serializers must hand out a capability, or the endpoint is unusable."""

    def test_the_plan_log_url_is_not_a_bare_run_id(self):
        from terrapod.api.routers import runs as runs_router

        run = MagicMock()
        run.id = uuid.uuid4()
        run.status = "planned"
        run.has_json_output = False
        run.resource_additions = None
        attrs = runs_router._plan_json(run)["data"]["attributes"]
        url = attrs["log-read-url"]
        assert str(run.id) not in url
        segment = url.split("/plans/")[1].split("/log")[0]
        assert cu.verify(segment, expect_kind=cu.KIND_PLAN_LOG) == str(run.id)

    def test_the_apply_log_url_is_not_a_bare_run_id(self):
        from terrapod.api.routers import runs as runs_router

        run = MagicMock()
        run.id = uuid.uuid4()
        run.status = "applied"
        attrs = runs_router._apply_json(run)["data"]["attributes"]
        url = attrs["log-read-url"]
        assert str(run.id) not in url
        segment = url.split("/applies/")[1].split("/log")[0]
        assert cu.verify(segment, expect_kind=cu.KIND_APPLY_LOG) == str(run.id)

    def test_the_two_phases_do_not_share_one_capability(self):
        from terrapod.api.routers import runs as runs_router

        run = MagicMock()
        run.id = uuid.uuid4()
        run.status = "applied"
        run.has_json_output = False
        run.resource_additions = None
        plan_url = runs_router._plan_json(run)["data"]["attributes"]["log-read-url"]
        apply_url = runs_router._apply_json(run)["data"]["attributes"]["log-read-url"]
        plan_seg = plan_url.split("/plans/")[1].split("/log")[0]
        assert cu.verify(plan_seg, expect_kind=cu.KIND_APPLY_LOG) is None
        assert plan_seg not in apply_url


class TestUploadsAreNotOpenToAGuessedId:
    """A configuration upload is arbitrary Terraform the next run executes."""

    async def test_a_bare_cv_id_without_a_credential_is_refused(self):
        app = _app()
        resp = await _put(app, f"/api/v2/configuration-versions/cv-{uuid.uuid4()}/upload")
        assert resp.status_code == 401

    async def test_a_bare_state_version_id_without_a_credential_is_refused(self):
        app = _app()
        resp = await _put(app, f"/api/v2/state-versions/sv-{uuid.uuid4()}/content")
        assert resp.status_code == 401

    async def test_the_json_state_endpoint_is_checked_too(self):
        # It discards the body today, so it is the one most likely to be left
        # open; an endpoint that answers 200 to any id also confirms ids.
        app = _app()
        resp = await _put(app, f"/api/v2/state-versions/sv-{uuid.uuid4()}/json-content")
        assert resp.status_code == 401

    async def test_a_cv_capability_is_not_accepted_by_the_state_upload(self):
        cap = cu.mint(cu.KIND_CV_UPLOAD, str(uuid.uuid4()))
        app = _app()
        resp = await _put(app, f"/api/v2/state-versions/{cap}/content")
        assert resp.status_code == 404


class TestPlanJsonOutputTakesACredential:
    """go-tfe authenticates this one, so it needs no capability — and the plan
    JSON is the full resolved plan, secrets included."""

    async def test_an_unauthenticated_read_is_refused(self):
        app = _app()
        resp = await _get(app, f"/api/v2/plans/{uuid.uuid4()}/json-output")
        assert resp.status_code == 401


class TestAReaderIsNotHandedAWriteCapability:
    """The escalation the `with_upload_capability` flag exists to prevent.

    An upload capability IS the authorisation to upload. Minting one in the
    shared serializer would put a live one in every read response, so a user
    with only read-metadata could list state versions, lift the capability, and
    overwrite the workspace's state — read becoming write through a field meant
    to be informational.
    """

    def _sv(self):
        sv = MagicMock()
        sv.id = uuid.uuid4()
        sv.serial = 1
        sv.lineage = "abc"
        sv.md5 = ""
        sv.state_size = 0
        sv.created_at = None
        sv.created_by = ""
        sv.run_id = None
        return sv

    def _cv(self):
        cv = MagicMock()
        cv.id = uuid.uuid4()
        cv.workspace_id = uuid.uuid4()
        cv.source = "tfe-api"
        cv.status = "pending"
        cv.auto_queue_runs = True
        cv.speculative = False
        cv.created_at = None
        return cv

    def test_reading_a_state_version_yields_no_upload_capability(self):
        from terrapod.api.routers.tfe_v2 import _state_version_json

        sv = self._sv()
        attrs = _state_version_json(sv)["data"]["attributes"]
        for field in ("hosted-state-upload-url", "hosted-json-state-upload-url"):
            segment = attrs[field].split("/state-versions/")[1].rsplit("/", 1)[0]
            assert not cu.looks_like_capability(segment), (
                f"{field} handed a reader an upload capability"
            )
            assert cu.verify(segment, expect_kind=cu.KIND_SV_UPLOAD) is None

    def test_creating_a_state_version_does_yield_one(self):
        # The other half: the client that just created it must be able to
        # upload, or state pushes stop working entirely.
        from terrapod.api.routers.tfe_v2 import _state_version_json

        sv = self._sv()
        attrs = _state_version_json(sv, with_upload_capability=True)["data"]["attributes"]
        segment = attrs["hosted-state-upload-url"].split("/state-versions/")[1].rsplit("/", 1)[0]
        assert cu.verify(segment, expect_kind=cu.KIND_SV_UPLOAD) == str(sv.id)

    def test_the_download_url_never_carries_one(self):
        # Download is credential-checked already; a capability there would be a
        # second, weaker way in.
        from terrapod.api.routers.tfe_v2 import _state_version_json

        sv = self._sv()
        for attrs in (
            _state_version_json(sv)["data"]["attributes"],
            _state_version_json(sv, with_upload_capability=True)["data"]["attributes"],
        ):
            segment = (
                attrs["hosted-state-download-url"].split("/state-versions/")[1].rsplit("/", 1)[0]
            )
            assert not cu.looks_like_capability(segment)

    def test_reading_a_configuration_version_yields_no_upload_capability(self):
        from terrapod.api.routers.config_versions import _cv_json

        cv = self._cv()
        segment = (
            _cv_json(cv)["data"]["attributes"]["upload-url"]
            .split("/configuration-versions/")[1]
            .rsplit("/", 1)[0]
        )
        assert not cu.looks_like_capability(segment)
        assert cu.verify(segment, expect_kind=cu.KIND_CV_UPLOAD) is None

    def test_creating_a_configuration_version_does_yield_one(self):
        from terrapod.api.routers.config_versions import _cv_json

        cv = self._cv()
        segment = (
            _cv_json(cv, with_upload_capability=True)["data"]["attributes"]["upload-url"]
            .split("/configuration-versions/")[1]
            .rsplit("/", 1)[0]
        )
        assert cu.verify(segment, expect_kind=cu.KIND_CV_UPLOAD) == str(cv.id)
