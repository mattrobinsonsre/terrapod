"""The per-cache upload routes, driven as requests (#2599).

The service has its own tests — `tests/services/test_package_cache_upload_*.py`
for the coordinates and the serve path, `tests/integration/` for what `admit`
does to a real database. Every one of them passes with these routes deleted,
which is the distinction that matters for a surface whose whole point is that an
operator can reach it.

What this file owns:

* the **route → ecosystem wiring**, which nothing else can see. Six routes each
  call one ecosystem's `upload_coordinates`, and a copy-paste that pointed the
  NuGet route at npm's would store every `.nupkg` at a key no client asks for
  while answering 201 to everyone;
* the status codes an operator and a form act on — 422 for coordinates this
  ecosystem cannot address, 409 for a replacement that was not asked for, 201
  against 200;
* that the response is the same resource the admin listing shows.
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.models import CachedPackageFile
from terrapod.db.session import get_db
from terrapod.services.package_cache import substrate, uploads
from terrapod.storage import get_storage

_BASE = "http://test"
BODY = b"a vetted artifact" * 1000
BODY_SHA = hashlib.sha256(BODY).hexdigest()

#: Every upload route with a form that its own ecosystem accepts. The route
#: expected to be reached is keyed by the ecosystem string that route must pass
#: to `admit` -- which is the wiring this file exists to pin.
FORMS: dict[str, tuple[str, dict[str, str]]] = {
    "pypi": (
        "/api/v1/admin/package-cache/pypi/artifacts",
        {"project": "Flask", "filename": "flask-3.0.0-py3-none-any.whl"},
    ),
    "npm": (
        "/api/v1/admin/package-cache/npm/artifacts",
        {"name": "@scope/pkg", "version": "1.3.0"},
    ),
    "nuget": (
        "/api/v1/admin/package-cache/nuget/artifacts",
        {"package_id": "Newtonsoft.Json", "version": "13.0.3"},
    ),
    "go": (
        "/api/v1/admin/package-cache/go/artifacts",
        {"module": "github.com/foo/bar", "version": "v1.2.3", "suffix": ".zip"},
    ),
    "galaxy": (
        "/api/v1/admin/package-cache/galaxy/artifacts",
        {"namespace": "community", "name": "general", "version": "9.2.0"},
    ),
    "pulumi": (
        "/api/v1/admin/package-cache/pulumi/artifacts",
        {
            "kind": "resource",
            "name": "aws",
            "version": "6.0.0",
            "os": "linux",
            "arch": "amd64",
        },
    ),
}

#: What each route's coordinates must resolve to. Written out rather than
#: computed from `upload_coordinates`, because computing it would make this
#: assert that the function agrees with itself.
EXPECTED_KEY: dict[str, tuple[str, str]] = {
    "pypi": ("flask", "flask-3.0.0-py3-none-any.whl"),
    "npm": ("@scope/pkg", "pkg-1.3.0.tgz"),
    "nuget": ("newtonsoft.json", "newtonsoft.json.13.0.3.nupkg"),
    "go": ("github.com/foo/bar", "v1.2.3.zip"),
    "galaxy": ("community.general", "community-general-9.2.0.tar.gz"),
    "pulumi": ("pulumi-resource-aws", "pulumi-resource-aws-v6.0.0-linux-amd64.tar.gz"),
}


def _user(email="admin@example.com", roles=None):
    return AuthenticatedUser(
        email=email,
        display_name="Admin",
        roles=roles if roles is not None else ["admin"],
        provider_name="local",
        auth_method="session",
    )


def _row(**kw) -> CachedPackageFile:
    defaults = {
        "ecosystem": "pypi",
        "name": "flask",
        "version": "3.0.0",
        "filename": "flask-3.0.0-py3-none-any.whl",
        "storage_key": "cache/packages/pypi/flask/flask-3.0.0-py3-none-any.whl",
        "size": len(BODY),
        "digest": f"sha256:{BODY_SHA}",
        "attestation": substrate.ATTEST_OPERATOR_UPLOAD,
        "uploaded_by": "admin@example.com",
    }
    return CachedPackageFile(**{**defaults, **kw})


class _Storage:
    def __init__(self) -> None:
        self.put: list[str] = []

    async def put_stream(self, key, chunks, content_type="application/octet-stream", metadata=None):
        async for _ in chunks:
            pass
        self.put.append(key)
        return MagicMock()

    async def exists(self, key: str) -> bool:
        return key in self.put


def _make_app(user=None, row=None, storage=None):
    app = create_app()
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user

    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db.execute.return_value = result
    db.get = AsyncMock(return_value=row)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db

    store = storage if storage is not None else _Storage()
    app.dependency_overrides[get_storage] = lambda: store
    return app, db, store


async def _upload(app, path: str, fields: dict[str, str], body: bytes = BODY, **extra):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
        return await c.post(
            path,
            data={**fields, **extra},
            files={"file": ("artifact.bin", body, "application/octet-stream")},
        )


class TestTheRouteReachesItsOwnEcosystem:
    """Six routes, six coordinate schemes, and nothing else can see the wiring.

    Every route answers 201 whatever ecosystem it hands `admit`, so a route
    pointed at the wrong module stores the artifact at a key no client asks for
    and reports success. Driven as requests, with `admit` mocked at the boundary
    below the route.
    """

    @pytest.mark.parametrize("ecosystem", sorted(FORMS))
    async def test_the_ecosystem_and_the_coordinates_are_its_own(self, ecosystem: str) -> None:
        path, fields = FORMS[ecosystem]
        app, _, _ = _make_app(user=_user())

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            admit.return_value = uploads.Admission(record=_row())
            resp = await _upload(app, path, fields)

        assert resp.status_code == 201, resp.text
        kwargs = admit.await_args.kwargs
        assert kwargs["ecosystem"] == ecosystem
        name, filename = EXPECTED_KEY[ecosystem]
        assert (kwargs["coordinates"].name, kwargs["coordinates"].filename) == (name, filename)

    def test_every_upload_route_on_the_router_has_a_form_here(self) -> None:
        """Otherwise a seventh ecosystem's route arrives with no wiring test and
        the loop above keeps passing on six."""
        import inspect

        from fastapi import APIRouter

        from terrapod.api.routers import cache_admin

        # Every router the module exposes, by TYPE rather than by name: each
        # ecosystem's upload route has its own router so it can go with that
        # ecosystem's own flag, so naming one of them would see none of the rest.
        live = {
            r.path
            for _, router in inspect.getmembers(cache_admin, lambda o: isinstance(o, APIRouter))
            for r in router.routes
            if getattr(r, "methods", None) and "POST" in r.methods and r.path.endswith("/artifacts")
        }
        assert len(live) >= 6, f"only {len(live)} upload routes read off the routers"
        covered = {path.removeprefix("/api/v1") for path, _ in FORMS.values()}
        assert not live - covered, f"no form for: {sorted(live - covered)}"


class TestEverythingTheRequestCarriesReachesTheService:
    """The rest of the wiring, which asserting the RESPONSE cannot see.

    These tests mock `admit`, so a response built from a mocked `Admission` says
    nothing about what the route passed in. Drop `replace=replace` from the call
    and every replacement would 409 for ever; drop `reference=reference` and no
    approval record would ever be stored. Both are a presence check standing in
    for an application check unless the kwargs themselves are asserted.
    """

    async def test_replace_reaches_admit(self) -> None:
        path, fields = FORMS["pypi"]
        app, _, _ = _make_app(user=_user())

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            admit.return_value = uploads.Admission(record=_row(), replaced=True)
            await _upload(app, path, fields, replace="true")

        assert admit.await_args.kwargs["replace"] is True

    async def test_omitting_replace_reaches_admit_as_false(self) -> None:
        """The default has to arrive as the refusing one: a form that leaves the
        box unticked must not be read as permission to overwrite."""
        path, fields = FORMS["pypi"]
        app, _, _ = _make_app(user=_user())

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            admit.return_value = uploads.Admission(record=_row())
            await _upload(app, path, fields)

        assert admit.await_args.kwargs["replace"] is False

    async def test_the_reference_reaches_admit(self) -> None:
        path, fields = FORMS["pypi"]
        app, _, _ = _make_app(user=_user())

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            admit.return_value = uploads.Admission(record=_row())
            await _upload(app, path, fields, reference="CHG-4471")

        assert admit.await_args.kwargs["reference"] == "CHG-4471"

    async def test_the_uploaded_bytes_reach_the_file_admit_is_given(self) -> None:
        """End to end through the multipart parse and the re-stream.

        `admit` is handed a PATH, so everything between the request and that file
        -- the part being found, re-streamed to the ephemeral PVC and closed --
        is only asserted by reading it. A truncated or base64-mangled re-stream
        would otherwise reach the digest and be recorded as the artifact.
        """
        import pathlib

        path, fields = FORMS["pypi"]
        app, _, _ = _make_app(user=_user())
        seen: dict[str, bytes] = {}

        async def _capture(_db, _storage, **kwargs):
            seen["bytes"] = pathlib.Path(kwargs["tmp_path"]).read_bytes()
            return uploads.Admission(record=_row())

        with patch.object(uploads, "admit", new=AsyncMock(side_effect=_capture)):
            resp = await _upload(app, path, fields)

        assert resp.status_code == 201, resp.text
        assert seen["bytes"] == BODY

    async def test_the_cap_comes_from_the_upload_service(self, monkeypatch) -> None:
        """So the bound is the one that module documents rather than the generic
        256 MiB default, which is below what this cache already holds -- a
        GPU-flavoured wheel is comfortably larger, and the pull-through path has
        no cap at all.
        """
        monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 16)
        path, fields = FORMS["pypi"]
        app, _, _ = _make_app(user=_user())

        resp = await _upload(app, path, fields, body=b"x" * 64)

        assert resp.status_code == 413
        assert "16-byte cap" in resp.json()["detail"]


class TestAnUploadRouteGoesWithItsOwnEcosystem:
    """Gated FINER than the rest of its namespace, and the contrast is the test.

    The listing, the totals, the evictions and verify are about the cache as a
    whole and go with `registry.package_cache.enabled`. An upload is for ONE
    ecosystem: offering a way to put an artifact into a cache this deployment
    will not serve is exactly the half-present surface the convention forbids.

    `tests/api/test_capability_gating_endpoints.py` asserts the absence
    generically for every capability. What it cannot see is whether turning one
    off took the others with it — unmounting all six would pass it.
    """

    def _paths(self) -> list[str]:
        return [getattr(r, "path", "") for r in create_app().routes]

    def test_turning_one_ecosystem_off_removes_only_its_upload_route(self, monkeypatch) -> None:
        from terrapod.config import settings

        monkeypatch.setattr(settings.registry.package_cache.pypi, "enabled", False)
        paths = self._paths()

        assert "/api/v1/admin/package-cache/pypi/artifacts" not in paths
        assert "/api/v1/admin/package-cache/npm/artifacts" in paths

    def test_the_shared_routes_stay_when_one_ecosystem_is_off(self, monkeypatch) -> None:
        """They are about the cache rather than about an ecosystem, so they are
        not an ecosystem's to switch off."""
        from terrapod.config import settings

        monkeypatch.setattr(settings.registry.package_cache.pypi, "enabled", False)
        paths = self._paths()

        assert "/api/v1/admin/package-cache" in paths
        assert "/api/v1/admin/package-cache/{file_id}/verify" in paths

    def test_every_upload_route_goes_when_the_cache_itself_is_off(self, monkeypatch) -> None:
        from terrapod.config import settings

        monkeypatch.setattr(settings.registry.package_cache, "enabled", False)
        paths = self._paths()

        assert [p for p in paths if p.endswith("/artifacts")] == []


