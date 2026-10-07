"""Phase: obtain `ansible-core` for this Job (#2010).

Ansible is the one platform tool that is **not** a release asset. `opa`,
`trivy`, `checkov`, `pulumi`, `node`, `go` and `dotnet` all publish a
per-platform archive with a known member and a checksum to verify it against,
which is what `platform_tool.py` and `services/platform_tools.py`' spec table
describe. `ansible-core` publishes no such thing: it is a PyPI package. So this
installs it into a virtualenv instead, and deliberately does not appear in
`PLATFORM_TOOLS`.

What it *does* share with those seven is the thing that matters: **an
operator-set version**, so an upstream fix is one `helm upgrade` away rather
than a Terrapod release.

**But it follows the engine-version pattern, not the platform-tool one.**
`registry.platform_tools` is platform-scoped by design and carries no
per-workspace field, whereas a fleet does not move to a new ansible in one
step: one workspace's playbooks are ready before another's. So the deployment
default is `api.config.default_ansible_version` and a workspace overrides it in
`Workspace.ansible_version` -- exactly `default_terraform_version` and
`Workspace.engine_version`. The version therefore arrives here as an argument,
resolved per run from the workspace, rather than being read from config by this
module.

**Through Terrapod's own PyPI proxy, never upstream.** The runner reaches
`/package-cache/pypi/simple` with the run's own token, which is what lets a
sealed deployment work at all -- the proxy is the only thing on that side with
upstream reach, and a runner that fetched from PyPI directly would break every
air-gapped install. All of the pip plumbing this needs already exists for
Pulumi's Python programs and is reused rather than re-derived: `pip_index_url`
keeps the token out of the URL (the runner streams its logs and pip prints its
index), `write_netrc` puts it in `$HOME/.netrc` at 0600, and `pip_env` carries
the four settings a read-only root filesystem and a sealed index require --
including `PIP_TRUSTED_HOST`, without which pip silently **ignores** an
http index and the install dies with "No matching distribution found" for a
package the proxy was serving perfectly well.

**`--only-binary=:all:` is a security choice, not a speed one.** It refuses to
build from source, so pip never executes a package's `setup.py`. Measured:
ansible-core and its eight dependencies -- `resolvelib PyYAML pycparser
packaging MarkupSafe jinja2 cffi cryptography` -- all publish wheels, so nothing
is given up. It is the same boundary that closed #1970: ansible resolving an
inventory must not become a route to running arbitrary code we did not choose.

**A virtualenv rather than the ambient interpreter**, for the reason
`pulumi_deps` gives: the root filesystem is read-only so `site-packages` cannot
be written, and pip was removed from the image deliberately because its vendored
bundle is what scanners report. `python -m venv` restores a working pip from the
untouched stdlib `ensurepip`.

**Built per Job, not cached across runs.** A runner Job is ephemeral and its
`/tmp` is an emptyDir, so there is nothing to cache into; the wheels come from
the proxy, which *is* the cache. Exactly the position `pulumi_deps` is in with
`node_modules`, and for the same reason -- preview and update are different pods.

**Fails closed.** A configure that cannot get ansible must not proceed: there is
no weaker answer than not running the playbook.

**The runner is the only place ansible is installed.** The API deliberately does
not install it, which is why there is no API-side sibling to this module. It
could not fetch through the pull-through cache without either authenticating to
its own HTTP surface with a credential it had minted for itself, or
reimplementing pip's resolver in-process -- and fetching upstream instead is not
an option, because every fetch has to go through the cache or an air-gapped
deployment cannot work. So an inventory preview is served from the last
`InventoryVersion` a configure wrote, refreshed by a resolve operation that runs
here like anything else (#1967).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import structlog

from terrapod.runner import exec_subprocess
from terrapod.runner.phases.pulumi_deps import pip_env, write_netrc

# `structlog` directly rather than `terrapod.logging_config`: the runner image
# ships only the modules Dockerfile.runner names and that one is not among them.
# Matches every sibling phase.
logger = structlog.get_logger("runner.phase.ansible_env")

#: Where the virtualenv goes. Under /tmp because that is an emptyDir and the
#: root filesystem is read-only; ~47MB measured for ansible-core plus its
#: dependency closure.
VENV_DIR = Path("/tmp/ansible-venv")

#: Minutes, not seconds. This is a pip install of nine wheels over an in-cluster
#: hop to a warm proxy, but a cold proxy has to reach PyPI first.
INSTALL_TIMEOUT_SECONDS = 600


class AnsibleUnavailable(RuntimeError):
    """ansible-core could not be obtained, so this Job cannot continue.

    Raised rather than warned. A configure with no ansible has no weaker thing
    it could do instead -- see the module docstring.
    """


def bin_dir(venv: Path | None = None) -> Path:
    """The venv's `bin`, where `ansible-playbook` and friends land."""
    return (venv or VENV_DIR) / "bin"


