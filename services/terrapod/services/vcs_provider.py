"""VCS provider abstraction.

Defines the VCSProvider protocol that GitHub and GitLab implementations
conform to. The poller works against this interface, not specific providers.
"""

from dataclasses import dataclass
from typing import Protocol

from terrapod.db.models import VCSConnection


class PullRequest:
    """Minimal PR/MR representation shared across providers."""

    __slots__ = (
        "number",
        "head_sha",
        "head_ref",
        "title",
        "draft",
        "author_login",
        "state",
        "merged",
    )

    def __init__(
        self,
        number: int,
        head_sha: str,
        head_ref: str,
        title: str,
        draft: bool = False,
        author_login: str = "",
        state: str = "",
        merged: bool = False,
    ) -> None:
        self.number = number
        self.head_sha = head_sha
        self.head_ref = head_ref
        self.title = title
        self.draft = draft
        self.author_login = author_login
        # `state` is provider-native ("open"/"closed" for GitHub;
        # "opened"/"closed"/"merged"/"locked" for GitLab). `merged` is
        # the normalised "did this PR/MR actually merge?" — used by the
        # autodiscovery orphan reconciler to tell a merged origin PR
        # (workspace graduated) from a closed-unmerged one (orphan).
        self.state = state
        self.merged = merged


@dataclass(frozen=True)
class MergeabilityStatus:
    """Result of a "can this PR be merged right now?" check.

    `mergeable` is the boolean we gate the apply on. `state` is the
    provider-native string surfaced verbatim on the PR comment so users
    see the same language their VCS uses (`dirty`, `blocked`, `behind`,
    `mergeable`, `unknown`, etc.). `reason` is a human-readable summary
    we synthesise from the provider response — used when the PR comment
    needs more than the bare state string (e.g. "blocked: review
    required by branch protection").

    `unknown=True` means the provider has not yet computed mergeability
    (GitHub returns `mergeable: null` briefly after a push). Callers
    should retry rather than treating this as "blocked".
    """

    mergeable: bool
    state: str
    reason: str
    unknown: bool = False


@dataclass(frozen=True)
class PRComment:
    """A comment on a PR/MR (the conversational kind, not a code review)."""

    id: str  # provider-side comment id, as a string for cross-provider portability
    body: str
    author_login: str
    author_user_id: str
    created_at: str  # ISO 8601
    updated_at: str


@dataclass(frozen=True)
class PRReview:
    """A PR/MR review. Used to detect approvals for apply gating."""

    id: str
    state: str  # "approved" / "changes_requested" / "commented" / etc.
    author_login: str
    submitted_at: str


@dataclass(frozen=True)
class PRMergeResult:
    """Outcome of a merge attempt. `merged=True` means the API confirmed merge.

    `message` is the provider-side response message (typically the
    commit message GitHub used). `error_reason` is set when `merged=False`
    and contains the provider's rejection reason, surfaced verbatim.
    """

    merged: bool
    sha: str = ""
    message: str = ""
    error_reason: str = ""


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

    async def get_default_branch(self, conn: VCSConnection, owner: str, repo: str) -> str | None:
        """Get the repository's default branch name."""
        ...

    async def download_archive(self, conn: VCSConnection, owner: str, repo: str, ref: str) -> bytes:
        """Download repository tarball at a given ref."""
        ...

    async def list_open_prs(
        self, conn: VCSConnection, owner: str, repo: str, base_branch: str
    ) -> list[PullRequest]:
        """List open PRs/MRs targeting the given base branch."""
        ...

    async def get_changed_files(
        self, conn: VCSConnection, owner: str, repo: str, base_sha: str, head_sha: str
    ) -> list[str] | None:
        """Get list of file paths changed between two commits.

        Returns None if the response is truncated, signaling the caller
        should skip filtering and create the run unconditionally.
        """
        ...

    async def list_branches(
        self, conn: VCSConnection, owner: str, repo: str
    ) -> list[dict[str, str]]:
        """List repository branches. Returns [{"name": str, "sha": str}]."""
        ...

    async def list_tags(self, conn: VCSConnection, owner: str, repo: str) -> list[dict[str, str]]:
        """List repository tags. Returns [{"name": str, "sha": str}]."""
        ...

    def parse_repo_url(self, repo_url: str) -> tuple[str, str] | None:
        """Parse a repo URL into (owner/namespace, repo). Returns None if unparseable."""
        ...

    # ── Apply-then-merge surface (#282) ─────────────────────────────────

    async def get_pull_request(
        self, conn: VCSConnection, owner: str, repo: str, pr_number: int
    ) -> PullRequest | None:
        """Fetch a single PR/MR's current state (including draft flag).

        Used when we have a PR number from a webhook event and need its
        current state. Returns None if not found.
        """
        ...

    async def is_mergeable(
        self, conn: VCSConnection, owner: str, repo: str, pr_number: int
    ) -> MergeabilityStatus:
        """Check whether a PR/MR is currently mergeable per the VCS provider.

        This is the apply gate for apply-then-merge mode — branch
        protection, required reviews, status checks, draft state, etc.
        are all the VCS provider's domain. We surface its decision
        verbatim.
        """
        ...

    async def merge_pull_request(
        self,
        conn: VCSConnection,
        owner: str,
        repo: str,
        pr_number: int,
        strategy: str,
        commit_title: str = "",
        commit_message: str = "",
    ) -> PRMergeResult:
        """Merge a PR/MR via the provider's merge API.

        `strategy` is one of `merge` / `squash` / `rebase`. Failure
        modes (conflicts, protection rules) come back as
        `merged=False` with `error_reason` populated.
        """
        ...

    async def list_pr_comments(
        self,
        conn: VCSConnection,
        owner: str,
        repo: str,
        pr_number: int,
        since: str | None = None,
    ) -> list[PRComment]:
        """List conversational comments on a PR/MR.

        `since` is an ISO 8601 timestamp; the provider filters server-
        side where supported (`?sort=asc&order_by=updated_at` for
        GitLab; `?since=` for GitHub). Caller is responsible for
        client-side de-duplication via `PRSession.last_processed_comment_id`.
        """
        ...

    async def post_pr_comment(
        self,
        conn: VCSConnection,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> str:
        """Post a new conversational comment on a PR/MR.

        Returns the provider-side comment id (stringified for
        portability). Used for the announcement comment when a Terrapod
        UI user clicks "Confirm and Apply".
        """
        ...

    async def update_pr_comment(
        self,
        conn: VCSConnection,
        owner: str,
        repo: str,
        comment_id: str,
        body: str,
    ) -> None:
        """Update (edit-in-place) the body of an existing PR comment.

        Used by the status-comment surface so the same comment is
        re-rendered on every state transition rather than appending a
        new comment per update.
        """
        ...

    async def list_pr_reviews(
        self,
        conn: VCSConnection,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> list[PRReview]:
        """List reviews on a PR (GitHub) or approvals on an MR (GitLab).

        Used to detect approval-state transitions for the mergeability
        gate, alongside the provider's `is_mergeable` summary.
        """
        ...