class TestWhatTheOperatorGetsBack:
    async def test_a_first_upload_is_201_and_says_who_uploaded_it(self) -> None:
        path, fields = FORMS["pypi"]
        app, _, _ = _make_app(user=_user())

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            admit.return_value = uploads.Admission(record=_row())
            resp = await _upload(app, path, fields, reference="CHG-4471")

        assert resp.status_code == 201
        attributes = resp.json()["data"]["attributes"]
        # Requirement 1: never indistinguishable from a fetch.
        assert attributes["attestation"] == "operator-upload"
        assert attributes["uploaded-by"] == "admin@example.com"
        assert attributes["digest"] == f"sha256:{BODY_SHA}"
        assert resp.json()["meta"] == {"replaced": False, "unchanged": False}

    async def test_the_uploader_is_the_authenticated_principal_not_a_form_field(self) -> None:
        """An operator cannot attribute their upload to somebody else.

        Who vouched for an artifact is the whole of the provenance, so it comes
        from the session rather than from the request body -- and a form field
        claiming otherwise is ignored rather than honoured.
        """
        path, fields = FORMS["pypi"]
        app, _, _ = _make_app(user=_user(email="real@example.com"))

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            admit.return_value = uploads.Admission(record=_row())
            await _upload(app, path, fields, uploaded_by="someone.else@example.com")

        assert admit.await_args.kwargs["uploaded_by"] == "real@example.com"

    async def test_a_replacement_is_200_and_reports_what_it_displaced(self) -> None:
        path, fields = FORMS["pypi"]
        app, _, _ = _make_app(user=_user())

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            admit.return_value = uploads.Admission(
                record=_row(replaced_digest="sha256:" + "0" * 64),
                replaced=True,
                previous_digest="sha256:" + "0" * 64,
            )
            resp = await _upload(app, path, fields, replace="true")

        assert resp.status_code == 200
        assert resp.json()["meta"]["replaced"] is True
        assert resp.json()["meta"]["previous-digest"] == "sha256:" + "0" * 64

    async def test_a_replacement_on_a_sealed_node_warns_that_it_cannot_be_undone(
        self, monkeypatch
    ) -> None:
        """A replacement DELETES the bytes that were there, and a sealed node
        cannot fetch them back -- which is exactly what the eviction routes
        already say about themselves."""
        from terrapod.config import settings

        monkeypatch.setattr(settings.registry, "cache_only", True)
        path, fields = FORMS["pypi"]
        app, _, _ = _make_app(user=_user())

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            admit.return_value = uploads.Admission(record=_row(), replaced=True)
            resp = await _upload(app, path, fields, replace="true")

        assert resp.json()["meta"]["sealed"] is True
        assert "cache_only" in resp.json()["meta"]["warning"]

    async def test_a_first_upload_carries_no_sealed_warning(self, monkeypatch) -> None:
        """It destroys nothing, so the warning would be noise -- and a warning
        that appears on every upload is one nobody reads on the one that matters."""
        from terrapod.config import settings

        monkeypatch.setattr(settings.registry, "cache_only", True)
        path, fields = FORMS["pypi"]
        app, _, _ = _make_app(user=_user())

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            admit.return_value = uploads.Admission(record=_row())
            resp = await _upload(app, path, fields)

        assert "warning" not in resp.json()["meta"]

    async def test_an_identical_re_upload_is_200_and_unchanged(self) -> None:
        path, fields = FORMS["pypi"]
        app, _, _ = _make_app(user=_user())

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            admit.return_value = uploads.Admission(record=_row(), unchanged=True)
            resp = await _upload(app, path, fields)

        assert resp.status_code == 200
        assert resp.json()["meta"]["unchanged"] is True

    async def test_the_response_is_the_resource_the_listing_shows(self) -> None:
        """One serializer, so the page an operator reads and the answer they get
        back cannot disagree about the same row."""
        from terrapod.api.routers.cache_admin import _package_file_json

        path, fields = FORMS["pypi"]
        app, _, _ = _make_app(user=_user())
        row = _row()

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            admit.return_value = uploads.Admission(record=row)
            resp = await _upload(app, path, fields)

        assert resp.json()["data"] == _package_file_json(row)


