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

from terrapod.runner import ee_client

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

#: Fallbacks for the two install targets, used only when the Job did not say.
#: The real values come from the environment -- see `collections_dir`.
_DEFAULT_COLLECTIONS_DIR = "/tp/collections"
_DEFAULT_ROLES_DIR = "/tp/roles"

#: The execution environment's own HOME, which is NOT this process's. From
#: `TP_EE_HOME`, set by the Job beside the other `TP_EE_*` paths, so it stays a
#: property of the Job's volume layout rather than something a phase decides.
#:
#: Two things live here and both have to be where the EE will look: pip's
#: `.netrc`, and ansible's `~/.ansible` (which is why no `ANSIBLE_HOME` is set
#: at all -- see `galaxy_env`).
_DEFAULT_EE_HOME = "/tp/home"

#: The name a collection declares its Python dependencies in. ansible-builder
#: reads the same file, so this is the ecosystem's convention rather than ours.
PYTHON_REQUIREMENTS = "requirements.txt"


def collections_dir() -> Path:
    """Where collections are installed to, and where ansible is told to look.

    Both, because `-p` outside the configured collections path installs
    successfully and then warns that nothing will pick them up.

    **It must be on the volume BOTH containers see.** Ansible runs in the
    execution-environment container and this installs from the orchestrator, so
    the runner's own `emptyDir` is invisible to the thing that reads the result:
    collections would install, report success, and no playbook would find one.
    `job_template` mounts a single shared volume into both and names it in
    `TP_EE_COLLECTIONS_DIR`.

    A function rather than a constant because the value arrives in the Job's
    environment, which a module-level read would freeze at import.
    """
    return Path(os.environ.get("TP_EE_COLLECTIONS_DIR") or _DEFAULT_COLLECTIONS_DIR)


def roles_dir() -> Path:
    """Where roles land, on that same shared volume.

    Separate from collections because ansible keeps the two search paths
    separate, and a role installed into the collections tree is not found.
    """
    return Path(os.environ.get("TP_EE_ROLES_DIR") or _DEFAULT_ROLES_DIR)


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


def ee_home() -> Path:
    """The execution environment's HOME, from the Job."""
    return Path(os.environ.get("TP_EE_HOME") or _DEFAULT_EE_HOME)


def python_requirements_files(config_dir: Path) -> list[Path]:
    """Every `requirements.txt` the install should satisfy, installed-first.

    Two sources, and the order is the precedence pip applies -- later wins a
    conflicting pin, the same relationship `--force` expresses for collections:

    * **each installed collection's own**, under
      `<collections>/ansible_collections/<ns>/<name>/requirements.txt`.
      `ansible-galaxy` installs a collection's CONTENT and not its Python
      dependencies -- `amazon.aws` ships one naming `boto3` and `botocore` -- so
      without this a collection installs cleanly and then fails at the first task
      or inventory-plugin load with an import error naming the SDK.
    * **the operator's own**, beside the `requirements.yml` they already declare,
      for a dependency no collection names: a `uri` task needing a client, or a
      filter plugin of their own. No new column, and the file sits where the
      Galaxy requirements already do.

    Sorted within the collection pass so a fleet installs in a stable order --
    two collections pinning the same package differently is the operator's to
    resolve, and an order that varies per run would make it intermittent.
    """
    files = sorted((collections_dir() / "ansible_collections").glob(f"*/*/{PYTHON_REQUIREMENTS}"))
    declared = config_dir / PYTHON_REQUIREMENTS
    if declared.is_file():
        files.append(declared)
    return files


