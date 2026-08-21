"""OCI Distribution push path (#1408).

Services-api tier. The assertions that matter most are the ones about *refusing*
things: a registry that stores content under a digest it does not hash to, or a
manifest naming a layer it does not hold, produces images that pull cleanly and
fail when someone tries to run them.
"""

import base64
import hashlib
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser
from terrapod.db.session import get_db
from terrapod.services.oci.auth import authenticate_oci
from terrapod.storage import get_storage

_BASE = "http://test"
_REPO = "terrapod/ansible-ee"
_BASIC = {"Authorization": "Basic " + base64.b64encode(b"u:tok").decode()}
_BODY = b"layer-bytes"
_DIGEST = "sha256:" + hashlib.sha256(_BODY).hexdigest()


def _user():
    return AuthenticatedUser(
        email="admin@example.com",
        display_name="Admin",
        roles=["admin"],
        provider_name="local",
        auth_method="session",
    )


def _repository(name=_REPO):
    repo = MagicMock()
    repo.id = uuid.uuid4()
    repo.name = name
    repo.labels = {}
    repo.owner_email = "admin@example.com"
    # Explicit: a MagicMock attribute is truthy, so leaving this unset would
    # send every lookup down the pull-through path.
    repo.upstream = None
    return repo


def _session(offset=0):
    s = MagicMock()
    s.id = uuid.uuid4()
    s.offset = offset
    s.chunk_count = 0
    s.repository_name = _REPO
    return s


def _make_app(storage=None):
    app = create_app()
    app.dependency_overrides[authenticate_oci] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_storage] = lambda: storage or AsyncMock()
    return app


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url=_BASE)


_WRITE = frozenset({"registry:read", "registry:write"})


class TestStartUpload:
    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.upload_service.open_session")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_returns_202_with_a_location_to_send_chunks_to(
        self, get_repo, open_session, caps
    ) -> None:
        get_repo.return_value = _repository()
        session = _session()
        open_session.return_value = session
        caps.return_value = _WRITE

        async with await _client(_make_app()) as c:
            r = await c.post(f"/v2/{_REPO}/blobs/uploads/", headers=_BASIC)

        assert r.status_code == 202
        assert r.headers["Location"] == f"/v2/{_REPO}/blobs/uploads/{session.id}"
        assert r.headers["Docker-Upload-UUID"] == str(session.id)

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.upload_service.mount_blob")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_cross_repo_mount_returns_201_without_transferring_bytes(
        self, get_repo, mount, caps
    ) -> None:
        get_repo.side_effect = [_repository(), _repository("other/base")]
        blob = MagicMock()
        blob.digest = _DIGEST
        mount.return_value = blob
        caps.return_value = _WRITE

        async with await _client(_make_app()) as c:
            r = await c.post(
                f"/v2/{_REPO}/blobs/uploads/?mount={_DIGEST}&from=other/base", headers=_BASIC
            )

        assert r.status_code == 201
        assert r.headers["Docker-Content-Digest"] == _DIGEST

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.upload_service.open_session")
    @patch("terrapod.services.oci.upload_service.mount_blob")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_failed_mount_falls_back_to_an_ordinary_upload(
        self, get_repo, mount, open_session, caps
    ) -> None:
        """The spec requires this: a client must be able to attempt a mount
        without knowing whether it will succeed, so a miss is 'send the bytes',
        not an error."""
        get_repo.side_effect = [_repository(), _repository("other/base")]
        mount.return_value = None
        open_session.return_value = _session()
        caps.return_value = _WRITE

        async with await _client(_make_app()) as c:
            r = await c.post(
                f"/v2/{_REPO}/blobs/uploads/?mount={_DIGEST}&from=other/base", headers=_BASIC
            )

        assert r.status_code == 202

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.upload_service.mount_blob")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_mount_requires_read_on_the_source(self, get_repo, mount, caps) -> None:
        """Without this, knowing a digest would be enough to lift a private
        layer into a repository the caller controls."""
        get_repo.side_effect = [_repository(), _repository("private/base")]
        caps.side_effect = [_WRITE, frozenset()]  # write on target, nothing on source

        async with await _client(_make_app()) as c:
            with patch("terrapod.services.oci.upload_service.open_session") as open_session:
                open_session.return_value = _session()
                r = await c.post(
                    f"/v2/{_REPO}/blobs/uploads/?mount={_DIGEST}&from=private/base", headers=_BASIC
                )

        mount.assert_not_awaited()
        assert r.status_code == 202  # fell back to an ordinary upload


