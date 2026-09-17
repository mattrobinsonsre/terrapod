"""A presigned module upload is finalized when it is next read (#1707).

The documented publishing flow creates a version, then PUTs the tarball to the
version's presigned URL. That PUT goes straight to object storage and never
reaches the API, so the version stayed `pending`, the CLI listing -- which only
serves `uploaded` versions -- stayed empty, and the module still reported
`setup_complete`. Nothing along the way returned an error.
"""

import io
import tarfile
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services import registry_module_service as svc


def _tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b'variable "region" { type = string }\noutput "id" { value = "x" }\n'
        info = tarfile.TarInfo(name="./main.tf")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _module(*versions):
    module = MagicMock()
    module.id = uuid.uuid4()
    module.namespace = "default"
    module.name = "vpc"
    module.provider = "aws"
    module.status = "pending"
    module.versions = list(versions)
    return module


def _version(v: str, status: str):
    mv = MagicMock()
    mv.version = v
    mv.upload_status = status
    mv.inputs = None
    mv.outputs = None
    return mv


def _storage(existing: set[str], body: bytes = b""):
    storage = MagicMock()
    storage.exists = AsyncMock(side_effect=lambda key: key in existing)

    async def _stream(key, chunk_size=256 * 1024):
        yield body

    storage.get_stream = _stream
    return storage


def _db(claimed: bool = True):
    """A session whose row-lock claim returns the version it was asked for,
    or nothing when another read already holds it."""
    db = AsyncMock()

    async def _execute(stmt, *_, **__):
        result = MagicMock()
        wanted = None
        if claimed:
            # The claim selects the version by id; hand back the same object the
            # caller already holds, as populate_existing would.
            wanted = _execute.version
        result.scalar_one_or_none.return_value = wanted
        return result

    _execute.version = None
    db.execute = AsyncMock(side_effect=_execute)
    db._execute = _execute
    savepoint = MagicMock()
    savepoint.__aenter__ = AsyncMock(return_value=None)
    savepoint.__aexit__ = AsyncMock(return_value=False)
    db.begin_nested = MagicMock(return_value=savepoint)
    return db


def _for(db, version):
    """Point the session's claim at `version`."""
    db._execute.version = version
    return db


def _leader(is_leader: bool = True):
    return patch("terrapod.services.ha_role.is_leader", AsyncMock(return_value=is_leader))


