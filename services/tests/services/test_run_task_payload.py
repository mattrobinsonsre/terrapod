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
    ws.working_directory = "infra/prod"
    ws.vcs_repo_url = "https://github.com/acme/payments"

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
    run.vcs_branch = "main"
    run.vcs_commit_sha = "deadbeefcafe"

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
    rt.enforcement_level = "advisory"

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
            # The spec-shaped half (#2366). `capabilities` declares what WE
            # accept in the callback, not what the integration does.
            "capabilities": {"outcomes": False},
            "task_result_id": "tsr-55555555-5555-5555-5555-555555555555",
            "task_result_enforcement_level": "advisory",
            "workspace_working_directory": "infra/prod",
            "configuration_version_id": "cv-33333333-3333-3333-3333-333333333333",
            # Both point at the ORDINARY AUTHENTICATED routes, which is what
            # makes emitting them cost nothing: a URL is not an authorization.
            "configuration_version_download_url": (
                f"{EXTERNAL}/api/v1/configuration-versions/"
                "cv-33333333-3333-3333-3333-333333333333/download"
            ),
            "plan_json_api_url": (
                f"{EXTERNAL}/api/tfe/v2/plans/22222222-2222-2222-2222-222222222222/json-output"
            ),
            "vcs_repo_url": "https://github.com/acme/payments",
            "vcs_branch": "main",
            "vcs_commit_hash": "deadbeefcafe",
            # Hardcoded `None`, and a documented gap rather than a value: we
            # hold the sha and the PR number but not a web URL, and building
            # one needs provider-aware construction that does not exist yet.
            # `null` is spec-legal; an omitted key is what breaks a strict
            # client.
            "vcs_commit_url": None,
            "vcs_pull_request_url": None,
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

    async def test_an_absent_fact_is_null_and_not_an_empty_string(self):
        """The distinction the nullable fields turn on (#2366).

        A run with no configuration version and a workspace with no VCS
        connection have no honest value for these, and the spec types them
        "or `null`" — so `None` is the documented absence. An empty string
        would be the #2367 defect wearing a different costume: a field that is
        present, readable and lying about the run.

        `workspace_working_directory` is deliberately NOT in this set. The spec
        types it a plain string and a workspace at the repository root really
        does have `""` as its working directory, so there the empty string is
        the fact rather than the absence of one.
        """
        rows, tsr = _fixture(with_cv=False)
        rows["Workspace"].vcs_repo_url = ""
        rows["Run"].vcs_branch = ""
        rows["Run"].vcs_commit_sha = ""
        sent = await _dispatch(rows, tsr)

        for field in (
            "configuration_version_id",
            "configuration_version_download_url",
            "vcs_repo_url",
            "vcs_branch",
            "vcs_commit_hash",
        ):
            assert sent["body"][field] is None, (
                f"{field} should be null, not {sent['body'][field]!r}"
            )
        assert "workspace_working_directory" in sent["body"]

    async def test_run_status_is_not_in_the_payload(self):
        """It was a parameter the caller resolved and nothing emitted (#2367).

        Pinned so the fix is not "completed" later by adding the key: a
        task's view of the run is the stage it was called at.
        """
        rows, tsr = _fixture()
        sent = await _dispatch(rows, tsr)
        assert "run_status" not in sent["body"]


class TestTheTwoSpecUrls:
    """Both are emitted, and both point at the AUTHENTICATED routes (#2366).

    That is the whole reason they need no per-run-task opt-in, so it is the
    property worth pinning rather than the strings themselves: a URL carrying
    no credential discloses nothing, and `config:read` / `state:read` are
    still resolved at fetch time against a real user.
    """

    async def test_neither_url_carries_a_credential(self):
        """The mutation this exists for is someone "helpfully" making them
        work with the payload's token by minting a capability URL -- which
        would put a code-read credential in a body webhook receivers log."""
        rows, tsr = _fixture()
        sent = await _dispatch(rows, tsr)

        for field in ("configuration_version_download_url", "plan_json_api_url"):
            url = sent["body"][field]
            assert url is not None
            assert "?" not in url, f"{field} carries a query string: {url}"
            assert "ticket" not in url, f"{field} looks like a capability URL: {url}"
            assert sent["body"]["access_token"] not in url, (
                f"{field} embeds the callback token: {url}"
            )

    async def test_the_archive_url_names_the_authenticated_download_route(self):
        rows, tsr = _fixture()
        sent = await _dispatch(rows, tsr)
        assert sent["body"]["configuration_version_download_url"].endswith(
            "/api/v1/configuration-versions/cv-33333333-3333-3333-3333-333333333333/download"
        )

    async def test_no_configuration_version_means_no_archive_url(self):
        rows, tsr = _fixture(with_cv=False)
        sent = await _dispatch(rows, tsr)
        assert sent["body"]["configuration_version_download_url"] is None

    @pytest.mark.parametrize("stage", ["post_plan", "pre_apply"])
    async def test_the_plan_url_is_sent_at_the_stages_a_plan_exists(self, stage: str):
        rows, tsr = _fixture()
        rows["TaskStage"].stage = stage
        sent = await _dispatch(rows, tsr)
        assert sent["body"]["plan_json_api_url"].endswith(
            "/api/tfe/v2/plans/22222222-2222-2222-2222-222222222222/json-output"
        )

    async def test_the_plan_url_is_null_at_pre_plan(self):
        """No plan exists yet, so a URL there could only ever 404. The spec
        marks the field conditional on the later stages for the same reason --
        and the KEY is still present, because an omitted key is what breaks a
        strict client."""
        rows, tsr = _fixture()
        rows["TaskStage"].stage = "pre_plan"
        sent = await _dispatch(rows, tsr)
        assert sent["body"]["plan_json_api_url"] is None
        assert "plan_json_api_url" in sent["body"]


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


class TestTheSignatureHeaders:
    """Both signature header names, over the same bytes (#2366).

    Neither name was asserted anywhere before this -- including the
    `X-TFE-Task-Signature` Terrapod has always sent, which is the half an
    existing integration verifies. A constant nothing reads is how a rename
    ships silently, so both are pinned here against the real HMAC.
    """

    async def test_both_names_carry_the_same_signature(self):
        from terrapod.services.run_task_dispatcher import (
            SPEC_SIGNATURE_HEADER,
            sign_payload,
        )

        rows, tsr = _fixture()
        rows["RunTask"].hmac_key = "s3cret"
        sent = await _dispatch(rows, tsr)

        headers = sent["headers"]
        expected = sign_payload(json.dumps(sent["body"]).encode(), "s3cret")

        assert headers["X-TFE-Task-Signature"] == expected, (
            "Terrapod's own header is what an existing integration verifies"
        )
        assert headers[SPEC_SIGNATURE_HEADER] == expected, (
            "the spec's name must carry the same signature, not a different one"
        )
        assert SPEC_SIGNATURE_HEADER == "X-TFC-Task-Signature"

    async def test_no_key_means_no_signature_header_at_all(self):
        """Not an empty one -- an integration checking for presence must not
        see a header carrying nothing."""
        from terrapod.services.run_task_dispatcher import SPEC_SIGNATURE_HEADER

        rows, tsr = _fixture()
        rows["RunTask"].hmac_key = None
        sent = await _dispatch(rows, tsr)

        assert "X-TFE-Task-Signature" not in sent["headers"]
        assert SPEC_SIGNATURE_HEADER not in sent["headers"]


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
