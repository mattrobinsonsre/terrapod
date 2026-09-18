"""What a Pulumi preview reports back (#1560).

The Terraform path posts two things after a plan: `has_changes`, which the
no-op short-circuit, drift and conditional auto-apply read, and the plan JSON
behind the change badges and the post-plan consumers. The Pulumi path posted
neither, so every Pulumi run had `has_changes` unknown and nothing to read.

The preview now also writes its engine event log, which is reduced to a digest.
These tests use real Pulumi event lines: a JSON object per line, `summaryEvent`
last.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from terrapod.runner.phases import pulumi_exec, pulumi_preview


def _events(*lines: dict) -> str:
    return "".join(json.dumps(line) + "\n" for line in lines)


def _summary(**changes) -> dict:
    return {"summaryEvent": {"durationSeconds": 3, "resourceChanges": changes}}


def _step(op: str, urn: str = "urn:pulumi:dev::p::aws:s3/bucket:Bucket::b") -> dict:
    return {
        "resourcePreEvent": {
            "metadata": {
                "op": op,
                "urn": urn,
                "type": "aws:s3/bucket:Bucket",
                # The engine also sends the resource's state, which is where a
                # stack's secrets live. The digest must not carry it.
                "new": {"inputs": {"password": "hunter2"}},
            }
        }
    }


class TestReadingTheEventLog:
    def test_a_preview_with_changes(self, tmp_path):
        log = tmp_path / "events.json"
        log.write_text(
            _events(_step("create"), _step("update"), _summary(create=1, update=1, same=7))
        )

        digest = pulumi_preview.parse_event_log(log)

        assert digest["has_changes"] is True
        assert digest["change_summary"] == {"create": 1, "update": 1, "same": 7}
        assert [s["op"] for s in digest["steps"]] == ["create", "update"]
        assert digest["engine"] == "pulumi"

    def test_a_preview_with_nothing_to_do(self, tmp_path):
        log = tmp_path / "events.json"
        log.write_text(_events(_summary(same=12)))

        assert pulumi_preview.parse_event_log(log)["has_changes"] is False

    def test_an_operation_this_code_has_never_heard_of_still_counts_as_a_change(self):
        # A newer Pulumi must never make a run look emptier than it is.
        assert pulumi_preview.has_changes({"same": 4, "quantum-entangle": 1}) is True
        assert pulumi_preview.has_changes({"same": 4}) is False

    def test_no_summary_means_no_answer(self, tmp_path):
        # A preview killed part-way through: `has_changes` stays unknown rather
        # than being guessed from the steps that did arrive.
        log = tmp_path / "events.json"
        log.write_text(_events(_step("create")))

        assert pulumi_preview.parse_event_log(log) is None

    def test_a_missing_log_is_not_an_error(self, tmp_path):
        assert pulumi_preview.parse_event_log(tmp_path / "nope.json") is None

    def test_a_truncated_last_line_does_not_lose_the_rest(self, tmp_path):
        log = tmp_path / "events.json"
        log.write_text(_events(_step("create"), _summary(create=1)) + '{"summaryEve')

        digest = pulumi_preview.parse_event_log(log)

        assert digest["change_summary"] == {"create": 1}

    def test_the_digest_carries_no_resource_state(self, tmp_path):
        log = tmp_path / "events.json"
        log.write_text(_events(_step("create"), _summary(create=1)))

        digest = pulumi_preview.parse_event_log(log)

        assert "hunter2" not in json.dumps(digest)
        assert set(digest["steps"][0]) == {"op", "urn", "type"}

    def test_a_huge_preview_keeps_its_counts_and_caps_its_steps(self, tmp_path):
        log = tmp_path / "events.json"
        many = [
            _step("create", f"urn:pulumi:dev::p::t::r{i}")
            for i in range(pulumi_preview.MAX_STEPS + 50)
        ]
        log.write_text(_events(*many, _summary(create=pulumi_preview.MAX_STEPS + 50)))

        digest = pulumi_preview.parse_event_log(log)

        assert len(digest["steps"]) == pulumi_preview.MAX_STEPS
        assert digest["steps_truncated"] is True
        assert digest["change_summary"]["create"] == pulumi_preview.MAX_STEPS + 50


class TestThePreviewIsAskedForIt:
    def test_the_preview_writes_an_event_log(self):
        argv = pulumi_exec.preview_argv("", None, event_log="/workspace/events.json")
        assert "--event-log=/workspace/events.json" in argv
        # Not --json: that would replace the output a person reads.
        assert "--json" not in argv

    def test_it_still_saves_a_plan_when_the_workspace_binds_one(self):
        argv = pulumi_exec.preview_argv("/workspace/plan.json", None, event_log="/w/e.json")
        assert "--save-plan=/workspace/plan.json" in argv
        assert "--event-log=/w/e.json" in argv

    def test_asking_for_neither_is_the_previous_command(self):
        assert pulumi_exec.preview_argv("", None) == ["preview", "--non-interactive"]


class TestReportingIt:
    def _cfg(self):
        return MagicMock(has_api=True, run_id="01a0", api_url="https://tp", auth_token="t")

    def _run(self, tmp_path, lines: str):
        log = tmp_path / "events.json"
        log.write_text(lines)
        from terrapod.runner import job_entrypoint

        with (
            patch("terrapod.runner.phases.uploads.post_plan_result") as post,
            patch("terrapod.runner.phases.uploads.upload_plan_json") as upload,
        ):
            job_entrypoint._report_pulumi_preview(self._cfg(), log)
        return post, upload, log

    def test_it_posts_the_result_and_uploads_the_digest(self, tmp_path):
        post, upload, log = self._run(tmp_path, _events(_step("delete"), _summary(delete=1)))

        post.assert_called_once()
        assert post.call_args.kwargs["has_changes"] is True
        upload.assert_called_once()
        digest = json.loads(Path(upload.call_args.args[1]).read_text())
        assert digest["change_summary"] == {"delete": 1}

    def test_a_preview_with_no_changes_says_so(self, tmp_path):
        post, _upload, _log = self._run(tmp_path, _events(_summary(same=3)))

        assert post.call_args.kwargs["has_changes"] is False

    def test_an_unfinished_preview_reports_nothing(self, tmp_path):
        post, upload, _log = self._run(tmp_path, _events(_step("create")))

        post.assert_not_called()
        upload.assert_not_called()

    def test_a_failed_upload_does_not_stop_the_result(self, tmp_path):
        # Best-effort, in the order that matters: the digest is uploaded first
        # so the counts are there when plan-result drives the transition, but a
        # failure must not leave `has_changes` unknown as well.
        log = tmp_path / "events.json"
        log.write_text(_events(_summary(create=2)))
        from terrapod.runner import job_entrypoint

        with (
            patch("terrapod.runner.phases.uploads.upload_plan_json", side_effect=OSError("boom")),
            patch("terrapod.runner.phases.uploads.post_plan_result") as post,
        ):
            job_entrypoint._report_pulumi_preview(self._cfg(), log)

        post.assert_called_once()

    def test_nothing_is_reported_without_an_api(self, tmp_path):
        log = tmp_path / "events.json"
        log.write_text(_events(_summary(create=1)))
        from terrapod.runner import job_entrypoint

        with (
            patch("terrapod.runner.phases.uploads.post_plan_result") as post,
            patch("terrapod.runner.phases.uploads.upload_plan_json") as upload,
        ):
            job_entrypoint._report_pulumi_preview(MagicMock(has_api=False), log)

        post.assert_not_called()
        upload.assert_not_called()


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (
            {"create": 2, "same": 1},
            {"additions": 2, "changes": 0, "destructions": 0, "replacements": 0, "imports": 0},
        ),
        (
            {"update": 3},
            {"additions": 0, "changes": 3, "destructions": 0, "replacements": 0, "imports": 0},
        ),
        (
            {"delete": 1},
            {"additions": 0, "changes": 0, "destructions": 1, "replacements": 0, "imports": 0},
        ),
        (
            {"replace": 2},
            {"additions": 0, "changes": 0, "destructions": 0, "replacements": 2, "imports": 0},
        ),
        (
            # `replace` already counts the pair; the halves must not be added again.
            {"replace": 1, "create-replacement": 1, "delete-replaced": 1},
            {"additions": 0, "changes": 0, "destructions": 0, "replacements": 1, "imports": 0},
        ),
        (
            {"import": 2},
            {"additions": 0, "changes": 0, "destructions": 0, "replacements": 0, "imports": 2},
        ),
        (
            {"same": 9},
            {"additions": 0, "changes": 0, "destructions": 0, "replacements": 0, "imports": 0},
        ),
    ],
)
def test_the_digest_counts_map_onto_the_platforms_columns(summary, expected):
    from terrapod.services.plan_summary import summarize_plan_json

    body = json.dumps({"engine": "pulumi", "change_summary": summary}).encode()
    assert summarize_plan_json(body) == expected


class TestWhetherTheUpdateIsBoundToThePreview:
    """The run says it, not just the workspace (#1553, #1560).

    It is a property of the decision someone is about to confirm: bound, the
    update performs the operations the preview showed; unbound, it works out
    its own. The run page shows it next to the confirm action.
    """

    def _json(self, ws):
        from terrapod.api.routers.runs import _bind_plan_of, _run_json
        from terrapod.services import run_service

        run = MagicMock(status="planned", plan_only=False, plan_finished_at=None)
        for attr in (
            "vcs_commit_sha",
            "vcs_branch",
            "vcs_pull_request_number",
            "configuration_version_id",
            "created_by",
        ):
            setattr(run, attr, None)
        with patch.object(run_service, "resolve_auto_apply_mode", return_value="never"):
            return _run_json(run, engine="pulumi", pulumi_bind_plan=_bind_plan_of(ws))["data"][
                "attributes"
            ]["pulumi-bind-plan"]

    def test_a_bound_pulumi_workspace(self):
        assert self._json(MagicMock(engine="pulumi", pulumi_bind_plan=True)) is True

    def test_an_unbound_pulumi_workspace(self):
        assert self._json(MagicMock(engine="pulumi", pulumi_bind_plan=False)) is False

    def test_a_terraform_run_has_no_such_question(self):
        # Terraform always applies its own saved plan; reporting false would
        # read as "unbound", which is not a state it can be in.
        assert self._json(MagicMock(engine="terraform", pulumi_bind_plan=False)) is None
        assert self._json(None) is None
