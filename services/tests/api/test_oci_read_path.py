"""OCI Distribution read path (#1408).

Services-api tier: the router with the DB and storage mocked.

The negative paths carry most of the weight. A registry that leaks which
repositories exist, or answers a container client in the wrong error shape, is
broken in ways that only show up as an unhelpful message on someone's
`docker pull` — so those are asserted explicitly rather than assumed.
"""

import base64
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser
from terrapod.db.session import get_db
from terrapod.services.oci.auth import authenticate_oci
from terrapod.storage import get_storage

_BASE = "http://test"
_DIGEST = "sha256:" + "a" * 64
_REPO = "terrapod/ansible-ee"

#: Any username; the password is the credential (the GHCR pattern).
_BASIC = {"Authorization": "Basic " + base64.b64encode(b"anything:tok.tpod.secret").decode()}


def _user(roles=None):
    return AuthenticatedUser(
        email="admin@example.com",
        display_name="Admin",
        roles=roles or ["admin"],
        provider_name="local",
        auth_method="session",
    )


def _repository(name=_REPO):
    repo = MagicMock()
    repo.id = uuid.uuid4()
    repo.name = name
    repo.labels = {}
    repo.owner_email = "admin@example.com"
    return repo


def _manifest(digest=_DIGEST):
    m = MagicMock()
    m.digest = digest
    m.media_type = "application/vnd.oci.image.manifest.v1+json"
    m.size = 1234
    m.storage_key = "oci/manifests/sha256/" + "a" * 64
    return m


def _blob(digest=_DIGEST):
    b = MagicMock()
    b.digest = digest
    b.size = 987654321
    b.storage_key = "oci/blobs/sha256/" + "a" * 64
    return b


def _make_app(user=_user(), storage=None):
    app = create_app()
    if user is not None:
        app.dependency_overrides[authenticate_oci] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_storage] = lambda: storage or AsyncMock()
    return app


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url=_BASE)


# ── the version check ──────────────────────────────────────────────────────


class TestVersionCheck:
    async def test_authenticated_request_succeeds_with_the_version_header(self) -> None:
        async with await _client(_make_app()) as c:
            r = await c.get("/v2/", headers=_BASIC)
        assert r.status_code == 200
        assert r.headers["Docker-Distribution-API-Version"] == "registry/2.0"

    async def test_unauthenticated_is_401_with_a_basic_challenge(self) -> None:
        """Docker will not send credentials without a challenge it recognises,
        so a 401 without this header makes the registry unusable rather than
        merely unauthorised."""
        app = create_app()  # no auth override — the real dependency runs
        async with await _client(app) as c:
            r = await c.get("/v2/")
        assert r.status_code == 401
        assert r.headers["WWW-Authenticate"].startswith("Basic ")

    async def test_unauthenticated_body_is_the_oci_envelope_not_the_house_shape(self) -> None:
        app = create_app()
        async with await _client(app) as c:
            r = await c.get("/v2/")
        body = r.json()
        assert "errors" in body and body["errors"][0]["code"] == "UNAUTHORIZED"
        assert "detail" not in body


# ── manifests ──────────────────────────────────────────────────────────────