class TestChunks:
    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.upload_service.append_chunk")
    @patch("terrapod.services.oci.upload_service.get_session")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_accepts_a_chunk_and_reports_the_new_range(
        self, get_repo, get_session, append, caps
    ) -> None:
        get_repo.return_value = _repository()
        session = _session(offset=0)
        get_session.return_value = session
        append.return_value = 11
        caps.return_value = _WRITE

        async with await _client(_make_app()) as c:
            r = await c.patch(
                f"/v2/{_REPO}/blobs/uploads/{session.id}",
                headers={**_BASIC, "Content-Range": "0-10"},
                content=_BODY,
            )

        assert r.status_code == 202
        assert r.headers["Range"] == "0-10"  # inclusive

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.upload_service.append_chunk")
    @patch("terrapod.services.oci.upload_service.get_session")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_a_gap_is_rejected_rather_than_silently_corrupting_the_blob(
        self, get_repo, get_session, append, caps
    ) -> None:
        """Accepting an out-of-order chunk produces a blob that fails its digest
        much later, with nothing pointing at the chunk responsible."""
        get_repo.return_value = _repository()
        get_session.return_value = _session(offset=100)
        caps.return_value = _WRITE

        async with await _client(_make_app()) as c:
            r = await c.patch(
                f"/v2/{_REPO}/blobs/uploads/{uuid.uuid4()}",
                headers={**_BASIC, "Content-Range": "500-600"},
                content=b"x",
            )

        assert r.status_code == 400
        assert r.json()["errors"][0]["code"] == "BLOB_UPLOAD_INVALID"
        append.assert_not_awaited()

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.upload_service.get_session")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_unknown_and_malformed_session_ids_are_indistinguishable(
        self, get_repo, get_session, caps
    ) -> None:
        get_repo.return_value = _repository()
        get_session.return_value = None
        caps.return_value = _WRITE

        async with await _client(_make_app()) as c:
            unknown = await c.patch(
                f"/v2/{_REPO}/blobs/uploads/{uuid.uuid4()}", headers=_BASIC, content=b"x"
            )
            malformed = await c.patch(
                f"/v2/{_REPO}/blobs/uploads/not-a-uuid", headers=_BASIC, content=b"x"
            )

        assert unknown.status_code == malformed.status_code == 404
        assert unknown.json()["errors"][0]["code"] == "BLOB_UPLOAD_UNKNOWN"
        assert malformed.json()["errors"][0]["code"] == "BLOB_UPLOAD_UNKNOWN"


