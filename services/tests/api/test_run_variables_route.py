"""The create path's side of per-run `-var` values (#2377).

The precedence lives in `variable_service` and is proven in the services tier.
What is proven here is what the route does: that the attribute is read at all,
that a refusal reaches the caller as a 422 rather than a 500, and that the run
document never carries a value back.

Reading it at all is the whole of the original bug — `variables` was accepted
and ignored, so `tofu plan -var` appeared to work and planned against the
workspace's value instead.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.auth.capabilities import caps_for_level
from terrapod.services import variable_service

from .test_runs import _AUTH, _BASE, _make_app, _mock_run, _mock_workspace, _user


def _app_for(ws):
    app, mock_db = _make_app(_user())
    result = MagicMock()
    result.scalar_one_or_none.return_value = ws
    mock_db.execute.return_value = result
    mock_db.refresh = AsyncMock()
    return app


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestRunVariablesOnTheCreatePath:
    @patch("terrapod.api.routers.runs.run_service.queue_run")
    @patch("terrapod.api.routers.runs.run_service.create_run")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_variables_reach_the_service(self, mock_resolve, mock_create, mock_queue, *_m):
        """Read off the body and passed on — the whole of the bug."""
        mock_resolve.return_value = caps_for_level("write")
        ws = _mock_workspace()
        run = _mock_run(ws_id=ws.id, status="queued")
        mock_create.return_value = run
        mock_queue.return_value = run

        async with AsyncClient(transport=ASGITransport(app=_app_for(ws)), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/runs",
                json={
                    "data": {
                        "attributes": {"variables": [{"key": "region", "value": "us-1"}]},
                        "relationships": {"workspace": {"data": {"id": f"ws-{ws.id}"}}},
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 201, resp.text
        assert mock_create.await_args.kwargs["run_variables"] == [
            {"key": "region", "value": "us-1"}
        ]

    @patch("terrapod.api.routers.runs.run_service.queue_run")
    @patch("terrapod.api.routers.runs.run_service.create_run")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_an_absent_attribute_passes_none_not_an_empty_list(
        self, mock_resolve, mock_create, mock_queue, *_m
    ):
        """A run nobody passed `-var` to must look exactly as it always did."""
        mock_resolve.return_value = caps_for_level("write")
        ws = _mock_workspace()
        run = _mock_run(ws_id=ws.id, status="queued")
        mock_create.return_value = run
        mock_queue.return_value = run

        async with AsyncClient(transport=ASGITransport(app=_app_for(ws)), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/runs",
                json={
                    "data": {
                        "attributes": {},
                        "relationships": {"workspace": {"data": {"id": f"ws-{ws.id}"}}},
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 201, resp.text
        assert mock_create.await_args.kwargs["run_variables"] is None

    @patch("terrapod.api.routers.runs.run_service.create_run")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_a_refusal_is_a_422_naming_the_key_not_a_500(
        self, mock_resolve, mock_create, *_m
    ):
        """The refusal has to reach whoever asked for the run.

        `UnknownExecutionEnvironment` next door had its own type "so a router
        can answer 422 naming the field" and no router translated it, so the
        condition answered `Internal server error` for a release. A typed
        exception nothing catches is the same defect as no exception at all.
        """
        mock_resolve.return_value = caps_for_level("write")
        ws = _mock_workspace()
        mock_create.side_effect = variable_service.RunVariableRejected(
            "run variable 'region' is pinned by a priority variable set"
        )

        async with AsyncClient(transport=ASGITransport(app=_app_for(ws)), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/runs",
                json={
                    "data": {
                        "attributes": {"variables": [{"key": "region", "value": "x"}]},
                        "relationships": {"workspace": {"data": {"id": f"ws-{ws.id}"}}},
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422, resp.text
        assert "region" in resp.text


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestRunVariablesAreTerraformOnly:
    """The refusal is about the VALUE, not the name.

    A run variable carries an HCL EXPRESSION -- `go-tfe`'s `RunVariable`
    contract -- and Pulumi has no HCL. Its own equivalent is `pulumi config
    set`, which is the workspace's variables rather than a per-run flag.

    This needs a route test rather than a unit one because the TFE surface
    already 404s a non-Terraform workspace, so the gap is only reachable
    NATIVELY: `POST /runs` is mounted on both, and without the refusal a native
    caller hands a Pulumi run an HCL string that `pulumi_config` then treats as
    a `--path` nested key.
    """

    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_a_pulumi_workspace_refuses_them(self, mock_resolve, *_m):
        mock_resolve.return_value = caps_for_level("write")
        ws = _mock_workspace()
        ws.engine = "pulumi"

        async with AsyncClient(transport=ASGITransport(app=_app_for(ws)), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v1/runs",
                json={
                    "data": {
                        "attributes": {"variables": [{"key": "region", "value": '"us-1"'}]},
                        "relationships": {"workspace": {"data": {"id": f"ws-{ws.id}"}}},
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422, resp.text
        assert "variables" in resp.text

    @patch("terrapod.api.routers.runs.run_service.queue_run")
    @patch("terrapod.api.routers.runs.run_service.create_run")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_an_EMPTY_list_is_not_refused_on_pulumi(
        self, mock_resolve, mock_create, mock_queue, *_m
    ):
        """Only a truthy value is refused, which is the map's own rule.

        A client that serialises every field sends `variables: []` on every run;
        422ing that would break run creation on every Pulumi workspace for a
        caller asking for nothing.
        """
        mock_resolve.return_value = caps_for_level("write")
        ws = _mock_workspace()
        ws.engine = "pulumi"
        run = _mock_run(ws_id=ws.id, status="queued")
        mock_create.return_value = run
        mock_queue.return_value = run

        async with AsyncClient(transport=ASGITransport(app=_app_for(ws)), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v1/runs",
                json={
                    "data": {
                        "attributes": {"variables": []},
                        "relationships": {"workspace": {"data": {"id": f"ws-{ws.id}"}}},
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 201, resp.text


class TestTheRunDocumentNeverCarriesAValue:
    """Keys yes, values never.

    `RunVariable` has no sensitive flag, so a value typed after `-var` may be a
    secret and nothing can tell us which -- which is why the column is
    encrypted. Echoing it on a document any run reader can fetch would undo
    that in one line.

    Asserted against the SERIALISED DOCUMENT, not against the helpers. The
    first version of this class called `_run_variables` / `_run_variable_keys`
    directly, and swapping the document's `_run_variable_keys(run)` for
    `_run_variables(run)` -- i.e. emitting the values -- left every test
    passing. A test that calls the helper proves the helper works and says
    nothing about what the route sends.
    """

    def _document(self, entries):
        from terrapod.api.routers.runs import _run_json

        run = MagicMock()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.status = "planned"
        run.has_changes = True
        run.run_variables = json.dumps(entries) if entries is not None else None
        run.module_overrides = None
        run.target_addrs = None
        run.replace_addrs = None
        return _run_json(run, engine="terraform", tfe=True)

    def test_the_keys_are_reported(self):
        doc = self._document([{"key": "region", "value": "s3cret"}])
        assert doc["data"]["attributes"]["run-variable-keys"] == ["region"]

    def test_the_value_appears_nowhere_in_the_document(self):
        doc = self._document([{"key": "region", "value": "s3cret-value-xyz"}])
        # default=str so an unset MagicMock attribute on the fixture stringifies
        # instead of raising -- the assertion is about the VALUE appearing, and a
        # repr can never contain it, so the check stays meaningful and survives
        # the document gaining attributes.
        assert "s3cret-value-xyz" not in json.dumps(doc, default=str), (
            "a run variable's VALUE reached the run document; `RunVariable` has no "
            "sensitive flag so it may be a secret, which is why the column is encrypted"
        )

    def test_go_tfes_own_variables_field_is_not_emitted(self):
        """Emitting `variables` with the values omitted would read to a go-tfe
        client as "this run set no variables", which is worse than silence."""
        doc = self._document([{"key": "region", "value": "v"}])
        assert "variables" not in doc["data"]["attributes"]

    def test_a_run_with_none_reports_an_empty_list(self):
        doc = self._document(None)
        assert doc["data"]["attributes"]["run-variable-keys"] == []