class TestManifests:
    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.resolve_manifest")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_get_by_tag_returns_body_and_content_digest(
        self, get_repo, resolve, caps
    ) -> None:
        get_repo.return_value = _repository()
        resolve.return_value = _manifest()
        caps.return_value = frozenset({"registry:read"})
        storage = AsyncMock()
        storage.get.return_value = b'{"schemaVersion":2}'

        async with await _client(_make_app(storage=storage)) as c:
            r = await c.get(f"/v2/{_REPO}/manifests/latest", headers=_BASIC)

        assert r.status_code == 200
        assert r.headers["Docker-Content-Digest"] == _DIGEST
        assert r.headers["content-type"] == "application/vnd.oci.image.manifest.v1+json"
        assert r.content == b'{"schemaVersion":2}'

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.resolve_manifest")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_head_agrees_with_get_on_every_header(self, get_repo, resolve, caps) -> None:
        """A client HEADs to decide whether it needs the body; disagreement on
        the digest or media type sends it down the wrong path."""
        get_repo.return_value = _repository()
        resolve.return_value = _manifest()
        caps.return_value = frozenset({"registry:read"})
        storage = AsyncMock()
        storage.get.return_value = b"{}"

        async with await _client(_make_app(storage=storage)) as c:
            head = await c.head(f"/v2/{_REPO}/manifests/latest", headers=_BASIC)
            get = await c.get(f"/v2/{_REPO}/manifests/latest", headers=_BASIC)

        for header in ("Docker-Content-Digest", "content-type", "Content-Length"):
            assert head.headers[header] == get.headers[header]
        assert head.content == b""

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.resolve_manifest")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_unknown_manifest_is_manifest_unknown(self, get_repo, resolve, caps) -> None:
        get_repo.return_value = _repository()
        resolve.return_value = None
        caps.return_value = frozenset({"registry:read"})

        async with await _client(_make_app()) as c:
            r = await c.get(f"/v2/{_REPO}/manifests/nope", headers=_BASIC)

        assert r.status_code == 404
        assert r.json()["errors"][0]["code"] == "MANIFEST_UNKNOWN"

    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_unknown_repository_is_name_unknown(self, get_repo) -> None:
        get_repo.return_value = None
        async with await _client(_make_app()) as c:
            r = await c.get(f"/v2/{_REPO}/manifests/latest", headers=_BASIC)
        assert r.status_code == 404
        assert r.json()["errors"][0]["code"] == "NAME_UNKNOWN"

    async def test_invalid_repository_name_is_name_invalid(self) -> None:
        """The only failure reported distinctly, because a malformed name
        cannot leak whether anything exists."""
        async with await _client(_make_app()) as c:
            r = await c.get("/v2/Invalid_UPPER/manifests/latest", headers=_BASIC)
        assert r.status_code == 400
        assert r.json()["errors"][0]["code"] == "NAME_INVALID"

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_without_read_capability_is_404_not_403(self, get_repo, caps) -> None:
        """Do not confirm the existence of a repository the caller cannot see —
        the same choice the module registry makes."""
        get_repo.return_value = _repository()
        caps.return_value = frozenset()

        async with await _client(_make_app(user=_user(roles=["everyone"]))) as c:
            r = await c.get(f"/v2/{_REPO}/manifests/latest", headers=_BASIC)

        assert r.status_code == 404
        assert r.json()["errors"][0]["code"] == "NAME_UNKNOWN"

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.resolve_manifest")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_nested_name_containing_manifests_resolves(self, get_repo, resolve, caps) -> None:
        """Greedy path matching must take the LAST suffix, so a repository
        genuinely called `org/manifests/thing` still works."""
        get_repo.return_value = _repository("org/manifests/thing")
        resolve.return_value = _manifest()
        caps.return_value = frozenset({"registry:read"})
        storage = AsyncMock()
        storage.get.return_value = b"{}"

        async with await _client(_make_app(storage=storage)) as c:
            r = await c.get("/v2/org/manifests/thing/manifests/v1", headers=_BASIC)

        assert r.status_code == 200
        assert get_repo.call_args[0][1] == "org/manifests/thing"


# ── blobs ──────────────────────────────────────────────────────────────────