class TestFinishUpload:
    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.upload_service.complete_session")
    @patch("terrapod.services.oci.upload_service.append_chunk")
    @patch("terrapod.services.oci.upload_service.get_session")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_completes_with_201_and_the_blob_location(
        self, get_repo, get_session, append, complete, caps
    ) -> None:
        get_repo.return_value = _repository()
        get_session.return_value = _session()
        blob = MagicMock()
        blob.digest = _DIGEST
        complete.return_value = blob
        caps.return_value = _WRITE

        async with await _client(_make_app()) as c:
            r = await c.put(
                f"/v2/{_REPO}/blobs/uploads/{uuid.uuid4()}?digest={_DIGEST}",
                headers=_BASIC,
                content=_BODY,
            )

        assert r.status_code == 201
        assert r.headers["Location"] == f"/v2/{_REPO}/blobs/{_DIGEST}"
        assert r.headers["Docker-Content-Digest"] == _DIGEST

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.upload_service.complete_session")
    @patch("terrapod.services.oci.upload_service.append_chunk")
    @patch("terrapod.services.oci.upload_service.get_session")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_digest_mismatch_is_refused(
        self, get_repo, get_session, append, complete, caps
    ) -> None:
        """The trust boundary: content-addressed storage that believes asserted
        addresses is not content-addressed."""
        get_repo.return_value = _repository()
        get_session.return_value = _session()
        complete.return_value = None  # the service reports a hash mismatch
        caps.return_value = _WRITE

        async with await _client(_make_app()) as c:
            r = await c.put(
                f"/v2/{_REPO}/blobs/uploads/{uuid.uuid4()}?digest={_DIGEST}",
                headers=_BASIC,
                content=b"different",
            )

        assert r.status_code == 400
        assert r.json()["errors"][0]["code"] == "DIGEST_INVALID"

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.upload_service.get_session")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_missing_digest_parameter_is_refused(self, get_repo, get_session, caps) -> None:
        get_repo.return_value = _repository()
        get_session.return_value = _session()
        caps.return_value = _WRITE

        async with await _client(_make_app()) as c:
            r = await c.put(f"/v2/{_REPO}/blobs/uploads/{uuid.uuid4()}", headers=_BASIC)

        assert r.status_code == 400
        assert r.json()["errors"][0]["code"] == "DIGEST_INVALID"


class TestManifestPut:
    def _manifest_body(self, layers=("sha256:" + "d" * 64,)):
        return json.dumps(
            {
                "schemaVersion": 2,
                "config": {"digest": "sha256:" + "c" * 64},
                "layers": [{"digest": d} for d in layers],
            }
        ).encode()

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.set_tag")
    @patch("terrapod.services.oci.registry_service.store_manifest")
    @patch("terrapod.services.oci.registry_service.missing_referenced_blobs")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_stores_and_tags(self, get_repo, missing, store, set_tag, caps) -> None:
        get_repo.return_value = _repository()
        missing.return_value = []
        store.return_value = MagicMock()
        caps.return_value = _WRITE
        body = self._manifest_body()

        async with await _client(_make_app()) as c:
            r = await c.put(f"/v2/{_REPO}/manifests/latest", headers=_BASIC, content=body)

        assert r.status_code == 201
        # The digest is computed from the bytes, never taken from the client.
        assert r.headers["Docker-Content-Digest"] == "sha256:" + hashlib.sha256(body).hexdigest()
        set_tag.assert_awaited_once()

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.missing_referenced_blobs")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_manifest_naming_an_absent_layer_is_refused(
        self, get_repo, missing, caps
    ) -> None:
        """Otherwise the image pulls cleanly and fails when someone runs it."""
        get_repo.return_value = _repository()
        missing.return_value = ["sha256:" + "d" * 64]
        caps.return_value = _WRITE

        async with await _client(_make_app()) as c:
            r = await c.put(
                f"/v2/{_REPO}/manifests/latest", headers=_BASIC, content=self._manifest_body()
            )

        assert r.status_code == 404
        assert r.json()["errors"][0]["code"] == "MANIFEST_BLOB_UNKNOWN"

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_digest_reference_must_match_the_body(self, get_repo, caps) -> None:
        get_repo.return_value = _repository()
        caps.return_value = _WRITE
        wrong = "sha256:" + "f" * 64

        async with await _client(_make_app()) as c:
            r = await c.put(
                f"/v2/{_REPO}/manifests/{wrong}", headers=_BASIC, content=self._manifest_body()
            )

        assert r.status_code == 400
        assert r.json()["errors"][0]["code"] == "DIGEST_INVALID"

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_non_json_body_is_manifest_invalid_not_a_500(self, get_repo, caps) -> None:
        get_repo.return_value = _repository()
        caps.return_value = _WRITE

        async with await _client(_make_app()) as c:
            r = await c.put(f"/v2/{_REPO}/manifests/latest", headers=_BASIC, content=b"not json")

        assert r.status_code == 400
        assert r.json()["errors"][0]["code"] == "MANIFEST_INVALID"
