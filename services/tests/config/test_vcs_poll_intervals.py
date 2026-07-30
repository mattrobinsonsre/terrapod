"""Workspace and module repos poll on separate intervals (#1149).

They were one setting, and that conflated two different problems. A workspace push
is latency-sensitive: the interval is how long a commit waits when nobody
configured a webhook. Module repos are the opposite — far more numerous than they
are active, so polling them at workspace cadence spends most of the VCS API budget
discovering that nothing changed.

Raising the module interval is only safe because **every** module poller now has a
webhook accelerator. Module-impact PR analysis already had one; tag publishing did
not, which made its interval the real auto-publish latency — push a tag, wait. So
this change also wires the accelerator that was missing, and the events for it were
already arriving: GitHub sends a tag push as an ordinary `push` with a
`refs/tags/...` ref, and the GitLab receiver already handled `Tag Push Hook`. They
were simply never connected to this poller.

That is the fact the longer default rests on, so it is asserted rather than left in
a comment.
"""

import inspect

from terrapod.config import VCSConfig


class TestTheTwoIntervalsAreSeparate:
    def test_workspace_polling_stays_at_a_minute(self):
        """The latency-sensitive one. Raising this makes every push wait longer
        wherever a webhook is not configured."""
        assert VCSConfig().poll_interval_seconds == 60

    def test_module_polling_is_five_minutes(self):
        assert VCSConfig().module_poll_interval_seconds == 300

    def test_module_polling_is_longer_than_workspace_polling(self):
        """The relationship, not just the numbers — this is the property that
        makes the split worth having, and it should survive retuning either."""
        config = VCSConfig()

        assert config.module_poll_interval_seconds > config.poll_interval_seconds

    def test_both_are_documented_with_their_reasoning(self):
        """A bare number invites someone to "tidy" the two back into one."""
        fields = VCSConfig.model_fields

        workspace = fields["poll_interval_seconds"].description or ""
        module = fields["module_poll_interval_seconds"].description or ""

        assert "WORKSPACE" in workspace
        assert "MODULE" in module
        # The fact the longer default rests on.
        assert "webhook" in module


class TestEveryModulePollerHasAWebhookAccelerator:
    """What makes the longer module interval safe.

    Tag publishing was the one VCS poller with no accelerator, so its periodic
    interval was the real auto-publish latency. The events were already arriving —
    they were never wired to it.
    """

    def _source(self, module) -> str:
        return inspect.getsource(module)

    def test_the_tag_poller_has_an_immediate_handler(self):
        from terrapod.services import registry_vcs_poller

        assert hasattr(registry_vcs_poller, "handle_registry_vcs_immediate_poll")

    def test_it_is_registered_as_a_trigger_handler(self):
        """A handler nothing dispatches to is a handler that never runs."""
        from terrapod.api import app as app_module

        source = self._source(app_module)

        assert '"registry_vcs_immediate_poll"' in source
        assert "handle_registry_vcs_immediate_poll" in source

    def test_a_github_tag_push_enqueues_it(self):
        from terrapod.api.routers import vcs_events

        source = self._source(vcs_events)
        start = source.index('"registry_vcs_immediate_poll"')
        # The gate is above the enqueue, so look back for it.
        assert "refs/tags/" in source[max(0, start - 600) : start]

    def test_an_ordinary_github_push_does_not(self):
        """Only a tag can produce a new module version. Waking the tag poller on
        every commit would spend the API budget this change exists to save."""
        from terrapod.api.routers import vcs_events

        source = self._source(vcs_events)
        # The enqueue must sit inside a ref check, not beside the unconditional
        # workspace/module-impact enqueues.
        tag_enqueue = source.index('"registry_vcs_immediate_poll"')
        preceding = source[max(0, tag_enqueue - 300) : tag_enqueue]

        assert 'startswith("refs/tags/")' in preceding or "Tag Push Hook" in preceding

    def test_a_gitlab_tag_push_hook_enqueues_it(self):
        from terrapod.api.routers import vcs_events

        source = self._source(vcs_events)

        assert 'event_type == "Tag Push Hook"' in source


class TestTheModulePollersUseTheModuleInterval:
    """Asserted against the scheduler wiring rather than trusted, because the
    setting existing is not the same as anything reading it."""

    def _lifespan_source(self) -> str:
        from terrapod.api import app as app_module

        return inspect.getsource(app_module)

    def test_tag_polling_and_impact_polling_use_it(self):
        source = self._lifespan_source()

        for task in ("registry_vcs_poll", "module_impact_poll"):
            start = source.index(f'"{task}"')
            block = source[start : start + 400]
            assert "settings.vcs.module_poll_interval_seconds" in block, task

    def test_workspace_polling_still_uses_the_workspace_interval(self):
        """The regression that would undo the point of the change."""
        source = self._lifespan_source()
        start = source.index('"vcs_poll"')
        block = source[start : start + 300]

        assert "settings.vcs.poll_interval_seconds" in block
        assert "module_poll_interval_seconds" not in block

    def test_policy_set_sync_is_deliberately_left_on_the_workspace_interval(self):
        """Policy sets are code a workspace's runs are evaluated against, not
        module repos, and they have their own webhook accelerator. Moving them
        would be a separate decision — pinned so it is made deliberately rather
        than by someone assuming all non-workspace polling belongs together."""
        source = self._lifespan_source()
        start = source.index('"policy_vcs_poll"')
        block = source[start : start + 300]

        assert "settings.vcs.poll_interval_seconds" in block
        assert "module_poll_interval_seconds" not in block
