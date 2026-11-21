"""
Tests for storage key path helpers.
"""

import pytest

from terrapod.storage.keys import (
    WORKSPACE_KEY_PREFIXES,
    apply_log_key,
    config_version_key,
    plan_log_key,
    plan_output_key,
    policy_set_key,
    shared_prefix,
    state_backup_key,
    state_key,
)


class TestKeyHelpers:
    def test_state_key(self) -> None:
        assert state_key("ws-123", "sv-456") == "state/ws-123/sv-456.tfstate"

    def test_state_backup_key(self) -> None:
        assert state_backup_key("ws-123", "sv-456") == "state/ws-123/sv-456.backup.tfstate"

    def test_plan_log_key(self) -> None:
        assert plan_log_key("ws-123", "run-789") == "logs/ws-123/plans/run-789.log"

    def test_apply_log_key(self) -> None:
        assert apply_log_key("ws-123", "run-789") == "logs/ws-123/applies/run-789.log"

    def test_plan_output_key(self) -> None:
        assert plan_output_key("ws-123", "run-789") == "plans/ws-123/run-789.tfplan"

    def test_config_version_key(self) -> None:
        assert config_version_key("ws-123", "cv-001") == "config/ws-123/cv-001.tar.gz"

    def test_policy_set_key(self) -> None:
        assert policy_set_key("ps-abc", "pv-001") == "policies/ps-abc/pv-001.tar.gz"


class TestSharedPrefix:
    #: Literal (input, expected) pairs, NOT computed from the implementation's own
    #: expression (#2621). The old table asserted
    #: `prefix if prefix.endswith("/") else prefix + "/"`, which is the body of the
    #: function under test -- so it could not surprise anyone, and it passed happily
    #: on `("", "/")`, an absolute key. Written out, the empty case is in front of
    #: whoever reads it.
    NORMALISED = [
        ("backups/", "backups/"),
        ("backups", "backups/"),
        ("dr/state-dumps/", "dr/state-dumps/"),
        # A leading `./` comes OFF, rather than being stripped for the check and
        # handed back: the filesystem store writes `./backups/x` to `root/backups/x`
        # and then lists `./backups/` to nothing, so retention never pruned.
        ("./backups/", "backups/"),
        ("./backups", "backups/"),
        ("././backups/", "backups/"),
        ("/backups/", "backups/"),
        ("//backups", "backups/"),
        # Only `./` is leading junk. A hidden directory is a real prefix.
        (".backups/", ".backups/"),
    ]

    def test_the_table_is_not_empty(self) -> None:
        """An emptied parametrize is a skipped test, not a failing one."""
        assert len(self.NORMALISED) >= 9

    @pytest.mark.parametrize(("prefix", "expected"), NORMALISED)
    def test_a_shared_prefix_is_normalised(self, prefix: str, expected: str) -> None:
        assert shared_prefix("backup.prefix", prefix) == expected

    @pytest.mark.parametrize("prefix", ["", ".", "./", "/", "././", "//", ".//"])
    def test_a_prefix_naming_nothing_is_refused(self, prefix: str) -> None:
        """`""` used to return `/`, and three legs disagreed about it (#2621).

        The filesystem store refuses an absolute key -- after a full pg_dump has
        run -- and the retention sweep would list every object in the store.
        """
        with pytest.raises(ValueError, match="names no prefix"):
            shared_prefix("backup.prefix", prefix)

    @pytest.mark.parametrize("prefix", ["vcs_archives/", "vcs_archives", "vcs_archives/db/"])
    def test_an_age_swept_prefix_is_refused(self, prefix: str) -> None:
        """Its own refusal and its own message: the hazard is deletion, not misreading.

        `artifact_retention_service` lists this prefix whole and deletes by age with
        no filter on what the objects are, so dumps there last until the sweep
        reaches them -- `vcs.archive_cache_retention_days`, 7 days by default.
        """
        with pytest.raises(ValueError, match="sweeps by age"):
            shared_prefix("backup.prefix", prefix)

    @pytest.mark.parametrize("prefix", ["./vcs_archives/", "/vcs_archives/"])
    def test_the_age_swept_refusal_sees_through_a_spelling(self, prefix: str) -> None:
        with pytest.raises(ValueError, match="sweeps by age"):
            shared_prefix("backup.prefix", prefix)

    def test_the_refusals_do_not_claim_the_list_is_exhaustive(self) -> None:
        """The message used to read as "anything not on this list is fine"."""
        with pytest.raises(ValueError, match="not the same as being"):
            shared_prefix("backup.prefix", "state/")

    @pytest.mark.parametrize("workspace_prefix", WORKSPACE_KEY_PREFIXES)
    @pytest.mark.parametrize("suffix", ["", "db/"])
    def test_a_workspace_scoped_prefix_is_refused(self, workspace_prefix: str, suffix: str) -> None:
        with pytest.raises(ValueError, match="workspace-scoped prefix"):
            shared_prefix("backup.prefix", workspace_prefix + suffix)

    @pytest.mark.parametrize("prefix", ["./state/", "././logs", "./state/db"])
    def test_a_dot_slash_spelling_is_refused(self, prefix: str) -> None:
        with pytest.raises(ValueError, match="workspace-scoped prefix"):
            shared_prefix("backup.prefix", prefix)

    def test_a_prefix_without_its_slash_is_refused(self) -> None:
        with pytest.raises(ValueError, match="'state/'"):
            shared_prefix("backup.prefix", "state")