def is_installed(version: str, venv: Path | None = None) -> bool:
    """Whether this exact version is already present in the venv.

    Checked by asking the installed `ansible` for its version rather than by
    the directory existing: a half-finished install leaves the directory there,
    and a Job that reused it would run an ansible nobody chose.
    """
    marker = (venv or VENV_DIR) / ".terrapod-ansible-version"
    try:
        return marker.read_text(encoding="utf-8").strip() == version
    except OSError:
        return False


def ensure(
    cfg,  # RunnerConfig
    *,
    version: str,
    child_grace: float = 60.0,
    log_file: str | None = None,
    venv: Path | None = None,
) -> Path:
    """Install `ansible-core==version` into a virtualenv; return its `bin`.

    Idempotent: a second call with the same version is a no-op, so a phase may
    call this without tracking whether an earlier one did.
    """
    target = venv or VENV_DIR

    if is_installed(version, target):
        logger.info("ansible already present", version=version, venv=str(target))
        return bin_dir(target)

    if not version:
        raise AnsibleUnavailable(
            "no ansible-core version was resolved for this run: the workspace sets "
            "none and api.config.default_ansible_version is empty, so there is "
            "nothing to install."
        )

    logger.info("creating virtualenv for ansible", venv=str(target), version=version)
    made = exec_subprocess.run(
        [sys.executable, "-m", "venv", str(target)],
        log_file=log_file,
        child_grace_seconds=child_grace,
        tee_to_stdout=True,
    )
    if made.exit_code != 0:
        raise AnsibleUnavailable(
            f"could not create a virtualenv for ansible-core (exit {made.exit_code}). "
            f"The log above is python's own output."
        )

    # Both already exist for Pulumi's Python programs; see the module docstring
    # for why the token is in a netrc rather than in the index URL.
    write_netrc(cfg.api_url, cfg.auth_token)
    os.environ.update(pip_env(cfg.api_url))

    logger.info("installing ansible-core", version=version)
    result = exec_subprocess.run(
        [
            str(bin_dir(target) / "python"),
            "-m",
            "pip",
            "install",
            # Never build from source: that would execute a package's setup.py.
            "--only-binary=:all:",
            f"ansible-core=={version}",
        ],
        log_file=log_file,
        child_grace_seconds=child_grace,
        tee_to_stdout=True,
    )
    if result.exit_code != 0:
        raise AnsibleUnavailable(
            f"installing ansible-core=={version} failed (exit {result.exit_code}). The log "
            f"above is pip's own output. A sealed deployment serves this from its own "
            f"PyPI pull-through cache, so a 'No matching distribution found' here usually "
            f"means the proxy has never seen this version rather than that it does not exist."
        )

    # Written last, so a half-finished install is not mistaken for a complete
    # one by `is_installed` above.
    try:
        (target / ".terrapod-ansible-version").write_text(version, encoding="utf-8")
    except OSError as exc:
        raise AnsibleUnavailable(
            f"ansible-core=={version} installed but the version marker could not be "
            f"written to {target}: {exc}"
        ) from exc

    logger.info("ansible-core ready", version=version, bin=str(bin_dir(target)))
    return bin_dir(target)
