"""Keeping per-workspace commit statuses off the aggregate statuses' names."""

from terrapod.config import aggregate_statuses_enabled, settings
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

_PREFIX = "terrapod/"


def aggregate_contexts() -> tuple[str, ...]:
    names = settings.vcs.commit_statuses
    return (names.plan_name, names.policy_name, names.apply_name)


def blocks_own_status(workspace_name: str, context: str) -> bool:
    """True, with a warning, when a per-workspace post would overwrite an aggregate status.

    Applies to every per-workspace post (branch, pull request, module impact): a branch
    run's commit can be the head of an open pull request too.
    """
    if not aggregate_statuses_enabled() or context not in aggregate_contexts():
        return False
    logger.warning(
        "Skipping the per-workspace status: its name is an aggregate status name",
        workspace=workspace_name,
        context=context,
    )
    return True


def colliding_workspace_names() -> list[str]:
    """Workspace names whose `terrapod/<name>` status would equal an aggregate name."""
    if not aggregate_statuses_enabled():
        return []
    return sorted(c[len(_PREFIX) :] for c in aggregate_contexts() if c.startswith(_PREFIX))
