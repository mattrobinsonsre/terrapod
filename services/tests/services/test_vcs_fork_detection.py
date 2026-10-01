"""Is this pull request from a fork? (the trust input for speculative plans)

Both providers fail CLOSED: anything that cannot be positively established as
same-repository counts as a fork. The consequence of guessing "trusted" is a
speculative plan running an outsider's code with the workspace's full
credential set, so an unparseable payload must not be read as safe.
"""

from terrapod.services.github_service import _is_fork as gh_is_fork
from terrapod.services.gitlab_service import _is_fork as gl_is_fork


class TestGitHub:
    def test_same_repository_is_not_a_fork(self):
        assert gh_is_fork({"head": {"repo": {"id": 1}}, "base": {"repo": {"id": 1}}}) is False

    def test_a_different_repository_is_a_fork(self):
        assert gh_is_fork({"head": {"repo": {"id": 2}}, "base": {"repo": {"id": 1}}}) is True

    def test_a_deleted_head_repository_counts_as_a_fork(self):
        """GitHub nulls `head.repo` once the fork is deleted. We cannot show it
        was the base, so it is not trusted."""
        assert gh_is_fork({"head": {"repo": None}, "base": {"repo": {"id": 1}}}) is True

    def test_it_falls_back_to_full_name_when_ids_are_absent(self):
        same = {"head": {"repo": {"full_name": "o/r"}}, "base": {"repo": {"full_name": "o/r"}}}
        diff = {"head": {"repo": {"full_name": "x/r"}}, "base": {"repo": {"full_name": "o/r"}}}
        assert gh_is_fork(same) is False
        assert gh_is_fork(diff) is True

    def test_an_unidentifiable_payload_fails_closed(self):
        assert gh_is_fork({"head": {"repo": {}}, "base": {"repo": {}}}) is True
        assert gh_is_fork({}) is True

    def test_head_repo_being_itself_a_fork_is_not_the_question(self):
        """`head.repo.fork` says the head repo is a fork OF SOMETHING — true for
        a PR raised inside a fork against that same fork, which is same-repo and
        trusted. Comparing head to base is the question that matters."""
        pr = {"head": {"repo": {"id": 7, "fork": True}}, "base": {"repo": {"id": 7, "fork": True}}}
        assert gh_is_fork(pr) is False


class TestGitLab:
    def test_same_project_is_not_a_fork(self):
        assert gl_is_fork({"source_project_id": 5, "target_project_id": 5}) is False

    def test_a_different_source_project_is_a_fork(self):
        assert gl_is_fork({"source_project_id": 9, "target_project_id": 5}) is True

    def test_a_missing_project_id_fails_closed(self):
        assert gl_is_fork({"target_project_id": 5}) is True
        assert gl_is_fork({"source_project_id": 5}) is True
        assert gl_is_fork({}) is True
