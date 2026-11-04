"""What an external run task service actually receives (#2367).

Six fields of this payload were hardcoded `""`/`False` while the values sat
on rows the dispatcher had already loaded, and `build_task_callback_payload`
appeared in **no test** -- so nothing asserted any field in either direction
and the defect was invisible to the suite. These tests drive
`handle_run_task_call` end to end and compare the whole decoded body, because
a per-field spot check is how the next field comes to go stale.

Driving the real function also catches a `MagicMock` leaking into the
payload for free: the dispatcher `json.dumps` it, which raises on a Mock.
"""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services.run_task_dispatcher import handle_run_task_call

EXTERNAL = "https://tp.example.com"


def _fixture(*, speculative: bool = False, with_cv: bool = True):
    """The five rows the dispatcher loads, plus the configuration version."""
    ws = MagicMock()
    ws.id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    ws.name = "payments-prod"

    run = MagicMock()
    run.id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    run.workspace_id = ws.id
    run.status = "planned"
    run.message = "Triggered via PR #418"
    run.created_by = "someone@example.com"
    run.created_at = datetime(2026, 11, 4, 9, 30, 0, tzinfo=UTC)
    run.configuration_version_id = (
        uuid.UUID("33333333-3333-3333-3333-333333333333") if with_cv else None
    )

    cv = MagicMock()
    cv.id = run.configuration_version_id
    cv.speculative = speculative

    ts = MagicMock()
    ts.id = uuid.uuid4()
    ts.run_id = run.id
    ts.stage = "post_plan"

    rt = MagicMock()
    rt.id = uuid.UUID("44444444-4444-4444-4444-444444444444")
    rt.name = "opa-gate"
    rt.url = "https://gate.example.com/webhook"
    rt.hmac_key = None

    tsr = MagicMock()
    tsr.id = uuid.UUID("55555555-5555-5555-5555-555555555555")
    tsr.task_stage_id = ts.id
    tsr.run_task_id = rt.id
    tsr.callback_token = "tok-abc"
    tsr.status = "pending"

    rows = {
        "TaskStageResult": tsr,
        "TaskStage": ts,
        "Run": run,
        "Workspace": ws,
        "RunTask": rt,
        "ConfigurationVersion": cv if with_cv else None,
    }
    return rows, tsr


async def _dispatch(rows, tsr, *, external_url: str | None = EXTERNAL):
    """Run the dispatcher against the fixture and return the decoded body."""
    db = AsyncMock()
    db.get.side_effect = lambda model, id_: rows[model.__name__]
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)

    sent: dict = {}

    async def _capture(client, method, url, **kw):
        sent["url"] = url
        sent["headers"] = kw.get("headers", {})
        sent["body"] = json.loads(kw["content"])
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "ok"
        return resp

    with (
        patch("terrapod.services.run_task_dispatcher.get_db_session", return_value=db),
        patch("terrapod.services.run_task_dispatcher.validate_outbound_url", new=AsyncMock()),
        patch("terrapod.services.run_task_dispatcher.resolve_stage", new=AsyncMock()),
        # Lazily imported inside the handler, so it must be patched at the
        # source module -- patching it on the dispatcher would patch nothing
        # and the test would exercise the real retry wrapper.
        patch("terrapod.http_retry.arequest_with_retry", new=_capture),
        patch("terrapod.config.settings.external_url", external_url),
        patch("terrapod.config.settings.public_webhook_url", None),
    ):
        await handle_run_task_call({"task_stage_result_id": str(tsr.id)})

    return sent


