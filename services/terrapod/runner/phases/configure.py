"""Run a playbook: the configure phase (#1972).

A configure is an independent operation rather than a phase bolted onto a
plan/apply -- but it executes in a runner Job, through the listener, with the
same credential delivery and log streaming as everything else. Ansible itself
runs in the execution-environment container beside this one; `ee_client` is the
only route to it.

Three decisions are settled elsewhere and implemented here, so the reasoning
lives with its callers rather than being re-derived:

* **The working directory is `config_directory`** (#1929, settled 2026-10-12).
  Two measured reasons: `group_vars`/`host_vars` at the config root are found
  **cwd-relative** and nowhere else when they sit beside rather than inside the
  inventory directory; and relative paths in the operator's own `ansible.cfg` --
  `inventory`, `roles_path`, `library` -- resolve against the working directory
  rather than against the config file. Run from anywhere else and a repository
  whose `ansible.cfg` was written for its own root silently resolves half of them
  wrong.

* **Their `ansible.cfg` is never replaced.** `ansible.cfg` is single-file with no
  merging, so writing one and pointing `ANSIBLE_CONFIG` at it discards the
  operator's entire configuration -- `roles_path`, `library`, `forks`, `become`,
  `remote_user`, `gathering`, `strategy`, and `inventory` itself. Our
  non-negotiables go through the ENVIRONMENT instead, which beats the file, so
  this is a stronger control than a generated config while costing them nothing
  else: an operator cannot re-enable a disabled plugin from their repository.

* **There is no `--check` dry run** (user, 2026-10-14). Check mode is unreliable
  per module, is not a diff, and misleads on anything gated by `when: changed`.
  A configure runs.

**Exit codes are ansible's, and `4` is overloaded**, which is why this phase
reports the code and the stream rather than interpreting them: `rc=4` is both
"your hosts are unreachable" and "your playbook is malformed", distinguished by
the stream and the presence of a PLAY RECAP rather than by the status. The
terminal mapping is #1973's.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

from terrapod.runner import ee_client
from terrapod.runner.runner_config import RunnerConfig

log = structlog.get_logger(__name__)

#: Forced on the process ansible runs in, never written into a config file.
#:
#: `ANSIBLE_INVENTORY_ENABLED` without `auto` and without `script` is what keeps
#: a repository from executing an inventory script or dispatching to a cloud
#: plugin (#1970) -- the exec bit is the whole decision, and `auto` reads a
#: file's own `plugin:` key and runs whatever it names. `ANY_UNPARSED_IS_FAILED`
#: is #1967's fail-closed property: a source that cannot be read must not
#: silently resolve to a partial host set, because a configure that reaches half
#: a fleet and reports success is worse than one that refuses.
#:
#: Both are measured to beat the operator's `ansible.cfg` while leaving the rest
#: of their file in force.
_FORCED_ENV = {
    "ANSIBLE_INVENTORY_ENABLED": "yaml,ini",
    "ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED": "True",
    # Colour is terminal control bytes in a log nobody views in a terminal.
    "ANSIBLE_FORCE_COLOR": "0",
    # A retry file is written next to the playbook, i.e. into the operator's
    # fetched tree, and nothing ever reads it back here.
    "ANSIBLE_RETRY_FILES_ENABLED": "False",
}


class ConfigureNotConfigured(RuntimeError):
    """The run asks for a configure the workspace does not describe.

    Raised rather than defaulted, because every fallback available here is wrong:
    guessing a playbook runs something nobody asked for, and running none at all
    reports success for a configure that did nothing.
    """


def _forced_env(cfg: RunnerConfig) -> dict[str, str]:
    """The environment ansible runs under, with inherited `ANSIBLE_*` stripped.

    Stripping rather than overriding is the point. Env beats `ansible.cfg`, so an
    inherited `ANSIBLE_INVENTORY_ENABLED=auto` would re-enable the plugin
    dispatcher that `_FORCED_ENV` exists to disable -- and the set of ansible
    settings is far larger than the set anyone thinks about, so an allowlist of
    what we set is not the same as a bound on what reaches the process.
    """
    env = dict(_FORCED_ENV)
    # Temp directories must be writable, which a read-only root filesystem makes
    # a live concern: ansible exits `rc 250` with "No usable temporary directory
    # found" rather than naming the cause. The Job supplies them.
    for passthrough in ("TMPDIR", "ANSIBLE_LOCAL_TEMP", "ANSIBLE_REMOTE_TEMP"):
        if value := os.environ.get(passthrough):
            env[passthrough] = value
    if cfg.parallelism > 0:
        # The platform's own per-workspace setting (#1431), which is why it is
        # set rather than left to their `forks`.
        env["ANSIBLE_FORKS"] = str(cfg.parallelism)
    return env


def argv(cfg: RunnerConfig, *, inventory: Path) -> list[str]:
    """`ansible-playbook`'s arguments for this run.

    Built here and not inline so it can be asserted on directly.

    **Exactly ONE `-i`, and it is the run's resolved artifact** -- never the
    workspace's `inventory_paths`. Those name the sources the API merged when it
    resolved this run's inventory at creation (#2021), so passing them here
    would have ansible re-read the repository's own inventory alongside the
    merged result: the same hosts from two sources, and variable precedence
    decided by the order they happened to be listed in rather than by what the
    operator declared. The artifact is the answer; re-deriving it is how the
    runner and the API come to disagree about what a configure targeted.

    `--limit` IS passed, because the artifact is written unlimited -- run
    creation calls `write_run_artifact(db, run)` with no limit, so the narrowing
    is this run's to apply and ansible expands it through the rendered nesting.
    """
    if not cfg.playbook:
        raise ConfigureNotConfigured(
            "this run is a configure but names no playbook, and the workspace has "
            "no `default_playbook` to fall back to"
        )
    out: list[str] = ["-i", str(inventory)]
    if cfg.ansible_limit:
        out += ["--limit", cfg.ansible_limit]
    out.append(cfg.playbook)
    return out


def run(cfg: RunnerConfig, *, work_dir: Path, inventory: Path, log_file: str | None = None) -> int:
    """Execute the playbook in the execution environment; return ansible's code.

    Args:
        cfg: the run's configuration. `config_directory`, `playbook` and
            and the resolved inventory are the configure-specific parts.
        work_dir: the fetched tree's root. `config_directory` is relative to it.
        inventory: the materialised resolved inventory, from
            `configure_inventory.materialise`. Passed in rather than fetched
            here so the download is a separate, separately-failing step: an
            inventory that cannot be read is not a playbook that failed.
        log_file: appended with the combined output for upload as the run log.

    Raises:
        ConfigureNotConfigured: the run names no playbook.
        ee_client.ExecutionEnvironmentUnavailable: no execution environment, or
            it could not be driven. Deliberately not caught here -- a configure
            that could not start must not report an exit code, because every
            caller above reads one as ansible's verdict.
    """
    if not ee_client.configured():
        raise ee_client.ExecutionEnvironmentUnavailable(
            "this run is a configure but the Job has no execution environment "
            "container; a configure cannot run in the orchestrator, which ships "
            "no ansible by design (#2154)"
        )

    cwd = (work_dir / cfg.config_directory).resolve() if cfg.config_directory else work_dir
    if not cwd.is_dir():
        raise ConfigureNotConfigured(
            f"the workspace's Ansible config root {cfg.config_directory!r} is not a "
            f"directory in the fetched tree; nothing to run a playbook from"
        )

    arguments = argv(cfg, inventory=inventory)
    log.info(
        "configure starting",
        playbook=cfg.playbook,
        limit=cfg.ansible_limit or None,
        inventory=str(inventory),
        cwd=str(cwd),
    )
    result = ee_client.run(
        "ansible-playbook",
        arguments,
        cwd=str(cwd),
        env=_forced_env(cfg),
        log_file=log_file,
    )
    if result.error:
        # The agent's own error, not ansible's verdict: the EE could not run the
        # entry point at all. Surfaced as a failure of the environment rather
        # than of the estate, because the operator's response is different.
        raise ee_client.ExecutionEnvironmentUnavailable(
            f"the execution environment could not run ansible-playbook: {result.error}"
        )
    log.info("configure finished", rc=result.rc)
    return result.rc