class TestWhatIsRefused:
    async def test_coordinates_this_ecosystem_cannot_address_are_422(self) -> None:
        """Not a 404: there is nothing to look up, by design, and a 404 would
        imply we had asked upstream."""
        app, _, _ = _make_app(user=_user())

        resp = await _upload(
            app,
            "/api/v1/admin/package-cache/pypi/artifacts",
            {"project": "flask", "filename": "../../etc/passwd"},
        )

        assert resp.status_code == 422
        assert "no path in it" in resp.json()["detail"]

    async def test_a_missing_coordinate_field_is_422(self) -> None:
        app, _, _ = _make_app(user=_user())

        resp = await _upload(app, "/api/v1/admin/package-cache/npm/artifacts", {"name": "left-pad"})

        assert resp.status_code == 422

    async def test_an_empty_file_is_400_and_says_where_the_bytes_go(self) -> None:
        app, _, _ = _make_app(user=_user())

        resp = await _upload(
            app,
            "/api/v1/admin/package-cache/pypi/artifacts",
            {"project": "flask", "filename": "flask-3.0.0.tar.gz"},
            body=b"",
        )

        assert resp.status_code == 400
        assert "`file` part" in resp.json()["detail"]

    async def test_an_existing_artifact_is_409_naming_what_is_there(self) -> None:
        path, fields = FORMS["pypi"]
        app, _, _ = _make_app(user=_user())

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            admit.side_effect = uploads.UploadConflict(_row())
            resp = await _upload(app, path, fields)

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "replace=true" in detail
        assert BODY_SHA in detail

    async def test_a_non_admin_cannot_upload(self) -> None:
        """Who may put bytes into what this deployment serves is a
        deployment-wide trust assertion, so it is an admin capability rather
        than a workspace one. The iterated gate in `test_cache_admin.py` covers
        every route; this is the one that matters most, asserted where it reads.
        """
        path, fields = FORMS["pypi"]
        app, _, _ = _make_app(user=_user(roles=["everyone"]))

        resp = await _upload(app, path, fields)

        assert resp.status_code == 403

    async def test_a_runner_token_cannot_upload(self) -> None:
        """Unlike the Galaxy publish path this needs no explicit refusal: a
        runner token carries no platform role, so it cannot reach an admin
        dependency at all. Asserted rather than assumed, because "it cannot
        happen by construction" is the kind of claim that stops being true.
        """
        path, fields = FORMS["pypi"]
        app, _, _ = _make_app(
            user=AuthenticatedUser(
                email="runner",
                display_name="runner",
                roles=["everyone"],
                provider_name="runner",
                auth_method="runner_token",
            )
        )

        resp = await _upload(app, path, fields)

        assert resp.status_code == 403


