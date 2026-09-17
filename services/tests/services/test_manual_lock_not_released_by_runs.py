"""The run lifecycle never releases the workspace lock (#1705).

`workspace.locked` is the manual/CLI state lock only. The one thing that sets it
is `POST /workspaces/{id}/actions/lock`; runs do not acquire it, and have not
since run serialization moved into the dispatcher. So nothing in the run
lifecycle has a lock of its own to release.

It nonetheless released one in nine places -- a plan-only run reaching
`planned`, and every applied, errored, discarded and canceled run -- clearing
whatever lock it found. An operator's maintenance lock was undone by the next
scheduled drift check, after which apply-capable runs were no longer blocked.

Pinned at the source, because the shape that reintroduces it is a two-line
"tidy up the lock" block that any future lifecycle change could add back.
"""

import pathlib
import re

SERVICES = pathlib.Path(__file__).resolve().parents[2] / "terrapod"

#: Modules that drive a run through its lifecycle. None of them may clear the
#: workspace lock.
RUN_LIFECYCLE = [
    SERVICES / "services" / "run_service.py",
    SERVICES / "services" / "run_reconciler.py",
    SERVICES / "api" / "routers" / "runs.py",
]

#: The places that ARE allowed to release it: the unlock and force-unlock
#: endpoints, the explicit PR-comment unlock command, and a local `pulumi up`
#: releasing the lock it took itself. None is a side effect of a run finishing.
DELIBERATE_UNLOCKS = [
    SERVICES / "api" / "routers" / "tfe_v2.py",
    SERVICES / "services" / "vcs_command_dispatcher.py",
    SERVICES / "services" / "pulumi_update_locks.py",
]

#: The only places that may acquire the lock. Both are a CLI or a person asking
#: for it: the lock endpoint (the terraform/tofu state lock and the UI padlock),
#: and a local `pulumi up`, which locks under its own `pulumi-update:<id>` lock
#: ID and releases only that ID.
LOCK_ACQUIRERS = ("api/routers/tfe_v2.py", "services/pulumi_update_locks.py")

_RELEASE = re.compile(r"\b\w*\.locked\s*=\s*False\b")


def test_no_run_lifecycle_module_releases_the_workspace_lock():
    offenders = [
        f"{path.relative_to(SERVICES.parent)}:{n}  {line.strip()}"
        for path in RUN_LIFECYCLE
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if _RELEASE.search(line)
    ]
    assert not offenders, (
        "A run-lifecycle module clears `workspace.locked`. Runs never acquire "
        "that lock -- it is the manual/CLI state lock -- so releasing it here "
        "undoes an operator's lock whenever a run finishes (#1705):\n  " + "\n  ".join(offenders)
    )


def test_the_deliberate_unlock_paths_still_exist():
    """Guards the guard: if these moved, the allowlist above is stale."""
    for path in DELIBERATE_UNLOCKS:
        assert _RELEASE.search(path.read_text()), (
            f"{path.name} no longer releases the lock; update DELIBERATE_UNLOCKS"
        )


def test_nothing_but_the_lock_endpoint_acquires_it():
    """The premise the fix rests on: only a CLI or a person takes the lock."""
    setters = [
        f"{path.relative_to(SERVICES.parent)}:{n}"
        for path in SERVICES.rglob("*.py")
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"\b\w*\.locked\s*=\s*True\b", line)
    ]
    assert setters and all(any(a in s for a in LOCK_ACQUIRERS) for s in setters), (
        "Something other than the lock endpoint or a local pulumi update now sets "
        "`workspace.locked`. If a "
        "run acquires the lock again, it must release its OWN lock -- compare "
        "lock_id -- rather than whatever lock it finds (#1705). Setters: " + ", ".join(setters)
    )