def pip_env(api_url: str) -> dict[str, str]:
    """pip settings for the EXECUTION ENVIRONMENT's pip.

    Built on `pulumi_deps.pip_env` rather than beside it, because that function
    carries the trap worth having exactly one copy of: **pip silently IGNORES a
    plain-HTTP index unless the host is named in `PIP_TRUSTED_HOST`** -- it does
    not fail, and the install then dies with "No matching distribution found"
    for a package the proxy was serving perfectly well. The runner reaches the
    API on the in-cluster URL, which is http by default.

    One value differs, and it is the same mistake this issue was about: that
    function's `PIP_CACHE_DIR` is `/tmp/pip-cache`, correct for the orchestrator
    whose `/tmp` is a writable emptyDir, and the EE's READ-ONLY ROOT. So the
    cache goes on the shared volume, where the wheels -- the biggest thing pip
    writes -- are also visible to whoever is debugging the run.
    """
    from terrapod.runner.phases import pulumi_deps

    env = dict(pulumi_deps.pip_env(api_url))
    env["PIP_CACHE_DIR"] = str(ee_home() / ".cache" / "pip")
    # `--user` needs no `PYTHONPATH`: the user site is on `sys.path` by default
    # and HOME is on the writable shared volume. Measured in the published image
    # with a read-only root -- site.ENABLE_USER_SITE is True and the install
    # lands in `<HOME>/.local/lib/pythonX.Y/site-packages`.
    env["PIP_USER"] = "1"
    return env


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

    # AN ALLOW-LIST, not a filtered copy of this process's environment, and the
    # difference is two measured failures rather than tidiness (#2305).
    #
    # `ee_client.run`'s env is ADDED to the execution environment's own -- the
    # agent runs `scrubbed(os.Environ(), req.Env, ...)` -- so anything returned
    # here OVERRIDES what the Job set for that container. This function runs in
    # the ORCHESTRATOR, whose filesystem is not the EE's, so forwarding its
    # environment wholesale forwarded paths that do not exist over there.
    # Measured against the published EE image with a read-only root:
    #
    #     HOME=/home/runner   (the orchestrator's, forwarded)  rc=1, ENOENT
    #     HOME=/tp/home       (the EE's own, from the Job)      rc=0
    #
    # A deny-list of the path-shaped names found so far would fix those two and
    # leave the next one to be found the same way, because the set of variables
    # naming a path is unbounded -- which is the argument the module docstring
    # already makes for replacing the galaxy server keys rather than merging
    # them. So: name what the EE needs to learn from here, and let its own
    # environment supply `HOME`, `TMPDIR` and the ansible temp paths, which the
    # Job already sets correctly for it. `configure._forced_env` takes the same
    # position from the other direction, stripping every inherited `ANSIBLE_*`.
    upper = SERVER_NAME.upper()

    # Ours FIRST, theirs kept after it: a repository that vendors collections
    # sets this itself, and discarding it would stop ansible finding them. The
    # install target has to be on the path too, or `-p` installs successfully
    # into somewhere nothing reads.
    search_path = str(collections_dir())
    if existing := source.get("ANSIBLE_COLLECTIONS_PATH", "").strip():
        search_path = f"{search_path}:{existing}"

    # Same rule for roles: a repository that vendors roles under its own
    # `roles/` directory sets this, and ours has to be on the path as well.
    roles_path = str(roles_dir())
    if existing_roles := source.get("ANSIBLE_ROLES_PATH", "").strip():
        roles_path = f"{roles_path}:{existing_roles}"

    # The two search paths are the allow-list's only members: both are read off
    # `source` above so a repository vendoring its own collections or roles keeps
    # working, which is why they are forwarded explicitly rather than dropped
    # with the rest (user's call, #2305).
    #
    # **There is deliberately no `ANSIBLE_HOME`.** It used to default to
    # `/tmp/ansible-home`, which is the ORCHESTRATOR's writable `tmp` emptyDir
    # and the EE's READ-ONLY ROOT -- so `ansible-galaxy` exited 1 with
    # `Unable to create local directories b'/tmp/ansible-home/galaxy_cache':
    # [Errno 30] Read-only file system`, and no collection could be installed at
    # all. `ANSIBLE_LOCAL_TEMP` does not rescue it: the Job already points that
    # at the shared volume and galaxy still fails, because what it needs is
    # `ANSIBLE_HOME/galaxy_cache`. Setting nothing is what works -- measured
    # rc=0 with the cache landing at `/tp/home/.ansible/galaxy_cache` -- because
    # ansible derives it from `~`, and the EE's own `HOME` is on the shared
    # volume. One fewer path to keep in step with the Job's volume layout.
    return {
        "ANSIBLE_GALAXY_SERVER_LIST": SERVER_NAME,
        f"ANSIBLE_GALAXY_SERVER_{upper}_URL": galaxy_server_url(api_url),
        f"ANSIBLE_GALAXY_SERVER_{upper}_TOKEN": token,
        "ANSIBLE_COLLECTIONS_PATH": search_path,
        "ANSIBLE_ROLES_PATH": roles_path,
    }


