"""JSON:API dual-key error envelope (#1063).

Every error body must carry BOTH the JSON:API ``errors`` array (what
go-terrapod's extractor understands) AND the legacy top-level ``detail`` (what
older clients read). The change is additive — ``detail`` is never removed.
"""

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from terrapod.api.errors import jsonapi_error_content

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _app() -> FastAPI:
    """A tiny app that registers the same exception handlers as the real one.

    (The full app's real registration is exercised implicitly by the ~890
    router tests, which now hit these handlers on every error path.)
    """
    application = FastAPI()

    from fastapi.encoders import jsonable_encoder
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from terrapod.api.errors import jsonapi_error_response

    @application.exception_handler(StarletteHTTPException)
    async def _http(request, exc):
        return jsonapi_error_response(
            exc.detail, exc.status_code, headers=getattr(exc, "headers", None)
        )

    @application.exception_handler(RequestValidationError)
    async def _val(request, exc):
        return jsonapi_error_response(jsonable_encoder(exc.errors()), 422)

    class Body(BaseModel):
        n: int

    @application.get("/boom")
    async def boom():
        raise HTTPException(status_code=404, detail="workspace not found")

    @application.get("/teapot")
    async def teapot():
        raise HTTPException(status_code=401, detail="nope", headers={"WWW-Authenticate": "Bearer"})

    @application.post("/validate")
    async def validate(body: Body):
        return {"ok": body.n}

    return application


async def test_http_exception_has_both_keys():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        resp = await c.get("/boom")
    assert resp.status_code == 404
    body = resp.json()
    # legacy key preserved verbatim
    assert body["detail"] == "workspace not found"
    # JSON:API errors array present
    assert body["errors"] == [{"detail": "workspace not found", "status": "404"}]


async def test_http_exception_preserves_headers():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        resp = await c.get("/teapot")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"
    body = resp.json()
    assert body["detail"] == "nope"
    assert body["errors"][0]["detail"] == "nope"


async def test_validation_error_has_both_keys():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        resp = await c.post("/validate", json={"n": "not-an-int"})
    assert resp.status_code == 422
    body = resp.json()
    # `detail` still the FastAPI validation list (back-compat)
    assert isinstance(body["detail"], list)
    # plus an errors array with a JSON-Pointer source
    assert body["errors"]
    assert body["errors"][0]["status"] == "422"
    assert body["errors"][0]["source"]["pointer"].startswith("/")


class TestEnvelopeHelper:
    def test_string_detail(self):
        c = jsonapi_error_content("boom", 500)
        assert c == {"errors": [{"detail": "boom", "status": "500"}], "detail": "boom"}

    def test_list_detail_maps_each_with_pointer(self):
        detail = [{"loc": ["body", "n"], "msg": "field required", "type": "missing"}]
        c = jsonapi_error_content(detail, 422)
        assert c["detail"] == detail  # verbatim
        assert c["errors"][0] == {
            "detail": "field required",
            "status": "422",
            "source": {"pointer": "/body/n"},
        }

    def test_empty_list_detail_gets_placeholder(self):
        c = jsonapi_error_content([], 422)
        assert c["errors"] == [{"detail": "Validation error", "status": "422"}]


class TestNoBodyStatusCodes:
    """Statuses that forbid a body (1xx/204/304) must emit NO body — matching
    FastAPI's default handler exactly. Regression guard: the dual-key envelope
    must not put JSON on a 204/304 (invalid HTTP, and a behaviour change vs
    pre-#1063). Nothing raises these today; the guard keeps the handler a strict
    superset of FastAPI's."""

    def _app_with_status(self, status: int) -> FastAPI:
        from fastapi.responses import Response
        from fastapi.utils import is_body_allowed_for_status_code
        from starlette.exceptions import HTTPException as StarletteHTTPException

        from terrapod.api.errors import jsonapi_error_response

        application = FastAPI()

        @application.exception_handler(StarletteHTTPException)
        async def _http(request, exc):
            headers = getattr(exc, "headers", None)
            if not is_body_allowed_for_status_code(exc.status_code):
                return Response(status_code=exc.status_code, headers=headers)
            return jsonapi_error_response(exc.detail, exc.status_code, headers=headers)

        @application.get("/x")
        async def x():
            raise HTTPException(status_code=status, detail="ignored")

        return application

    async def test_204_has_empty_body(self):
        async with AsyncClient(
            transport=ASGITransport(app=self._app_with_status(204)), base_url="http://t"
        ) as c:
            resp = await c.get("/x")
        assert resp.status_code == 204
        assert resp.content == b""

    async def test_304_has_empty_body(self):
        async with AsyncClient(
            transport=ASGITransport(app=self._app_with_status(304)), base_url="http://t"
        ) as c:
            resp = await c.get("/x")
        assert resp.status_code == 304
        assert resp.content == b""

    async def test_404_still_has_the_dual_key_body(self):
        async with AsyncClient(
            transport=ASGITransport(app=self._app_with_status(404)), base_url="http://t"
        ) as c:
            resp = await c.get("/x")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "ignored"
        assert resp.json()["errors"][0]["detail"] == "ignored"


class TestPaginationRefactorIsByteIdentical:
    """`build_meta` must reproduce the hand-rolled `meta.pagination` blocks the
    audit/config_versions/runs/users routers used before #1063, byte for byte.
    All four clamp `page[size]` to 1..100, so the helper's cap never engages.
    """

    @staticmethod
    def _old(total: int, number: int, size: int, *, falsy_guard: bool) -> dict:
        # `falsy_guard` reproduces config_versions' `if total else 0`; the other
        # three used `if total > 0 else 0`. Equivalent for non-negative ints.
        pages = (total + size - 1) // size if (total if falsy_guard else total > 0) else 0
        return {
            "pagination": {
                "current-page": number,
                "page-size": size,
                "total-count": total,
                "total-pages": pages,
            }
        }

    def test_matches_across_the_clamped_input_space(self):
        from terrapod.api.pagination import build_meta

        for total in (0, 1, 2, 19, 20, 21, 99, 100, 101, 1000, 12345):
            for size in (1, 5, 20, 50, 99, 100):
                for number in (1, 2, 3, 7, 100):
                    new = build_meta(total, number, size)
                    assert new == self._old(total, number, size, falsy_guard=False), (
                        total,
                        number,
                        size,
                    )
                    assert new == self._old(total, number, size, falsy_guard=True), (
                        total,
                        number,
                        size,
                    )