class TestEveryFieldDescribesTheRun:
    async def test_the_whole_payload_is_populated_from_the_rows(self):
        """The complete body, not a spot check of the fields we just fixed."""
        rows, tsr = _fixture()
        sent = await _dispatch(rows, tsr)

        assert sent["body"] == {
            "payload_version": 1,
            "stage": "post_plan",
            "access_token": "tok-abc",
            "task_result_callback_url": (
                f"{EXTERNAL}/api/v1/task-stage-results/"
                "tsr-55555555-5555-5555-5555-555555555555/callback"
            ),
            "run_app_url": (
                f"{EXTERNAL}/workspaces/11111111-1111-1111-1111-111111111111"
                "/runs/22222222-2222-2222-2222-222222222222"
            ),
            "run_id": "run-22222222-2222-2222-2222-222222222222",
            "run_message": "Triggered via PR #418",
            "run_created_at": "2026-11-04T09:30:00Z",
            "run_created_by": "someone@example.com",
            "workspace_id": "ws-11111111-1111-1111-1111-111111111111",
            "workspace_name": "payments-prod",
            "workspace_app_url": f"{EXTERNAL}/workspaces/11111111-1111-1111-1111-111111111111",
            "organization_name": "default",
            "task_name": "opa-gate",
            "task_id": "task-44444444-4444-4444-4444-444444444444",
            "is_speculative": False,
        }

    async def test_no_field_is_left_empty(self):
        """The defect's own shape: a field that is present and blank.

        Asserted separately from the equality above because that test would
        also fail on an unrelated change, and this one names the class of
        defect -- a field emitted as `""` while the row carries a value.
        """
        rows, tsr = _fixture()
        sent = await _dispatch(rows, tsr)
        blank = [k for k, v in sent["body"].items() if v == ""]
        assert blank == [], f"hardcoded-empty fields are back: {blank}"

    async def test_run_status_is_not_in_the_payload(self):
        """It was a parameter the caller resolved and nothing emitted (#2367).

        Pinned so the fix is not "completed" later by adding the key: a
        task's view of the run is the stage it was called at.
        """
        rows, tsr = _fixture()
        sent = await _dispatch(rows, tsr)
        assert "run_status" not in sent["body"]


class TestIsSpeculativeComesFromTheConfigurationVersion:
    @pytest.mark.parametrize("speculative", [True, False])
    async def test_it_follows_the_version_flag(self, speculative: bool):
        rows, tsr = _fixture(speculative=speculative)
        sent = await _dispatch(rows, tsr)
        assert sent["body"]["is_speculative"] is speculative

    async def test_a_run_with_no_configuration_version_reports_false(self):
        """`configuration_version_id` is nullable (`ondelete="SET NULL"`).

        And the version must not be looked up at all in that case -- a
        `db.get(ConfigurationVersion, None)` is a query for nothing.
        """
        rows, tsr = _fixture(with_cv=False)
        db_models_asked: list[str] = []

        db = AsyncMock()

        def _get(model, id_):
            db_models_asked.append(model.__name__)
            return rows[model.__name__]

        db.get.side_effect = _get
        db.__aenter__ = AsyncMock(return_value=db)
        db.__aexit__ = AsyncMock(return_value=False)

        sent: dict = {}

        async def _capture(client, method, url, **kw):
            sent["body"] = json.loads(kw["content"])
            resp = MagicMock()
            resp.status_code = 200
            resp.text = "ok"
            return resp

        with (
            patch("terrapod.services.run_task_dispatcher.get_db_session", return_value=db),
            patch("terrapod.services.run_task_dispatcher.validate_outbound_url", new=AsyncMock()),
            patch("terrapod.services.run_task_dispatcher.resolve_stage", new=AsyncMock()),
            patch("terrapod.http_retry.arequest_with_retry", new=_capture),
            patch("terrapod.config.settings.external_url", EXTERNAL),
            patch("terrapod.config.settings.public_webhook_url", None),
        ):
            await handle_run_task_call({"task_stage_result_id": str(tsr.id)})

        assert sent["body"]["is_speculative"] is False
        assert "ConfigurationVersion" not in db_models_asked


class TestAnUnsetExternalUrl:
    async def test_the_app_urls_are_empty_strings_not_null_and_not_absent(self):
        """`run_links` returns None for an unset `external_url`; the payload
        has declared string fields an integration may read directly, and both
        have been `""` since the payload existed. So `or ""` at the edge --
        the same choice `vcs_status_dispatcher` and `slack_notify_service`
        already make -- rather than `null` or a vanished key.
        """
        rows, tsr = _fixture()
        sent = await _dispatch(rows, tsr, external_url=None)

        body = sent["body"]
        assert body["run_app_url"] == ""
        assert body["workspace_app_url"] == ""
        assert "run_app_url" in body and "workspace_app_url" in body
        # Everything that does not depend on external_url is still populated.
        assert body["run_message"] == "Triggered via PR #418"
        assert body["task_id"] == "task-44444444-4444-4444-4444-444444444444"
