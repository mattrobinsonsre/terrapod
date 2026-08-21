"""Behaviours the OCI conformance suite caught (#1408).

Each of these was a real failure against `opencontainers/distribution-spec`'s
conformance suite, found by running it rather than by reading the spec. They are
pinned here so they get fast feedback on every run, not only when the suite
itself is exercised.

None of them affected `docker push`/`pull`, which worked throughout — that is
the point. A registry can serve the client you happen to test with and still be
wrong for the next one.
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
from terrapod.services.oci.registry_service import referenced_digests
from terrapod.storage import get_storage

_BASE = "http://test"
_REPO = "ns/repo"
_BASIC = {"Authorization": "Basic " + base64.b64encode(b"u:tok").decode()}
_WRITE = frozenset({"registry:read", "registry:write"})


def _user():
    return AuthenticatedUser(
        email="a@b.c",
        display_name=None,
        roles=["admin"],
        provider_name="local",
        auth_method="session",
    )


def _repository():
    r = MagicMock()
    r.id = uuid.uuid4()
    r.name = _REPO
    r.labels = {}
    r.owner_email = "a@b.c"
    r.upstream = None
    return r


def _app(storage=None):
    app = create_app()
    app.dependency_overrides[authenticate_oci] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_storage] = lambda: storage or AsyncMock()
    return app


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url=_BASE)


class TestManifestDigestAlgorithm:
    """The manifest digest was hardcoded to sha256.

    A manifest referenced by a sha512 digest could therefore never match: the
    registry hashed with sha256, compared against the client's sha512, and
    rejected every push as a mismatch while reporting a digest the client had
    not asked about.
    """

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.set_tag")
    @patch("terrapod.services.oci.registry_service.store_manifest")
    @patch("terrapod.services.oci.registry_service.missing_referenced_blobs")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_sha512_reference_is_hashed_with_sha512(
        self, get_repo, missing, store, _set_tag, caps
    ) -> None:
        get_repo.return_value = _repository()
        missing.return_value = []
        store.return_value = MagicMock()
        caps.return_value = _WRITE
        body = json.dumps({"schemaVersion": 2, "layers": []}).encode()
        digest = "sha512:" + hashlib.sha512(body).hexdigest()

        async with await _client(_app()) as c:
            r = await c.put(f"/v2/{_REPO}/manifests/{digest}", headers=_BASIC, content=body)

        assert r.status_code == 201
        assert r.headers["Docker-Content-Digest"] == digest

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.set_tag")
    @patch("terrapod.services.oci.registry_service.store_manifest")
    @patch("terrapod.services.oci.registry_service.missing_referenced_blobs")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_a_tag_reference_still_defaults_to_sha256(
        self, get_repo, missing, store, _set_tag, caps
    ) -> None:
        """A tag names no algorithm, so the default must not change."""
        get_repo.return_value = _repository()
        missing.return_value = []
        store.return_value = MagicMock()
        caps.return_value = _WRITE
        body = json.dumps({"schemaVersion": 2, "layers": []}).encode()

        async with await _client(_app()) as c:
            r = await c.put(f"/v2/{_REPO}/manifests/latest", headers=_BASIC, content=body)

        assert r.headers["Docker-Content-Digest"] == "sha256:" + hashlib.sha256(body).hexdigest()


class TestOCISubjectHeader:
    """`OCI-Subject` was not echoed, so a client could not tell whether the
    registry had understood the attachment or stored an orphan — and concluded
    referrers were unsupported."""

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.set_tag")
    @patch("terrapod.services.oci.registry_service.store_manifest")
    @patch("terrapod.services.oci.registry_service.missing_referenced_blobs")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_subject_is_echoed(self, get_repo, missing, store, _set_tag, caps) -> None:
        get_repo.return_value = _repository()
        missing.return_value = []
        store.return_value = MagicMock()
        caps.return_value = _WRITE
        subject = "sha256:" + "b" * 64
        body = json.dumps(
            {"schemaVersion": 2, "layers": [], "subject": {"digest": subject}}
        ).encode()

        async with await _client(_app()) as c:
            r = await c.put(f"/v2/{_REPO}/manifests/latest", headers=_BASIC, content=body)

        assert r.headers["OCI-Subject"] == subject

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.set_tag")
    @patch("terrapod.services.oci.registry_service.store_manifest")
    @patch("terrapod.services.oci.registry_service.missing_referenced_blobs")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_absent_subject_yields_no_header(
        self, get_repo, missing, store, _set_tag, caps
    ) -> None:
        get_repo.return_value = _repository()
        missing.return_value = []
        store.return_value = MagicMock()
        caps.return_value = _WRITE

        async with await _client(_app()) as c:
            r = await c.put(
                f"/v2/{_REPO}/manifests/latest",
                headers=_BASIC,
                content=json.dumps({"schemaVersion": 2, "layers": []}).encode(),
            )

        assert "OCI-Subject" not in r.headers


class TestNonDistributableLayers:
    """Foreign layers are referenced by digest but deliberately never uploaded.

    Requiring their presence rejected every image carrying one — Windows base
    images most obviously — with MANIFEST_BLOB_UNKNOWN for content the registry
    is not permitted to hold.
    """

    def test_foreign_layers_are_not_required_to_be_present(self) -> None:
        digests = referenced_digests(
            {
                "layers": [
                    {
                        "digest": "sha256:" + "a" * 64,
                        "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    },
                    {
                        "digest": "sha256:" + "f" * 64,
                        "mediaType": "application/vnd.oci.image.layer.nondistributable.v1.tar+gzip",
                    },
                    {
                        "digest": "sha256:" + "e" * 64,
                        "mediaType": "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip",
                    },
                ]
            }
        )
        assert digests == ["sha256:" + "a" * 64]

    def test_ordinary_layers_are_still_required(self) -> None:
        digests = referenced_digests(
            {
                "layers": [
                    {
                        "digest": "sha256:" + "a" * 64,
                        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    }
                ]
            }
        )
        assert digests == ["sha256:" + "a" * 64]


class TestReferrers:
    """Referrers answered 404 for a repository nobody had pushed to.

    The spec wants an empty index: 404 tells a client the endpoint is
    unsupported and sends it down a fallback path, where an empty index says
    "supported, nothing attached" — which is the truth.
    """

    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_unknown_repository_yields_an_empty_index_not_404(self, get_repo) -> None:
        get_repo.return_value = None

        async with await _client(_app()) as c:
            r = await c.get(f"/v2/{_REPO}/referrers/sha256:{'a' * 64}", headers=_BASIC)

        assert r.status_code == 200
        assert r.json()["manifests"] == []

    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_the_media_type_is_the_index_type(self, get_repo) -> None:
        """Clients check this to decide whether the response is a real referrers
        index; `application/json` reads as 'not supported'."""
        get_repo.return_value = None

        async with await _client(_app()) as c:
            r = await c.get(f"/v2/{_REPO}/referrers/sha256:{'a' * 64}", headers=_BASIC)

        assert r.headers["content-type"].startswith("application/vnd.oci.image.index.v1+json")


class TestOutOfOrderChunkOnPut:
    """The offset check ran on PATCH but not on PUT.

    A client may deliver the final chunk with the PUT that closes the upload, so
    an out-of-order chunk slipped past the check entirely on that path.
    """

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.upload_service.append_chunk")
    @patch("terrapod.services.oci.upload_service.get_session")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_put_rejects_a_gap(self, get_repo, get_session, append, caps) -> None:
        get_repo.return_value = _repository()
        session = MagicMock()
        session.id = uuid.uuid4()
        session.offset = 100
        get_session.return_value = session
        caps.return_value = _WRITE

        async with await _client(_app()) as c:
            r = await c.put(
                f"/v2/{_REPO}/blobs/uploads/{session.id}?digest=sha256:{'a' * 64}",
                headers={**_BASIC, "Content-Range": "500-600"},
                content=b"x",
            )

        assert r.status_code == 416
        append.assert_not_awaited()
