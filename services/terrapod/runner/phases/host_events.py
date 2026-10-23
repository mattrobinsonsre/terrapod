"""Deliver our callback plugins to the EE, and collect what they wrote (#1928, #2144).

Two plugins, one directory, because they are one concern and ansible permits
exactly that combination: `terrapod_events` is a NOTIFICATION callback (many
allowed) and `terrapod_stdout` is the STDOUT one (one allowed, and ours
replaces the operator's). The second is #2144's and tags every line ansible
prints with its host; it needs no addition to `ANSIBLE_CALLBACK_PLUGINS`,
because that is a search PATH and both files sit on it.

Ansible's unit of outcome is the host, not the resource, so a configure has to
report per host. The capture itself is a notification callback plugin
(`runner/ansible_callback/terrapod_events.py`) that runs inside the execution
environment; this phase is the three things around it that belong to the
orchestrator -- putting the plugin where the EE can load it, telling ansible to
load it without disturbing the operator's own, and uploading what it wrote.

**Why a file on the shared volume rather than the plugin POSTing results.** The
callback runs beside the operator's playbook, which is third-party code by
design. `exec_subprocess.run` scrubs `TP_AUTH_TOKEN` from the engine's
environment for exactly that reason -- "a provider is third-party code running
against the operator's credentials; it has no business also holding the token
that writes this run's state" -- and the EE agent scrubs its own token before
handing the environment over. A plugin that posted its own events would need the
run's token inside the EE and would undo both. So the events land on `/tp`, which
both containers already share, and the orchestrator uploads them with the
credential it already holds.

**Why the plugin is DELIVERED rather than baked into the EE.** An EE may be the
operator's own image (#2154 D-whatever: an operator-supplied EE is trusted code
but it is theirs), so nothing of ours can be assumed present in it. The same
reasoning that puts the agent binary on `/tp` puts the plugin there.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil

import structlog

from terrapod.runner import ee_client
from terrapod.runner.runner_config import RunnerConfig

log = structlog.get_logger(__name__)

#: Set by the Job; the default matches `job_template._EE_CALLBACK_DIR` so a
#: runner-tier test needs no Job spec.
_DIR_ENV = "TP_EE_CALLBACK_DIR"
_DEFAULT_DIR = "/tp/callback"

#: The env var the plugin reads for where to write. Named here rather than
#: imported from the plugin, which imports `ansible` and so cannot be imported
#: by the orchestrator at all.
_PATH_ENV = "TP_HOST_EVENTS_PATH"

#: The plugin asset in the runner image. `Dockerfile.runner` copies it to a
#: destination OUTSIDE `terrapod/`, because it is data we ship rather than a
#: module of ours -- it imports `ansible`, which the orchestrator does not have,
#: and the runner-image import gates are keyed on that destination.
_ASSET_CANDIDATES = (
    "/opt/terrapod/ansible-callback",
    # The source tree, for a test or a local run.
    str(pathlib.Path(__file__).resolve().parents[1] / "ansible_callback"),
)

#: Both plugins we ship, delivered together because they are one concern: our
#: callback plugins, in the one directory the EE loads from. They are different
#: TYPES -- `terrapod_events` is a notification callback and `terrapod_stdout`
#: is the stdout one (#2144) -- which is exactly why they coexist: ansible
#: permits many of the former and one of the latter.
_EVENTS_PLUGIN = "terrapod_events.py"
_STDOUT_PLUGIN = "terrapod_stdout.py"
_PLUGIN_FILENAMES = (_EVENTS_PLUGIN, _STDOUT_PLUGIN)

#: The stdout plugin's `CALLBACK_NAME`. `ANSIBLE_STDOUT_CALLBACK` takes a NAME,
#: not a path -- the path comes from `ANSIBLE_CALLBACK_PLUGINS`.
_STDOUT_CALLBACK_NAME = "terrapod_stdout"

#: What ansible reports as the effective callback search path. Parsed rather than
#: assumed: an operator's `callback_plugins` REPLACES the default, so the default
#: cannot be used as a stand-in for theirs.
_PATH_LINE = re.compile(r"^DEFAULT_CALLBACK_PLUGIN_PATH\((?P<source>[^)]*)\) = \[(?P<value>.*)\]$")

#: Ansible's own default, used only when the dump cannot be read. Prepending to
#: this still finds an operator's `~/.ansible/plugins/callback` plugins, so the
#: common case survives a failed dump; a CUSTOM path would not, which is why the
#: fallback is logged as a warning rather than taken quietly.
_DEFAULT_SEARCH_PATH = ("~/.ansible/plugins/callback", "/usr/share/ansible/plugins/callback")


def callback_dir() -> pathlib.Path:
    """Where the plugin and its output live, on the volume the EE shares."""
    return pathlib.Path(os.environ.get(_DIR_ENV) or _DEFAULT_DIR)


def events_path() -> pathlib.Path:
    """The event stream the plugin appends to."""
    return callback_dir() / "events.jsonl"


def large_path() -> pathlib.Path:
    """The sidecar holding payloads too big to keep inline.

    One per run rather than one per payload: the retention sweep deletes a fixed
    key list per run and has no prefix enumeration, so an object per oversized
    payload would leak for ever.
    """
    return callback_dir() / "events-large.jsonl"


def _asset(filename: str) -> pathlib.Path | None:
    for candidate in _ASSET_CANDIDATES:
        path = pathlib.Path(candidate) / filename
        if path.is_file():
            return path
    return None


def deliver() -> pathlib.Path | None:
    """Copy the plugin where the EE can load it; return its directory.

    Returns None when the asset is missing, which is a shipping defect rather
    than an operator's problem -- so it is logged and the configure proceeds
    WITHOUT capture. The alternative, refusing to run, would mean a packaging
    mistake in our own image stopping an operator's fleet from being configured,
    which trades a reporting gap for an outage.
    """
    assets = {name: _asset(name) for name in _PLUGIN_FILENAMES}
    if assets[_EVENTS_PLUGIN] is None:
        log.warning(
            "the host-event callback plugin is not in this image — a configure will "
            "run but will report no per-host results",
            looked_in=list(_ASSET_CANDIDATES),
        )
        return None
    target_dir = callback_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, asset in assets.items():
            if asset is None:
                # Each plugin degrades on its own: a missing stdout plugin costs
                # the per-host VIEW (#2144) and not the per-host RESULTS, so it
                # must not take the events capture down with it.
                log.warning(
                    "a callback plugin is not in this image — the configure will run "
                    "without what it provides",
                    plugin=name,
                    looked_in=list(_ASSET_CANDIDATES),
                )
                continue
            target = target_dir / name
            shutil.copyfile(asset, target)
            # Owner-only: the EE runs as the SAME pinned uid as this process
            # (`_ee_security_context`, #1928), so it reads this as the owner and
            # the group bit would grant more than the contract needs.
            target.chmod(0o600)
    except OSError as exc:
        log.warning(
            "could not place the host-event callback plugin — a configure will run "
            "but will report no per-host results",
            dir=str(target_dir),
            error=str(exc),
        )
        return None
    return target_dir


def _their_search_path(cfg: RunnerConfig, *, cwd: str) -> tuple[str, ...]:
    """Ansible's EFFECTIVE callback path, asked of ansible in the EE.

    Measured, and the reason this round trip exists: an operator's
    `callback_plugins` replaces the default search path entirely, so setting
    `ANSIBLE_CALLBACK_PLUGINS` to our directory alone leaves every notification
    callback they run unable to load -- silently, with their `callbacks_enabled`
    still naming it. Reading their value and prepending ours is the only form
    that captures AND leaves their configuration working.

    `ansible-config` is on the EE agent's allowlist, so this needs no new
    capability there.
    """
    try:
        result = ee_client.run(
            "ansible-config", ["dump"], cwd=cwd, env=dict(env_for_dump()), capture=True
        )
    except ee_client.ExecutionEnvironmentUnavailable as exc:
        log.warning(
            "could not read the execution environment's effective callback path; "
            "falling back to ansible's default, so a CUSTOM callback_plugins in the "
            "operator's ansible.cfg may not load for this run",
            error=str(exc),
        )
        return _DEFAULT_SEARCH_PATH
    if result.rc != 0 or not result.output:
        log.warning(
            "`ansible-config dump` did not answer; falling back to ansible's default "
            "callback path, so a CUSTOM callback_plugins may not load for this run",
            rc=result.rc,
        )
        return _DEFAULT_SEARCH_PATH
    for line in result.output.decode("utf-8", "replace").splitlines():
        match = _PATH_LINE.match(line.strip())
        if not match:
            continue
        entries = [e.strip().strip("'\"") for e in match.group("value").split(",")]
        found = tuple(e for e in entries if e)
        if found:
            log.info(
                "read the effective callback path from the execution environment",
                source=match.group("source"),
                entries=len(found),
            )
            return found
    log.warning(
        "`ansible-config dump` answered without a callback path; falling back to ansible's default"
    )
    return _DEFAULT_SEARCH_PATH


def env_for_dump() -> dict[str, str]:
    """The environment the configuration dump runs under.

    Deliberately empty of our forced settings: the question is what the
    OPERATOR's configuration says, so forcing anything would change the answer
    we are reading.
    """
    return {}


def callback_env(cfg: RunnerConfig, *, cwd: str, plugin_dir: pathlib.Path) -> dict[str, str]:
    """`ANSIBLE_CALLBACK_PLUGINS` with ours first, plus where to write.

    **`ANSIBLE_CALLBACKS_ENABLED` is deliberately absent.** Writing it would
    replace the operator's list and disable their own notification callbacks;
    the plugin carries `CALLBACK_NEEDS_ENABLED = False` so being on the path is
    enough. Measured both ways on ansible-core 2.18.3.
    """
    search = (
        str(plugin_dir),
        *(p for p in _their_search_path(cfg, cwd=cwd) if p != str(plugin_dir)),
    )
    env = {
        "ANSIBLE_CALLBACK_PLUGINS": ":".join(search),
        _PATH_ENV: str(events_path()),
    }
    # **`ANSIBLE_STDOUT_CALLBACK` REPLACES the operator's renderer, deliberately
    # (user, 2026-10-23):** *"We have to control the output format because it's
    # our responsibility to make it visible to the UI and api."* There is exactly
    # one stdout slot, so unlike `ANSIBLE_CALLBACK_PLUGINS` above there is no
    # "ours AND theirs" form to reach for -- the platform owns the log's shape
    # for the same reason it owns redaction. Note what is NOT replaced: the
    # operator's `ansible.cfg`, which `configure.py`'s promise is about and which
    # we still never write.
    #
    # Set only when the plugin actually landed, so a packaging defect degrades to
    # ansible's own renderer rather than naming a callback the EE cannot find.
    if (plugin_dir / _STDOUT_PLUGIN).is_file():
        env["ANSIBLE_STDOUT_CALLBACK"] = _STDOUT_CALLBACK_NAME
    return env