def _run_install(
    argv_tail: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_file: str | None,
    what: str,
) -> None:
    """One `ansible-galaxy` invocation, run in the execution environment.

    `argv_tail` carries the subcommand, so a caller chooses `role install` or
    `collection install` -- the two have different flags and must not be built
    from one template.

    **It runs in the EE rather than here, and that is a correctness requirement
    rather than tidiness.** `requires_ansible` is not enforced at install time --
    measured, a collection declaring `>=2.20.0` installs on 2.16.19 at exit 0
    with no warning -- so installing with one ansible-core and running with
    another produces a set that fails mid-play, after a configure has already
    begun changing hosts. The agent's allowlist carries `ansible-galaxy` for
    exactly this reason.

    The binary is a NAME, never a path: the agent resolves it with `LookPath`
    inside the EE, where the orchestrator's filesystem does not reach.

    No `child_grace` any more. The agent owns ansible's descriptors and parents
    the process, so the grace is its own fixed contract (one SIGINT, then SIGKILL
    after 25s) and a value passed from here would describe nothing.
    """
    result = ee_client.run(
        "ansible-galaxy",
        argv_tail,
        cwd=str(cwd),
        env=env,
        log_file=log_file,
    )
    if result.error:
        # The agent could not run the entry point at all, which is a failure of
        # the execution environment rather than of the requirements.
        raise CollectionInstallError(
            f"the execution environment could not run ansible-galaxy to install "
            f"{what}: {result.error}",
            exit_code=result.rc,
        )
    if result.rc != 0:
        raise CollectionInstallError(
            f"installing {what} failed; the log above is ansible-galaxy's own output",
            exit_code=result.rc,
        )


def _pip_is_available(env: dict[str, str], cwd: Path, log_file: str | None) -> bool:
    """Whether this execution environment has a usable pip.

    PROBED rather than inferred, and that is deliberate (user's call, #2305):
    pip is **not** part of the custom-EE contract, so an operator-supplied image
    may legitimately not have one, and requiring it at the startup probe would
    fail an image that conforms today. A probe also answers the question that
    matters -- "can pip run here" -- rather than the one a string match on the
    agent's refusal would answer, which is "did this particular message appear".
    """
    result = ee_client.run("pip", ["--version"], cwd=str(cwd), env=env, log_file=log_file)
    return not result.error and result.rc == 0