class TestBlobs:
    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.get_repository_blob")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_get_redirects_rather_than_proxying_the_bytes(
        self, get_repo, get_blob, caps
    ) -> None:
        """A layer is hundreds of MB; proxying it would put that through the API
        and the BFF for no reason."""
        get_repo.return_value = _repository()
        get_blob.return_value = _blob()
        caps.return_value = frozenset({"registry:read"})
        storage = AsyncMock()
        storage.presigned_get_url.return_value = MagicMock(url="https://storage.test/blob?sig=x")

        async with await _client(_make_app(storage=storage)) as c:
            r = await c.get(f"/v2/{_REPO}/blobs/{_DIGEST}", headers=_BASIC, follow_redirects=False)

        assert r.status_code == 307
        assert r.headers["location"] == "https://storage.test/blob?sig=x"

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.get_repository_blob")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_head_answers_from_metadata_without_a_redirect(
        self, get_repo, get_blob, caps
    ) -> None:
        """HEAD asks whether it exists and how big it is — both already known,
        so a round trip to object storage would be wasted."""
        get_repo.return_value = _repository()
        get_blob.return_value = _blob()
        caps.return_value = frozenset({"registry:read"})
        storage = AsyncMock()

        async with await _client(_make_app(storage=storage)) as c:
            r = await c.head(f"/v2/{_REPO}/blobs/{_DIGEST}", headers=_BASIC, follow_redirects=False)

        assert r.status_code == 200
        assert r.headers["Content-Length"] == "987654321"
        assert r.headers["Docker-Content-Digest"] == _DIGEST
        storage.presigned_get_url.assert_not_awaited()

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.get_repository_blob")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_blob_not_linked_to_this_repository_is_blob_unknown(
        self, get_repo, get_blob, caps
    ) -> None:
        """The link table is the access check: knowing a digest must not let
        anyone pull it through a repository they happen to have read on."""
        get_repo.return_value = _repository()
        get_blob.return_value = None
        caps.return_value = frozenset({"registry:read"})

        async with await _client(_make_app()) as c:
            r = await c.get(f"/v2/{_REPO}/blobs/{_DIGEST}", headers=_BASIC)

        assert r.status_code == 404
        assert r.json()["errors"][0]["code"] == "BLOB_UNKNOWN"

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_malformed_digest_is_rejected_before_any_lookup(self, get_repo, caps) -> None:
        get_repo.return_value = _repository()
        caps.return_value = frozenset({"registry:read"})

        async with await _client(_make_app()) as c:
            r = await c.get(f"/v2/{_REPO}/blobs/sha256:nothex", headers=_BASIC)

        assert r.status_code == 404
        assert r.json()["errors"][0]["code"] == "BLOB_UNKNOWN"


# ── tags ───────────────────────────────────────────────────────────────────


class TestTags:
    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.list_tags")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_lists_tags(self, get_repo, list_tags, caps) -> None:
        get_repo.return_value = _repository()
        list_tags.return_value = ["2.16", "2.17", "latest"]
        caps.return_value = frozenset({"registry:read"})

        async with await _client(_make_app()) as c:
            r = await c.get(f"/v2/{_REPO}/tags/list", headers=_BASIC)

        assert r.status_code == 200
        assert r.json() == {"name": _REPO, "tags": ["2.16", "2.17", "latest"]}

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.list_tags")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_pagination_parameters_are_passed_through(
        self, get_repo, list_tags, caps
    ) -> None:
        get_repo.return_value = _repository()
        list_tags.return_value = []
        caps.return_value = frozenset({"registry:read"})

        async with await _client(_make_app()) as c:
            await c.get(f"/v2/{_REPO}/tags/list?n=2&last=2.16", headers=_BASIC)

        assert list_tags.call_args.kwargs == {"limit": 2, "last": "2.16"}

    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.list_tags")
    @patch("terrapod.services.oci.registry_service.get_repository")
    async def test_a_malformed_n_is_ignored_rather_than_failing_the_pull(
        self, get_repo, list_tags, caps
    ) -> None:
        get_repo.return_value = _repository()
        list_tags.return_value = []
        caps.return_value = frozenset({"registry:read"})

        async with await _client(_make_app()) as c:
            r = await c.get(f"/v2/{_REPO}/tags/list?n=banana", headers=_BASIC)

        assert r.status_code == 200
        assert list_tags.call_args.kwargs["limit"] is None
