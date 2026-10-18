"""Install the content a configure needs: collections, and roles (#1931).

`ansible-core` ships neither, so a playbook beyond the builtins depends on
content fetched at run time. **Collections and roles are two distinct
mechanisms**, and each is optimised for the case it is usually used for while
both remain available either way:

|             | in-house                                   | external                     |
|-------------|--------------------------------------------|------------------------------|
| collections | published to Terrapod's own registry       | pull-through Galaxy proxy    |
| roles       | git clone, with the run's git credentials  | the Galaxy roles API         |

* **Collections go through Terrapod** — its own published ones answered from the
  registry, upstream ones proxied — so a runner in a restricted network needs no
  route to galaxy.ansible.com. That is the optimised path and the common case.
* **Roles are usually in-house and come from git**, which is already solved: an
  operator declares the credential once as a `git_http_auth` / `git_ssh_auth`
  workspace variable and `runner/phases/git_auth.py` materialises it into git's
  own config, with `insteadOf` rewrites, *before* anything fetches. A role whose
  requirement names a git source is an ordinary clone and inherits all of that,
  needing nothing from this phase.
* **A Galaxy-HOSTED role is the one corner not served.** `ansible-galaxy role
  install ns.name` uses the v1 roles API, which Terrapod does not implement
  (`docs/galaxy-cli-surface.md`), so such a role reaches upstream directly where
  there is a route out and fails where there is not. Use a git source, or vendor
  the role into a collection, until that surface exists.

**Two subcommands, not one.** `ansible-galaxy install -r` reads both sections,
but it does **not** accept `--no-cache`, which the air-gap gate records as
load-bearing (#1637): the client writes a placeholder cache entry before each
request and fills it in only on success, so a retry after a transient failure
finds the placeholder and dies with "Missing expected 'results' in
ansible-galaxy cache" instead of asking Terrapod again. So the collection half
runs as `collection install --no-cache` and the role half as `role install`.
Measured: each subcommand ignores the other's section, and against a file
carrying only the other's it prints "Skipping install, no requirements found"
and exits 0 — so both can run unconditionally.

**Configured by environment, never by a generated `ansible.cfg`.** `ansible.cfg`
is single-file with no merging, so writing one discards the operator's own
(#2035). Measured against ansible-core 2.21.5: `ANSIBLE_GALAXY_SERVER_LIST` plus
`ANSIBLE_GALAXY_SERVER_<NAME>_URL` / `_TOKEN` configure a server entirely by env,
and env beats `ansible.cfg` while leaving the rest of theirs standing.

**The galaxy server settings are removed before ours are set, and nothing else
is.** An inherited `ANSIBLE_GALAXY_SERVER_LIST` would redirect where a run's
collections come from, which is a supply-chain decision and not the ambient
environment's to make, so those keys are replaced rather than merged. The
inventory resolver strips the whole `ANSIBLE_*` prefix, and that is right
*there* -- it runs in the API pod, where no operator-supplied environment
exists. Doing the same here would be wrong: on a runner, `ANSIBLE_*` is where a
workspace's own environment variables arrive, so blanket-stripping would
silently discard settings the operator deliberately set.

**The install child holds the run's token; `ansible-playbook` must not.** The
credential is scoped to the galaxy server and exists so the registry can be
read. A collection is third-party code, and once it is on disk the playbook that
runs it has no business also holding the token that writes this run's state --
the same boundary `exec_subprocess` draws by scrubbing `TP_AUTH_TOKEN` from an
engine's environment. So this environment belongs to the install step alone and
must not be reused for the run that follows it.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

from terrapod.runner import exec_subprocess

# `structlog` directly rather than `terrapod.logging_config`: the runner image
# ships only the modules Dockerfile.runner names and that one is not among them.
# Matches every sibling phase.
logger = structlog.get_logger("runner.phase.ansible_collections")

#: The DEPRECATED alias, deliberately, and the same one `pulumi_deps` uses.
#: `/api/v1` is canonical, but this is a runner calling back to an API it may lag
#: or lead by up to N-2 minors: the alias is served by every version in that
#: window, where the canonical path is the newer name. A runner picks the prefix
#: that cannot be missing.
_API_PREFIX = "/api/terrapod/v1"

#: The name of the `galaxy_server` entry the run is pointed at. Only used to
#: build the per-server env var names, which ansible uppercases.
SERVER_NAME = "terrapod"

#: Where collections are installed to, and where ansible is told to look. Both,
#: because `-p` outside the configured collections path installs successfully and
#: then warns that nothing will pick them up.
COLLECTIONS_DIR = Path("/tmp/ansible-collections")

#: Where roles land. Separate from collections because ansible keeps the two
#: search paths separate, and a role installed into the collections tree is not
#: found.
ROLES_DIR = Path("/tmp/ansible-roles")

#: The paths a requirements file is looked for at, in order, relative to the
#: workspace's `config_directory`. Ansible's own convention: `ansible-galaxy`
#: documents `requirements.yml`, and the directory form is what a repository
#: laid out with `collections/` uses.
REQUIREMENTS_PATHS = ("collections/requirements.yml", "requirements.yml")


class CollectionInstallError(RuntimeError):
    """The run's roles or collections could not be installed.

    Carries the child's exit code so the orchestrator returns it unchanged, as
    `DependencyError` does for a Pulumi program's dependencies.
    """

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def galaxy_server_url(api_url: str) -> str:
    """Terrapod's Galaxy v3 surface, with no credential in it.

    The trailing slash matters: `ansible-galaxy` requests this URL as given for
    version discovery, and only falls back to appending `/api/` when that call
    fails. Terrapod serves discovery at this path, so the first call is the one
    that answers — but note that when something IS wrong, the client reports the
    fallback URL rather than this one, which makes its error name a path Terrapod
    never served.
    """
    return f"{api_url.rstrip('/')}{_API_PREFIX}/package-cache/galaxy/"


def requirements_files(config_dir: Path) -> list[Path]:
    """The requirements file this run installs from, if the configuration has one.

    **The single producer of "what the configuration declares", deliberately**,
    so a caller asks this rather than reaching for a path of its own. The
    configuration is one of two sources: the other is the workspace's own
    `ansible_collections`, which `install` applies over the top of this one.

    Deriving the set from the playbooks themselves — scanning for FQCN module
    names rather than reading a declared file — was considered and dropped: a
    declaration an operator can see and govern beats one inferred from code,
    and the workspace list is where that declaration lives.
    """
    return [p for rel in REQUIREMENTS_PATHS if (p := config_dir / rel).is_file()]


def galaxy_env(api_url: str, token: str, *, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment `ansible-galaxy` installs under.

    A named function rather than a literal at the call site so the tests that
    drive real `ansible-galaxy` run under exactly this, rather than rebuilding
    their own and verifying a configuration production does not use.
    """
    source = dict(os.environ if base is None else base)

    # Replaced, not merged -- see the module docstring. Scoped to the galaxy
    # server keys, because every other `ANSIBLE_*` on a runner may be the
    # operator's own, delivered as a workspace environment variable.
    inherited = {k: v for k, v in source.items() if not k.startswith("ANSIBLE_GALAXY_SERVER")}
    upper = SERVER_NAME.upper()

    # Ours FIRST, theirs kept after it: a repository that vendors collections
    # sets this itself, and discarding it would stop ansible finding them. The
    # install target has to be on the path too, or `-p` installs successfully
    # into somewhere nothing reads.
    search_path = str(COLLECTIONS_DIR)
    if existing := source.get("ANSIBLE_COLLECTIONS_PATH", "").strip():
        search_path = f"{search_path}:{existing}"

    # Same rule for roles: a repository that vendors roles under its own
    # `roles/` directory sets this, and ours has to be on the path as well.
    roles_path = str(ROLES_DIR)
    if existing_roles := source.get("ANSIBLE_ROLES_PATH", "").strip():
        roles_path = f"{roles_path}:{existing_roles}"

    env = {
        **inherited,
        "ANSIBLE_GALAXY_SERVER_LIST": SERVER_NAME,
        f"ANSIBLE_GALAXY_SERVER_{upper}_URL": galaxy_server_url(api_url),
        f"ANSIBLE_GALAXY_SERVER_{upper}_TOKEN": token,
        "ANSIBLE_COLLECTIONS_PATH": search_path,
        "ANSIBLE_ROLES_PATH": roles_path,
    }
    # The root filesystem is read-only, so ansible needs somewhere to write --
    # but only if the operator has not already said where.
    env.setdefault("ANSIBLE_HOME", "/tmp/ansible-home")
    return env


