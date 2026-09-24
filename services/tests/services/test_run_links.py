"""The shared run-URL helper, and the contracts its callers still depend on.

Five call sites built this string independently before it was hoisted. These
tests pin the two things that made hoisting risky: that an unset
`external_url` still means "no link" at every caller, and that the two id
forms in use are both passed through unchanged.
"""

from unittest.mock import patch

from terrapod.services import notification_service, run_links, slack_notify_service


class TestRunUrl:
    def test_builds_the_run_page_url(self):
        from terrapod.config import settings

        with patch.object(settings, "external_url", "https://terrapod.example"):
            assert run_links.run_url("w1", "r1") == "https://terrapod.example/workspaces/w1/runs/r1"

    def test_trailing_slash_is_not_doubled(self):
        from terrapod.config import settings

        with patch.object(settings, "external_url", "https://terrapod.example/"):
            assert run_links.run_url("w1", "r1").count("//") == 1  # only the scheme's

    def test_none_when_no_external_url(self):
        """ "No link" and "an empty link" are different facts for a renderer."""
        from terrapod.config import settings

        with patch.object(settings, "external_url", ""):
            assert run_links.run_url("w1", "r1") is None

    def test_prefixed_ids_are_passed_through_not_normalised(self):
        """Notifications link with prefixed ids; rewriting them would change
        every URL already sent. Both forms resolve via `parse_id`."""
        from terrapod.config import settings

        with patch.object(settings, "external_url", "https://terrapod.example"):
            assert run_links.run_url("ws-abc", "run-def").endswith(
                "/workspaces/ws-abc/runs/run-def"
            )


class TestCallerContractsSurviveTheHoist:
    """The two named adapters keep returning "" — their consumers expect it."""

    def test_notification_service_still_returns_empty_string(self):
        from terrapod.config import settings

        with patch.object(settings, "external_url", ""):
            assert notification_service.run_ui_url("ws-a", "run-b") == ""

    def test_slack_still_returns_empty_string(self):
        from terrapod.config import settings

        with patch.object(settings, "external_url", ""):
            assert slack_notify_service.run_url("a", "b") == ""

    def test_notification_service_keeps_its_url(self):
        from terrapod.config import settings

        with patch.object(settings, "external_url", "https://terrapod.example"):
            assert (
                notification_service.run_ui_url("ws-a", "run-b")
                == "https://terrapod.example/workspaces/ws-a/runs/run-b"
            )

    def test_slack_keeps_its_url(self):
        from terrapod.config import settings

        with patch.object(settings, "external_url", "https://terrapod.example"):
            assert (
                slack_notify_service.run_url("a", "b")
                == "https://terrapod.example/workspaces/a/runs/b"
            )
