"""A per-workspace status must never carry an aggregate status's name."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import settings
from terrapod.services import vcs_status_names as names


@pytest.fixture
def cfg():
    c = settings.vcs.commit_statuses
    saved = (c.plan_name, c.policy_name, c.apply_name)
    yield c
    c.plan_name, c.policy_name, c.apply_name = saved


class TestBlocksOwnStatus:
    @pytest.mark.parametrize("ws", ["plan", "policy", "apply"])
    def test_the_default_names_collide(self, ws):
        with patch.object(names, "logger") as log:
            assert names.blocks_own_status(ws, f"terrapod/{ws}") is True
        log.warning.assert_called_once()

    def test_a_similar_name_does_not(self):
        with patch.object(names, "logger") as log:
            assert names.blocks_own_status("plans", "terrapod/plans") is False
        log.warning.assert_not_called()

    def test_a_configured_name_equal_to_a_workspace_status_collides(self, cfg):
        cfg.plan_name = "terrapod/prod-vpc"
        assert names.blocks_own_status("prod-vpc", "terrapod/prod-vpc") is True

    def test_an_aggregate_name_outside_the_terrapod_prefix_frees_the_default_one(self, cfg):
        cfg.plan_name = "ci/tf-plan"
        assert names.blocks_own_status("plan", "terrapod/plan") is False
        assert names.blocks_own_status("ci/tf-plan", "ci/tf-plan") is True


class TestSwitchedOff:
    def test_nothing_collides_when_there_are_no_aggregates(self):
        settings.vcs.commit_statuses.enabled = False
        try:
            assert names.blocks_own_status("plan", "terrapod/plan") is False
            assert names.colliding_workspace_names() == []
        finally:
            settings.vcs.commit_statuses.enabled = True


class TestWhichWorkspaceNamesCollide:
    def test_defaults(self):
        assert names.colliding_workspace_names() == ["apply", "plan", "policy"]

    def test_custom_names_outside_the_prefix_collide_with_nothing(self, cfg):
        cfg.plan_name, cfg.policy_name, cfg.apply_name = "ci/p", "ci/q", "ci/r"
        assert names.colliding_workspace_names() == []

    def test_a_custom_terrapod_name(self, cfg):
        cfg.plan_name = "terrapod/checks"
        assert names.colliding_workspace_names() == ["apply", "checks", "policy"]


class TestModuleImpactPostsAreChecked:
    async def test_the_unusable_candidate_check_is_skipped_for_a_colliding_workspace(self):
        from terrapod.services import module_impact_service as mis

        conn = MagicMock(provider="github")
        ws = MagicMock()
        ws.name = "plan"
        with patch.object(mis.github_service, "create_commit_status", new=AsyncMock()) as post:
            done = await mis._post_unusable_module_pr_status(
                conn, "o", "r", MagicMock(head_sha="a" * 40), ws, "bad"
            )
        post.assert_not_awaited()
        assert done is True  # final: retrying cannot help

    async def test_a_normal_workspace_still_gets_it(self):
        from terrapod.services import module_impact_service as mis

        conn = MagicMock(provider="github")
        ws = MagicMock()
        ws.name = "prod-vpc"
        with patch.object(mis.github_service, "create_commit_status", new=AsyncMock()) as post:
            await mis._post_unusable_module_pr_status(
                conn, "o", "r", MagicMock(head_sha="a" * 40), ws, "bad"
            )
        post.assert_awaited_once()
        assert post.await_args.kwargs["context"] == "terrapod/prod-vpc"

    async def test_the_run_status_post_is_skipped_for_a_colliding_workspace(self):
        import uuid

        from terrapod.services import module_impact_service as mis

        conn = MagicMock(provider="github", status="active")
        module = MagicMock(vcs_connection_id=uuid.uuid4(), vcs_repo_url="https://github.com/o/r")
        ws = MagicMock(id=uuid.uuid4())
        ws.name = "policy"
        run = MagicMock(
            workspace_id=ws.id,
            id=uuid.uuid4(),
            vcs_commit_sha="a" * 40,
            plan_only=True,
            has_changes=False,
            vcs_pull_request_number=None,
        )
        db = MagicMock()
        db.get = AsyncMock(side_effect=[conn, ws])
        with (
            patch.object(mis.github_service, "create_commit_status", new=AsyncMock()) as post,
            patch.object(mis, "_dispatch_parse_repo_url", return_value=lambda _: ("o", "r")),
        ):
            await mis._post_module_vcs_status(db, run, module, "planned")
        post.assert_not_awaited()