def install_python_requirements(
    cfg,  # RunnerConfig
    config_dir: Path,
    *,
    log_file: str | None = None,
) -> None:
    """Install the Python dependencies the installed collections declare (#2305).

    `ansible-galaxy` installs a collection's content and not its Python
    requirements, so without this `amazon.aws` installs cleanly and then fails at
    the first task -- or, for a dynamic inventory, at plugin load -- with an
    import error naming boto3. The interpreter that must import them is the EE's,
    which is why the install runs there rather than in the orchestrator.

    **Through Terrapod's PyPI pull-through cache**, which is what makes this work
    in a sealed deployment at all, with the credential in a `.netrc` in the EE's
    own HOME rather than in the index URL -- pip prints its index, and the run's
    log is streamed to the API and the UI.

    **A missing pip is skipped, a failed install is fatal.** The first is the
    user's call and the reason is the custom-EE contract. The second is because a
    missing SDK fails the run anyway, later and less legibly: a partial configure
    against real hosts is the outcome worth refusing a runner slot over.

    A no-op when nothing declares a `requirements.txt`, which is the common case.
    """
    from terrapod.runner.phases import pulumi_deps

    files = python_requirements_files(config_dir)
    if not files:
        logger.info(
            "no collection declares Python requirements; nothing to install",
            config_directory=str(config_dir),
            looked_for=PYTHON_REQUIREMENTS,
        )
        return

    env = pip_env(cfg.api_url)
    pulumi_deps.write_netrc(cfg.api_url, cfg.auth_token, home=ee_home())

    if not _pip_is_available(env, config_dir, log_file):
        logger.warning(
            "this execution environment has no usable pip, so the Python "
            "requirements these collections declare were NOT installed; a task or "
            "inventory plugin needing one will fail with an import error. Add pip "
            "to the image, or install the dependencies into it directly",
            requirements=[str(f) for f in files],
        )
        return

    for path in files:
        logger.info("installing Python requirements", requirements=str(path))
        result = ee_client.run(
            "pip",
            ["install", "--no-input", "--disable-pip-version-check", "-r", str(path)],
            cwd=str(config_dir),
            env=env,
            log_file=log_file,
        )
        if result.error:
            raise CollectionInstallError(
                f"the execution environment could not run pip to install {path}: {result.error}",
                exit_code=result.rc,
            )
        if result.rc != 0:
            raise CollectionInstallError(
                f"installing the Python requirements in {path} failed; pip's own output "
                f"is in the run log. A collection declaring a dependency Terrapod cannot "
                f"install is a configure that would fail at its first task instead",
                exit_code=result.rc,
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
            "no roles or collections declared",
            config_directory=str(config_dir),
            looked_for=list(REQUIREMENTS_PATHS),
        )
        # Not a bare return: an operator may declare a `requirements.txt` and no
        # Galaxy requirements at all -- a `uri` task needing a client, a filter
        # plugin of their own -- and returning here would install nothing while
        # the file sat there looking honoured. There are no collection-declared
        # requirements to find in this branch, by construction, so the only file
        # this can pick up is theirs.
        install_python_requirements(cfg, config_dir, log_file=log_file)
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
    collections = collections_dir()
    roles = roles_dir()
    # Created from here, on the shared volume, so the agent does not have to.
    collections.mkdir(parents=True, exist_ok=True)
    roles.mkdir(parents=True, exist_ok=True)

    if files:
        path = files[0]
        # Roles first, and unconditionally: a file with no `roles:` section
        # prints "Skipping install, no requirements found" and exits 0, so this
        # costs one process rather than a YAML parser the runner does not have.
        # A git-sourced role clones with the credentials `git_auth` has already
        # written into git's own config.
        logger.info("installing the configuration's roles", requirements=str(path))
        _run_install(
            ["role", "install", "-r", str(path), "-p", str(roles)],
            cwd=config_dir,
            env=env,
            log_file=log_file,
            what=f"the roles declared in {path}",
        )
        logger.info("installing the configuration's collections", requirements=str(path))
        _run_install(
            ["collection", "install", "-r", str(path), "-p", str(collections), "--no-cache"],
            cwd=config_dir,
            env=env,
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
                str(collections),
                "--force",
                "--no-cache",
            ],
            cwd=config_dir,
            env=env,
            log_file=log_file,
            what="the collections declared on the workspace",
        )

    # LAST, because it reads the installed tree: a collection's own
    # `requirements.txt` only exists once the collection is on disk, so this
    # cannot be folded into either pass above (#2305).
    install_python_requirements(cfg, config_dir, log_file=log_file)