class TestServingAnUploadThatNoLongerMatchesItsRecord:
    """Driven through the PROXY route, not the service.

    `substrate.lookup_present` raises `UploadedArtifactUnavailable`, and nothing
    on the serve routes catches it -- deliberately, because it is called from
    several of them and in some cases above any `try`, so a per-route `except`
    is a predicate written six times with one eventually missed. The app-level
    handler is what turns it into an answer, and without one it falls through to
    the generic net as a **500 "Internal server error"**, which tells an
    installing client nothing and reads as a fault rather than as missing bytes
    somebody has to put back.

    So this has to be a request. A service-level test passes either way.
    """

    def _proxy_app(self, row, storage):
        from terrapod.api.routers.package_cache import authenticate_package_request

        app = create_app()
        app.dependency_overrides[authenticate_package_request] = lambda: _user()
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = row
        db.execute.return_value = result
        db.flush = AsyncMock()
        db.delete = AsyncMock()
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_storage] = lambda: storage
        return app

    async def test_a_missing_object_is_503_naming_the_artifact(self) -> None:
        class _Gone:
            async def head(self, key):
                from terrapod.storage.protocol import ObjectNotFoundError

                raise ObjectNotFoundError(key)

        app = self._proxy_app(_row(uploaded_by="vetting@example.com"), _Gone())

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                "/api/v1/package-cache/pypi/simple/flask/flask-3.0.0-py3-none-any.whl",
                headers={"Authorization": "Bearer test-token"},
            )

        assert resp.status_code == 503, resp.text
        detail = resp.json()["detail"]
        assert "vetting@example.com" in detail
        assert "uploaded again" in detail

    async def test_bytes_that_are_not_the_recorded_ones_are_503(self) -> None:
        class _Truncated:
            async def head(self, key):
                from terrapod.storage.protocol import ObjectMeta

                return ObjectMeta(
                    key=key,
                    size_bytes=10,
                    content_type="application/octet-stream",
                    etag="",
                    last_modified=MagicMock(),
                )

        app = self._proxy_app(_row(), _Truncated())

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                "/api/v1/package-cache/pypi/simple/flask/flask-3.0.0-py3-none-any.whl",
                headers={"Authorization": "Bearer test-token"},
            )

        assert resp.status_code == 503
        assert "not those bytes" in resp.json()["detail"]

    async def test_it_is_503_rather_than_404(self) -> None:
        """404 would say the package does not exist, which sends a developer to
        look in entirely the wrong place -- the same reason sealing answers 503."""

        class _Gone:
            async def head(self, key):
                from terrapod.storage.protocol import ObjectNotFoundError

                raise ObjectNotFoundError(key)

        app = self._proxy_app(_row(), _Gone())

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                "/api/v1/package-cache/pypi/simple/flask/flask-3.0.0-py3-none-any.whl",
                headers={"Authorization": "Bearer test-token"},
            )

        assert resp.status_code not in (404, 500)


