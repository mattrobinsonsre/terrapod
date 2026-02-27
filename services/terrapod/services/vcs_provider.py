"""VCS provider abstraction.

Defines the VCSProvider protocol that GitHub and GitLab implementations
conform to. The poller works against this interface, not specific providers.
"""

from typing import Protocol

from terrapod.db.models import VCSConnection


class PullRequest:
    """Minimal PR/MR representation shared across providers."""

    __slots__ = ("number", "head_sha", "head_ref", "title")

    def __init__(self, number: int, head_sha: str, head_ref: str, title: str) -> None:
        self.number = number
        self.head_sha = head_sha
        self.head_ref = head_ref
        self.title = title


class VCSProvider(Protocol):
    """Interface for VCS provider operations.

    Each method receives the VCSConnection so it can resolve auth
    (GitHub installation token, GitLab PAT, etc.) without global state.
    """

    async def get_branch_sha(
        self, conn: VCSConnection, owner: str, repo: str, branch: str
    ) -> str | None:
        """Get HEAD commit SHA for a branch. Returns None if not found."""
        ...

    async def get_default_branch(
        self, conn: VCSConnection, owner: str, repo: str
    ) -> str | None:
        """Get the repository's default branch name."""
        ...

    async def download_archive(
        self, conn: VCSConnection, owner: str, repo: str, ref: str
    ) -> bytes:
        """Download repository tarball at a given ref."""
        ...

    async def list_open_prs(
        self, conn: VCSConnection, owner: str, repo: str, base_branch: str
    ) -> list[PullRequest]:
        """List open PRs/MRs targeting the given base branch."""
        ...

    def parse_repo_url(self, repo_url: str) -> tuple[str, str] | None:
        """Parse a repo URL into (owner/namespace, repo). Returns None if unparseable."""
        ...
