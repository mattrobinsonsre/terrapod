"""OCI upload sessions are scoped to their repository (GHSA-mhhr-896g-4p33).

Every session handler authorised the repository in the PATH and then loaded the
session by **id alone**. So a caller with write on a repository of their own, who
learned another session's id, could append to it, read its progress, cancel it,
or complete it into their own repository — the blob is written wherever the path
says. The id is not a secret: it travels in the `Location` header of every chunk
response.

Each test drives the route with a real request, because the gate is inside the
handler and a test calling `_open_session` directly would pass whether or not the
handlers were changed to pass it a repository.
"""

import base64
import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser
from terrapod.db.session import get_db
from terrapod.services.oci.auth import authenticate_oci
from terrapod.storage import get_storage

_BASE = "http://test"
_MINE = "alice/app"
_THEIRS = "bob/app"
_BASIC = {"Authorization": "Basic " + base64.b64encode(b"u:tok").decode()}
_BODY = b"layer-bytes"
_DIGEST = "sha256:" + hashlib.sha256(_BODY).hexdigest()
_WRITE = frozenset({"registry:read", "registry:write"})


def _user():
    return AuthenticatedUser(
        email="alice@example.com",
        display_name="Alice",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _repository(name):
    repo = MagicMock()
    repo.id = uuid.uuid4()
    repo.name = name
    repo.labels = {}
    repo.owner_email = "alice@example.com"
    repo.upstream = None
    return repo


def _session(repository_name, offset=0):
    s = MagicMock()
    s.id = uuid.uuid4()
    s.offset = offset
    s.chunk_count = 0
    s.repository_name = repository_name
    return s


def _app(storage=None):
    app = create_app()
    app.dependency_overrides[authenticate_oci] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_storage] = lambda: storage or AsyncMock()
    return app


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url=_BASE)


#: (method, url suffix after the session id, what the handler would do). One per
#: session-bearing route, because the gate had to be added to each of them and a
#: single example would not notice one being missed.
SESSION_ROUTES = [
    ("PATCH", "", "append a chunk"),
    ("PUT", f"?digest={_DIGEST}", "complete the upload"),
    ("GET", "", "read the upload's progress"),
    ("DELETE", "", "cancel the upload"),
]


@pytest.mark.parametrize(("method", "query", "what"), SESSION_ROUTES)
@patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
@patch("terrapod.services.oci.upload_service.discard_session", new_callable=AsyncMock)
@patch("terrapod.services.oci.upload_service.append_chunk", new_callable=AsyncMock)
@patch("terrapod.services.oci.upload_service.complete_session", new_callable=AsyncMock)
@patch("terrapod.services.oci.upload_service.get_session", new_callable=AsyncMock)
@patch("terrapod.services.oci.registry_service.get_repository")
async def test_a_session_from_another_repository_is_not_found(
    get_repo,
    get_session,
    complete_session,
    append_chunk,
    discard_session,
    caps,
    method,
    query,
    what,
):
    """404 BLOB_UPLOAD_UNKNOWN — the same answer as an id that never existed, so
    the route cannot be used to discover which repository owns a session."""
    caps.return_value = _WRITE
    get_repo.return_value = _repository(_MINE)
    theirs = _session(_THEIRS)
    get_session.return_value = theirs
    append_chunk.return_value = len(_BODY)

    async with await _client(_app()) as c:
        url = f"/v2/{_MINE}/blobs/uploads/{theirs.id}{query}"
        resp = await c.request(method, url, headers=_BASIC, content=_BODY)

    assert resp.status_code == 404, f"a cross-repository session could {what}"
    assert resp.json()["errors"][0]["code"] == "BLOB_UPLOAD_UNKNOWN"
    # Nothing was written, completed or reclaimed on someone else's session.
    append_chunk.assert_not_awaited()
    complete_session.assert_not_awaited()
    discard_session.assert_not_awaited()


@pytest.mark.parametrize(("method", "query", "what"), SESSION_ROUTES)
@patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
@patch("terrapod.services.oci.upload_service.discard_session", new_callable=AsyncMock)
@patch("terrapod.services.oci.upload_service.append_chunk", new_callable=AsyncMock)
@patch("terrapod.services.oci.upload_service.complete_session", new_callable=AsyncMock)
@patch("terrapod.services.oci.upload_service.get_session", new_callable=AsyncMock)
@patch("terrapod.services.oci.registry_service.get_repository")
async def test_a_session_for_this_repository_still_works(
    get_repo,
    get_session,
    complete_session,
    append_chunk,
    discard_session,
    caps,
    method,
    query,
    what,
):
    """Without this the refusals above would also pass if the gate simply broke
    every session route."""
    caps.return_value = _WRITE
    get_repo.return_value = _repository(_MINE)
    mine = _session(_MINE)
    get_session.return_value = mine
    append_chunk.return_value = len(_BODY)
    blob = MagicMock()
    blob.digest = _DIGEST
    complete_session.return_value = blob

    async with await _client(_app()) as c:
        url = f"/v2/{_MINE}/blobs/uploads/{mine.id}{query}"
        resp = await c.request(method, url, headers=_BASIC, content=_BODY)

    assert resp.status_code != 404, f"own-repository session refused: cannot {what}"


@patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
@patch("terrapod.services.oci.upload_service.get_session", new_callable=AsyncMock)
@patch("terrapod.services.oci.registry_service.get_repository")
async def test_an_unknown_session_answers_exactly_as_a_foreign_one(get_repo, get_session, caps):
    """The two must be indistinguishable, or the 404 leaks what the 403 would
    have."""
    caps.return_value = _WRITE
    get_repo.return_value = _repository(_MINE)
    sid = uuid.uuid4()

    get_session.return_value = None
    async with await _client(_app()) as c:
        unknown = await c.get(f"/v2/{_MINE}/blobs/uploads/{sid}", headers=_BASIC)

    get_session.return_value = _session(_THEIRS)
    async with await _client(_app()) as c:
        foreign = await c.get(f"/v2/{_MINE}/blobs/uploads/{sid}", headers=_BASIC)

    assert unknown.status_code == foreign.status_code == 404
    assert unknown.json() == foreign.json()