class TestTheVerifyRoute:
    async def test_it_reports_a_match(self) -> None:
        app, _, _ = _make_app(user=_user(), row=_row())

        with patch.object(uploads, "verify_object", new_callable=AsyncMock) as verify:
            verify.return_value = uploads.Verification(
                ok=True,
                detail="the stored object matches what was recorded",
                algorithm="sha256",
                recorded=f"sha256:{BODY_SHA}",
                computed=f"sha256:{BODY_SHA}",
            )
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post("/api/v1/admin/package-cache/f1/verify")

        assert resp.status_code == 200
        attributes = resp.json()["data"]["attributes"]
        assert attributes["ok"] is True
        assert attributes["attestation"] == "operator-upload"
        assert attributes["uploaded-by"] == "admin@example.com"

    async def test_an_unverifiable_row_is_null_rather_than_a_pass_or_a_fail(self) -> None:
        """A `none-published` row carries no digest. Reporting that as `false`
        would send an operator hunting a corruption that is not there, and as
        `true` would claim a check nobody performed."""
        app, _, _ = _make_app(user=_user(), row=_row(digest=""))

        with patch.object(uploads, "verify_object", new_callable=AsyncMock) as verify:
            verify.return_value = uploads.Verification(
                ok=None, detail="no digest was recorded for this artifact"
            )
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post("/api/v1/admin/package-cache/f1/verify")

        assert resp.json()["data"]["attributes"]["ok"] is None

    async def test_an_unknown_id_is_404(self) -> None:
        app, _, _ = _make_app(user=_user(), row=None)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/v1/admin/package-cache/nope/verify")

        assert resp.status_code == 404