class TestFinalizePresignedUploads:
    async def test_a_landed_upload_becomes_installable(self):
        mv = _version("1.0.0", "pending")
        module = _module(mv)
        key = svc.module_tarball_key("default", "vpc", "aws", "1.0.0")
        db = _for(_db(), mv)

        with (
            _leader(),
            patch("terrapod.config.settings.registry.module_interface.enabled", True),
            patch(
                "terrapod.services.module_impact_service.trigger_linked_workspace_runs",
                new_callable=AsyncMock,
            ) as trigger,
        ):
            await svc.finalize_presigned_uploads(db, module, _storage({key}, _tarball()))

        assert mv.upload_status == "uploaded"
        assert module.status == "setup_complete"
        # The interface is parsed too -- and the documented `./` tarball parses.
        assert [i["name"] for i in mv.inputs] == ["region"]
        assert [o["name"] for o in mv.outputs] == ["id"]
        # As the direct upload endpoint does for a new version.
        trigger.assert_awaited_once_with(db, module, "1.0.0")

    async def test_an_upload_that_has_not_arrived_stays_pending(self):
        mv = _version("1.0.0", "pending")
        module = _module(mv)
        with (
            _leader(),
            patch(
                "terrapod.services.module_impact_service.trigger_linked_workspace_runs",
                new_callable=AsyncMock,
            ) as trigger,
        ):
            await svc.finalize_presigned_uploads(_for(_db(), mv), module, _storage(set()))
        assert mv.upload_status == "pending"
        assert module.status == "pending"
        trigger.assert_not_awaited()

    async def test_nothing_pending_touches_nothing(self):
        # The usual read of a fully-published module: no storage lookup at all,
        # not even resolving the storage backend.
        module = _module(_version("1.0.0", "uploaded"))
        with patch("terrapod.storage.get_storage", side_effect=AssertionError("not needed")):
            await svc.finalize_presigned_uploads(AsyncMock(), module)

    async def test_a_storage_error_leaves_the_version_pending(self):
        mv = _version("1.0.0", "pending")
        storage = MagicMock()
        storage.exists = AsyncMock(side_effect=RuntimeError("storage down"))
        with _leader():
            await svc.finalize_presigned_uploads(_for(_db(), mv), _module(mv), storage)
        assert mv.upload_status == "pending"

    async def test_a_concurrent_finalizer_that_lost_the_claim_queues_nothing(self):
        """Two reads see `pending` together; only the one whose conditional
        UPDATE changed the row queues linked-workspace runs."""
        mv = _version("1.0.0", "pending")
        module = _module(mv)
        key = svc.module_tarball_key("default", "vpc", "aws", "1.0.0")
        with (
            _leader(),
            patch(
                "terrapod.services.module_impact_service.trigger_linked_workspace_runs",
                new_callable=AsyncMock,
            ) as trigger,
        ):
            await svc.finalize_presigned_uploads(
                _for(_db(claimed=False), mv), module, _storage({key}, _tarball())
            )
        trigger.assert_not_awaited()

    async def test_a_follower_does_not_write_on_a_read(self):
        mv = _version("1.0.0", "pending")
        module = _module(mv)
        storage = _storage({svc.module_tarball_key("default", "vpc", "aws", "1.0.0")})
        db = _for(_db(), mv)
        with _leader(False):
            await svc.finalize_presigned_uploads(db, module, storage)
        assert mv.upload_status == "pending"
        db.execute.assert_not_awaited()
        storage.exists.assert_not_awaited()

    async def test_a_failure_creating_runs_does_not_fail_the_read(self):
        """Run creation happens in a savepoint, so the caller's session stays
        usable and the CLI listing that triggered it still commits."""
        mv = _version("1.0.0", "pending")
        module = _module(mv)
        key = svc.module_tarball_key("default", "vpc", "aws", "1.0.0")
        db = _for(_db(), mv)
        with (
            _leader(),
            patch(
                "terrapod.services.module_impact_service.trigger_linked_workspace_runs",
                AsyncMock(side_effect=RuntimeError("db error")),
            ),
        ):
            await svc.finalize_presigned_uploads(db, module, _storage({key}, _tarball()))
        assert mv.upload_status == "uploaded"
        db.begin_nested.assert_called_once()


class TestCreatingAVersionDoesNotClaimSuccess:
    async def test_module_is_not_setup_complete_before_the_tarball_lands(self):
        module = _module()
        result = MagicMock()
        result.scalars.return_value.first.return_value = module
        db = AsyncMock()
        db.execute.return_value = result
        db.add = MagicMock()
        storage = MagicMock()
        storage.presigned_put_url = AsyncMock(return_value=MagicMock(url="https://x"))

        await svc.create_module_version(db, storage, module.id, "1.0.0")

        assert module.status == "pending"


class TestTheCliListingServesALandedUpload:
    """The read path that matters: `tofu init` asks this listing."""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.registry_modules.resolve_registry_capabilities_for")
    @patch("terrapod.api.routers.registry_modules.get_module")
    async def test_version_appears_once_its_tarball_exists(self, mock_get_module, mock_caps, *_):
        from httpx import ASGITransport, AsyncClient

        from terrapod.api.app import create_application
        from terrapod.api.dependencies import AuthenticatedUser, get_current_user
        from terrapod.auth.capabilities import caps_for_level
        from terrapod.db.session import get_db

        mv = _version("1.2.0", "pending")
        module = _module(mv)
        module.labels = {}
        module.owner_email = ""
        mock_get_module.return_value = module
        mock_caps.return_value = caps_for_level("read")
        key = svc.module_tarball_key("default", "vpc", "aws", "1.2.0")

        app = create_application()
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
            email="u@example.com",
            display_name="u",
            roles=[],
            provider_name="local",
            auth_method="session",
        )
        app.dependency_overrides[get_db] = lambda: _for(_db(), mv)
        with (
            _leader(),
            patch("terrapod.storage.get_storage", return_value=_storage({key}, _tarball())),
            patch("terrapod.config.settings.registry.module_interface.enabled", False),
            patch(
                "terrapod.services.module_impact_service.trigger_linked_workspace_runs",
                new_callable=AsyncMock,
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(
                    "/api/v2/registry/modules/default/vpc/aws/versions",
                    headers={"Authorization": "Bearer x"},
                )

        assert resp.status_code == 200
        assert resp.json() == {"modules": [{"versions": [{"version": "1.2.0"}]}]}