def _run_install(
    argv_tail: list[str],
    *,
    bin_dir: Path,
    env: dict[str, str],
    child_grace: float,
    log_file: str | None,
    what: str,
) -> None:
    """One `ansible-galaxy` invocation. `argv_tail` carries the subcommand, so a
    caller chooses `role install` or `collection install` -- the two have
    different flags and must not be built from one template."""
    result = exec_subprocess.run(
        [str(bin_dir / "ansible-galaxy"), *argv_tail],
        log_file=log_file,
        child_grace_seconds=child_grace,
        tee_to_stdout=True,
        env=env,
    )
    if result.exit_code != 0:
        raise CollectionInstallError(
            f"installing {what} failed; the log above is ansible-galaxy's own output",
            exit_code=result.exit_code,
        )


def requirement_arguments(declared: list[str]) -> list[str]:
    """The workspace's declared collections as `ansible-galaxy` arguments.

    Already in `name[:version]` form when they arrive: the API flattens the
    stored `{"name", "version"}` entries, so the runner carries no opinion about
    the shape and needs no YAML parser to read them.
    """
    return [d for d in declared if d]


def install(
    cfg,  # RunnerConfig
    config_dir: Path,
    *,
    bin_dir: Path,
    child_grace: float = 60.0,
    log_file: str | None = None,
) -> None:
    """Install the configuration's roles and collections, then the workspace's.

    Three invocations, and the order of the last two is the precedence: whatever
    the configuration declares is installed first, then the workspace's own
    collections with `--force`, so a workspace entry replaces the repository's
    build of the same collection and every other one stands. That is "the
    platform is the later word", the relationship a workspace variable has to a
    default committed in the configuration.

    **Order and `--force` are what express the override, measured rather than
    assumed** (ansible-core 2.21.5). A second install *without* `--force`
    reports "already installed" and does not upgrade, so the first source
    silently wins; *with* `--force` it overwrites that collection and leaves the
    rest alone. The alternative -- merging both sources into one document so
    ansible resolves them jointly -- cannot express an override at all: handed
    two constraints for one collection the resolver refuses rather than
    preferring either, and it would need a YAML parser the runner image
    deliberately does not carry. The cost of overriding is that the two sets are
    not dependency-resolved together, and a workspace pin that breaks a
    repository collection's own requirement is the operator's declared intent.

    A no-op when nothing is declared anywhere -- the common case for a playbook
    using only `ansible.builtin`, and not an error.
    """
    files = requirements_files(config_dir)
    declared = requirement_arguments(getattr(cfg, "ansible_collections", []) or [])

    if not files and not declared:
        logger.info(
            "no roles or collections declared; nothing to install",
            config_directory=str(config_dir),
            looked_for=list(REQUIREMENTS_PATHS),
        )
        return

    if len(files) > 1:
        raise CollectionInstallError(
            "this configuration declares requirements in more than one place — "
            + ", ".join(str(f.relative_to(config_dir)) for f in files)
            + ". Terrapod installs from one requirements file, because handing ansible "
            "several lets the order they are read in decide a dependency version "
            "silently. Keep one of them."
        )

    env = galaxy_env(cfg.api_url, cfg.auth_token)
    COLLECTIONS_DIR.mkdir(parents=True, exist_ok=True)
    ROLES_DIR.mkdir(parents=True, exist_ok=True)

    if files:
        path = files[0]
        # Roles first, and unconditionally: a file with no `roles:` section
        # prints "Skipping install, no requirements found" and exits 0, so this
        # costs one process rather than a YAML parser the runner does not have.
        # A git-sourced role clones with the credentials `git_auth` has already
        # written into git's own config.
        logger.info("installing the configuration's roles", requirements=str(path))
        _run_install(
            ["role", "install", "-r", str(path), "-p", str(ROLES_DIR)],
            bin_dir=bin_dir,
            env=env,
            child_grace=child_grace,
            log_file=log_file,
            what=f"the roles declared in {path}",
        )
        logger.info("installing the configuration's collections", requirements=str(path))
        _run_install(
            ["collection", "install", "-r", str(path), "-p", str(COLLECTIONS_DIR), "--no-cache"],
            bin_dir=bin_dir,
            env=env,
            child_grace=child_grace,
            log_file=log_file,
            what=f"the collections declared in {path}",
        )

    if declared:
        # Said out loud: an operator reading the log must be able to see that a
        # version they did not commit came from the workspace, and which ones.
        logger.info(
            "installing the workspace's declared collections over the configuration's",
            collections=declared,
        )
        _run_install(
            [
                "collection",
                "install",
                *declared,
                "-p",
                str(COLLECTIONS_DIR),
                "--force",
                "--no-cache",
            ],
            bin_dir=bin_dir,
            env=env,
            child_grace=child_grace,
            log_file=log_file,
            what="the collections declared on the workspace",
        )