#: The index-document routes, with a form each ecosystem accepts. An artifact
#: upload supplies BYTES; three of the six ecosystems answer "what versions
#: exist" from a cached document and nowhere else, so without one a vetted
#: artifact is present and unresolvable on the sealed node this feature exists
#: for. Keyed by the ecosystem the route must pass to `admit`, as FORMS is.
INDEX_FORMS: dict[str, tuple[str, dict[str, str]]] = {
    "npm": ("/api/v1/admin/package-cache/npm/index", {"name": "@scope/pkg"}),
    "nuget": ("/api/v1/admin/package-cache/nuget/index", {"package_id": "Newtonsoft.Json"}),
    "go": (
        "/api/v1/admin/package-cache/go/index",
        {"module": "github.com/foo/bar", "document": "latest"},
    ),
    "galaxy": (
        "/api/v1/admin/package-cache/galaxy/index",
        {"namespace": "community", "name": "general", "document": "versions"},
    ),
}

#: Where each index document must land. The filename is what the SEALED read asks
#: for, so a document stored elsewhere is present and never found -- written out
#: rather than read off `index_coordinates`, which would assert only that the
#: function agrees with itself.
EXPECTED_INDEX_KEY: dict[str, tuple[str, str]] = {
    "npm": ("@scope/pkg", "packument.json"),
    "nuget": ("newtonsoft.json", "index.json"),
    "go": ("github.com/foo/bar", "@latest"),
    "galaxy": ("community.general", "versions.json"),
}

INDEX_BODY = b'{"versions": {"1.3.0": {"dist": {"tarball": "x"}}}}'


