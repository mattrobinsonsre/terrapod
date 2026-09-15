"""module_autodiscovery_repositories: per-repository scan state for module rules

Org-wide module autodiscovery (#1620). A rule's `repo-url` may now name one
repository, an org or group, or a pattern over one namespace's repositories, so
the scan state that lived on the rule — the head it last scanned, the
directories it has seen, when it took its baseline — moves to one row per
(rule, repository).

The rule gains what the server decided about `repo-url` when it was saved
(`target_kind`, `target_id`), when the namespace was last listed in full
(`last_enumerated_at`), and why the last poll could not do its work
(`last_error`).

**Expand only.** Every existing rule gets one state row, backfilled from its
own `last_scanned_sha`, `seen_subdirectories` and `first_scan_at` with
`origin = baseline`, so a single-repository rule continues exactly where it
was. The old per-rule columns are NOT dropped: the new code keeps writing them
for repository-target rules, so a replica still on older code during a rolling
upgrade reads current state and registers nothing twice. They are dropped in
1.8, as a ledgered contraction.

Release line 1.7. Written on release/v1.7 on top of the line's head, and carried
to main at the same point with main's next migration re-parented onto it — see
"A release line's migrations are a prefix of main's" in AGENTS.md.

Revision ID: f9b3aac00aac
Revises: a7c3e91f52d4
"""

import json
import re
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f9b3aac00aac"
down_revision: str | None = "a7c3e91f52d4"
branch_labels = None
depends_on = None

_TABLE = "module_autodiscovery_repositories"
_GIT_SUFFIX = re.compile(r"(\.git)?/*$", re.IGNORECASE)


def _repo_path(repo_url: str, server_url: str, provider: str) -> str:
    """The `owner/repo` (or `group/subgroup/project`) a stored URL names.

    Self-contained on purpose: a migration must not import application code
    that later changes under it. Strips the scheme and host (or the `git@host:`
    prefix), a GitLab instance's relative URL root, `.git` and trailing
    slashes. Falls back to the URL itself, so a row always gets a path.
    """
    url = (repo_url or "").strip()
    if url.startswith("git@") and ":" in url:
        path = url.split(":", 1)[1]
    elif "://" in url:
        path = url.split("://", 1)[1].partition("/")[2]
        root = urlsplit(server_url or "").path.strip("/") if provider == "gitlab" else ""
        if root and path.lower().startswith(root.lower() + "/"):
            path = path[len(root) + 1 :]
    else:
        path = url
    path = _GIT_SUFFIX.sub("", path.strip("/"))
    return path or url


def upgrade() -> None:
    op.add_column(
        "module_autodiscovery_rules",
        sa.Column("target_kind", sa.String(16), nullable=False, server_default="repository"),
    )
    op.add_column(
        "module_autodiscovery_rules",
        sa.Column("target_id", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column(
        "module_autodiscovery_rules",
        sa.Column("last_enumerated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "module_autodiscovery_rules",
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
    )

    table = op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "rule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("module_autodiscovery_rules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("repo_path", sa.String(1024), nullable=False),
        sa.Column("repo_url", sa.String(2048), nullable=False, server_default=""),
        sa.Column("vcs_repo_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("default_branch", sa.String(255), nullable=False, server_default=""),
        sa.Column("origin", sa.String(16), nullable=False, server_default="baseline"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("change_marker", sa.String(64), nullable=False, server_default=""),
        sa.Column("last_scanned_sha", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "seen_subdirectories",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "candidates", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "last_skips", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "previous_paths",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("repo_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("rule_id", "repo_path", name="uq_module_autodiscovery_repo_path"),
    )
    op.create_index(
        "uq_module_autodiscovery_repo_id",
        _TABLE,
        ["rule_id", "vcs_repo_id"],
        unique=True,
        postgresql_where=sa.text("vcs_repo_id <> ''"),
    )
    op.create_index("ix_module_autodiscovery_repo_next_check", _TABLE, ["rule_id", "next_check_at"])

    # Backfill: one state row per existing rule, carrying its scan state over,
    # so a repository rule polls on from exactly where it was. The JSONB is
    # read as text and decoded here, so what is inserted does not depend on
    # the driver's JSON codec.
    bind = op.get_bind()
    rules = bind.execute(
        sa.text(
            "SELECT r.id, r.repo_url, r.first_scan_at, r.last_scanned_sha, "
            "r.seen_subdirectories::text AS seen, c.server_url, c.provider "
            "FROM module_autodiscovery_rules r "
            "JOIN vcs_connections c ON c.id = r.vcs_connection_id"
        )
    ).all()
    now = datetime.now(UTC)
    rows = [
        {
            "id": uuid.uuid4(),
            "rule_id": r.id,
            "repo_path": _repo_path(r.repo_url, r.server_url or "", r.provider or ""),
            "repo_url": r.repo_url,
            "vcs_repo_id": "",
            "default_branch": "",
            "origin": "baseline",
            "status": "active",
            "change_marker": "",
            "last_scanned_sha": r.last_scanned_sha or "",
            "seen_subdirectories": json.loads(r.seen or "[]"),
            "candidates": [],
            "last_skips": [],
            "previous_paths": [],
            "first_seen_at": r.first_scan_at or now,
            "failure_count": 0,
            "last_error": "",
            "created_at": now,
            "updated_at": now,
        }
        for r in rules
    ]
    if rows:
        op.bulk_insert(table, rows)


def downgrade() -> None:
    # The per-rule columns were kept current throughout, so nothing in the
    # state rows needs carrying back before they go.
    op.drop_index("ix_module_autodiscovery_repo_next_check", table_name=_TABLE)
    op.drop_index("uq_module_autodiscovery_repo_id", table_name=_TABLE)
    op.drop_table(_TABLE)
    op.drop_column("module_autodiscovery_rules", "last_error")
    op.drop_column("module_autodiscovery_rules", "last_enumerated_at")
    op.drop_column("module_autodiscovery_rules", "target_id")
    op.drop_column("module_autodiscovery_rules", "target_kind")
