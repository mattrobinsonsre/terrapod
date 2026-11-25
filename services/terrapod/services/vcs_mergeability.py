"""Telling a block Terrapod inflicted on itself from a real one (#2707).

`terrapod/apply` is `pending` from the moment a run plans until its apply lands.
Make it a required check and GitHub reports the pull request's `mergeable_state`
as `blocked`; `run_service._check_mergeability_or_block` refuses to apply while
the state is blocked; and the status that would clear it cannot go green until
the apply has run. Nothing breaks the loop, which is why
[`docs/vcs-workflows.md`](../../../docs/vcs-workflows.md) told operators never to
require it.

**The self-reference is in the gate, not in the status.** GitHub will not say
*why* a pull request is blocked -- `mergeable_state` is one enum value for every
unmet protection requirement at once -- and the endpoint that would enumerate a
branch's required checks sits behind a repository-administration permission the
App does not have and that every existing installation would have to re-approve.
So Terrapod cannot derive which contexts are required.

What it can do is take the one statement only the operator can make: *"I have
made the apply status a required check."* That is
`api.config.vcs.commit_statuses.apply_is_required_check`, and with it set this
module says that a `blocked` state on GitHub is attributable to Terrapod's own
pending status and the apply may proceed.

**That is sound rather than a loosened gate, because the confirm-time check was
never the enforcement.** The enforcement is the provider's own merge call, which
`vcs_auto_merge._execute_merge` makes after the apply and which applies branch
protection in full; a merge refused there is already a handled outcome, since
anybody can push to the branch between the gate and the merge. The gate is an
advance warning that saves applying when the merge will obviously fail -- and it
keeps every reason that is still worth refusing on. Only `blocked` is
disregarded. `dirty` (merge conflicts) and `behind` (out of date) are real
reasons not to apply and still refuse, as do a draft pull request, a closed one,
and the transient state GitHub returns while it is still computing.

**GitLab is deliberately absent from the mapping below, and that is
documentation-derived** -- we have no GitLab instance to test against, so it is
read off GitLab's published documentation rather than measured. Two reasons, and
either alone is enough. GitLab has no per-name required check: a commit status
posted to `/statuses` becomes a job on the merge request's pipeline (its commit
status API documents searching "existing pipelines ... first to append the job
to", and otherwise creating one with `CI_PIPELINE_SOURCE: external`), so
*Pipelines must succeed* is all-or-nothing and there is nothing to exempt. And
`gitlab_service._GITLAB_MERGEABLE_DETAILED` already accepts `ci_must_pass` and
`ci_still_running` as mergeable, so a pipeline that has not finished does not
refuse the apply by this route in the first place.
"""

from __future__ import annotations

from collections.abc import Mapping

from terrapod.config import aggregate_statuses_enabled, settings

#: Per provider, the merge states a required Terrapod status can produce.
#:
#: Keyed on the provider rather than written as an `if provider == "github"`,
#: so a third provider arrives with an empty set -- refusing, which is the safe
#: direction -- instead of silently taking GitHub's answer.
SELF_INFLICTED_STATES: Mapping[str, frozenset[str]] = {
    # GitHub collapses every unmet protection requirement into this one value,
    # and a required status that is pending is one of them.
    "github": frozenset({"blocked"}),
    # See the module docstring: nothing on GitLab is attributable this way.
    "gitlab": frozenset(),
}


def apply_is_required_check() -> bool:
    """Whether the operator has declared the aggregate apply status required.

    ANDed with the aggregates being on, which is the safe direction: with
    `enabled: false` nothing posts `terrapod/apply`, so there is no status of
    ours for branch protection to be waiting on and no block of ours to
    disregard. Turning the aggregates off must not leave a relaxed apply gate
    behind.
    """
    return aggregate_statuses_enabled() and settings.vcs.commit_statuses.apply_is_required_check


def block_is_self_inflicted(provider: str, state: str) -> bool:
    """True when this provider's `state` is one Terrapod's own required status produces.

    Pure, and takes the operator's declaration from configuration rather than as
    an argument, so no caller can reach the relaxed branch by omitting it.
    """
    if not apply_is_required_check():
        return False
    return state in SELF_INFLICTED_STATES.get(provider, frozenset())
