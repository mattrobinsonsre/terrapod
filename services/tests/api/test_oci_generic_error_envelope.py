"""An app-wide error net must answer `/v2/` in the OCI envelope (#2562).

`services/oci/errors.py` opens by predicting this exact symptom: a container
client that receives Terrapod's house `{"detail": ...}` where it expects
`{"errors": [{"code": ...}]}` "will report something unhelpful -- `docker push`
in particular will surface a bare 'unknown error'". That is what happened. The
router's own `OCIError` handler was correct; the generic nets BELOW it -- which
no route raises on purpose, so nobody looked -- answered in JSON:API, and a real
`docker push` reported

    error from registry: unknown error - Resource already exists or violates a constraint

which is docker printing its fallback because it could not read the body.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.asyncio

#: Paths the registry serves, and one the house envelope must keep.
REGISTRY = "/v2/"
NATIVE = "/api/v1/workspaces"


def _handlers(app):
    """The app's exception handlers, by exception type name."""
    return {getattr(k, "__name__", str(k)): v for k, v in app.exception_handlers.items()}


class TestTheGenericNetsKnowTheRegistrySurface:
    async def _response(self, handler, path, exc):
        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "headers": [],
            "query_string": b"",
        }
        return await handler(Request(scope), exc)

    @pytest.mark.parametrize("path", [REGISTRY, "/v2/team/app/manifests/latest", "/v2"])
    async def test_an_integrity_error_on_the_registry_is_an_oci_envelope(self, path):
        import json

        from terrapod.api.app import app

        handler = _handlers(app)["IntegrityError"]
        exc = IntegrityError("INSERT", {}, Exception("duplicate key"))
        response = await self._response(handler, path, exc)
        body = json.loads(response.body)
        assert "errors" in body, body
        assert body["errors"][0].get("code"), (
            f"no `code` -- a container client reports `unknown error` for this: {body}"
        )

    async def test_an_unhandled_error_on_the_registry_is_an_oci_envelope(self):
        import json

        from terrapod.api.app import app

        handler = _handlers(app)["Exception"]
        response = await self._response(handler, REGISTRY, RuntimeError("boom"))
        body = json.loads(response.body)
        assert body["errors"][0].get("code"), body

    @pytest.mark.parametrize("name", ["IntegrityError", "Exception"])
    async def test_the_native_surface_keeps_the_house_envelope(self, name):
        """The control. Without it the assertions above are satisfied by a
        handler that returned the OCI shape everywhere, which would break every
        non-registry client at once."""
        import json

        from terrapod.api.app import app

        handler = _handlers(app)[name]
        exc = (
            IntegrityError("INSERT", {}, Exception("dup"))
            if name == "IntegrityError"
            else RuntimeError("boom")
        )
        response = await self._response(handler, NATIVE, exc)
        body = json.loads(response.body)
        assert "detail" in body, f"the native surface lost its house envelope: {body}"

    async def test_a_path_merely_containing_v2_is_not_the_registry(self):
        """`/api/v2/...` is the TFE compatibility surface, not the registry.

        A `startswith('/v2')` without the boundary would also match `/v20/` and
        a substring test would match `/api/v2/runs`, handing go-tfe a shape it
        does not parse -- trading one client's broken errors for another's.
        """
        import json

        from terrapod.api.app import app

        handler = _handlers(app)["IntegrityError"]
        for path in ("/api/v2/runs", "/v20/thing", "/api/tfe/v2/workspaces"):
            response = await self._response(
                handler, path, IntegrityError("INSERT", {}, Exception("dup"))
            )
            body = json.loads(response.body)
            assert "detail" in body, f"{path} was treated as the registry: {body}"