class TestIndexDocumentUploads:
    """The upload that makes an uploaded ARTIFACT reachable.

    A sealed node resolves npm from a cached packument, Galaxy from its two
    collection documents and NuGet from a versions index. PyPI builds its sealed
    index from the rows it holds and Pulumi plugins are filename-addressed, so
    neither has a route here -- asserted in the coordinates tests rather than
    assumed.
    """

    @pytest.mark.parametrize("ecosystem", sorted(INDEX_FORMS))
    async def test_the_document_lands_where_the_sealed_read_looks(self, ecosystem: str) -> None:
        path, fields = INDEX_FORMS[ecosystem]
        app, _, _ = _make_app(user=_user())

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            admit.return_value = uploads.Admission(record=_row())
            resp = await _upload(app, path, fields, body=INDEX_BODY)

        assert resp.status_code == 201, resp.text
        assert admit.await_args.kwargs["ecosystem"] == ecosystem
        coords = admit.await_args.kwargs["coordinates"]
        assert (coords.name, coords.filename) == EXPECTED_INDEX_KEY[ecosystem], (
            "stored at a key the sealed read does not ask for, so the document "
            "would be present and never found"
        )

    @pytest.mark.parametrize("ecosystem", sorted(INDEX_FORMS))
    async def test_an_unparseable_document_is_refused_rather_than_stored(
        self, ecosystem: str
    ) -> None:
        """A sealed node PARSES these to answer what versions exist, so storing one
        that does not parse would fail every read rather than this upload.

        `go`'s `latest` is JSON like the others; its `list` is text and exempt, which
        the next test covers.
        """
        path, fields = INDEX_FORMS[ecosystem]
        app, _, store = _make_app(user=_user())

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            resp = await _upload(app, path, fields, body=b"this is not json {")

        assert resp.status_code == 422, resp.text
        assert "not valid JSON" in resp.json()["detail"]
        assert admit.await_count == 0, "refused after admitting it, so the bytes are stored"
        assert store.put == [], "wrote the object despite refusing the upload"

    async def test_gos_version_list_is_text_and_is_NOT_parsed_as_json(self) -> None:
        """`@v/list` is newline-separated versions. Parsing it as JSON would refuse a
        correct document, so the gate keys on the coordinates' own content type."""
        app, _, _ = _make_app(user=_user())

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            admit.return_value = uploads.Admission(record=_row())
            resp = await _upload(
                app,
                "/api/v1/admin/package-cache/go/index",
                {"module": "github.com/foo/bar", "document": "list"},
                body=b"v1.0.0\nv1.2.3\n",
            )

        assert resp.status_code == 201, resp.text
        assert admit.await_args.kwargs["coordinates"].content_type.startswith("text/plain")

    async def test_an_artifact_upload_is_never_parsed_as_json(self) -> None:
        """The gate is keyed on the content type the COORDINATES carry, not on the
        route, so a tarball -- which is not JSON and must not be -- is unaffected."""
        app, _, _ = _make_app(user=_user())

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            admit.return_value = uploads.Admission(record=_row())
            resp = await _upload(app, *FORMS["npm"], body=b"\x1f\x8b\x08 not json at all")

        assert resp.status_code == 201, resp.text

    @pytest.mark.parametrize("ecosystem", sorted(INDEX_FORMS))
    async def test_a_non_admin_is_refused(self, ecosystem: str) -> None:
        """Who may put bytes into what this deployment serves is a deployment-wide
        trust assertion, so an index document is held to the same gate as an
        artifact."""
        path, fields = INDEX_FORMS[ecosystem]
        app, _, store = _make_app(user=_user(email="dev@example.com", roles=["everyone"]))

        with patch.object(uploads, "admit", new_callable=AsyncMock) as admit:
            resp = await _upload(app, path, fields, body=INDEX_BODY)

        assert resp.status_code == 403, resp.text
        assert admit.await_count == 0
        assert store.put == []

    @pytest.mark.parametrize(
        ("path", "fields"),
        [
            (
                "/api/v1/admin/package-cache/galaxy/index",
                {"namespace": "community", "name": "general", "document": "nope"},
            ),
            (
                "/api/v1/admin/package-cache/go/index",
                {"module": "github.com/foo/bar", "document": "nope"},
            ),
        ],
    )
    async def test_an_unknown_document_name_is_422_naming_the_valid_ones(
        self, path: str, fields: dict[str, str]
    ) -> None:
        """422 rather than 404: the request is well-formed and the document is not one
        this ecosystem has. A 404 would imply we had looked somewhere for it."""
        app, _, _ = _make_app(user=_user())

        with patch.object(uploads, "admit", new_callable=AsyncMock):
            resp = await _upload(app, path, fields, body=INDEX_BODY)

        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert "is not a" in detail and "valid values are" in detail, detail
