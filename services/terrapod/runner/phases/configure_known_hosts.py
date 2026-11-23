"""Making approved host keys take effect: the host-key anchor (#2592).

Terrapod models which keys a host is trusted to present; OpenSSH enforces it.
This phase is the join: it reads this workspace's approved keys, renders them as
a `known_hosts` file on the volume the execution environment shares, and returns
the environment that points `ssh` at it. Nothing here decides whether a key is
good -- that is the crypto, and it is OpenSSH's.

**The anchor is only raised for a workspace that declared keys.** A workspace
with none writes no file, sets no environment, and behaves exactly as it did
before this phase existed. That gate matters in both directions: without it,
pointing `ssh` at an empty `known_hosts` under `StrictHostKeyChecking=yes` would
refuse every host in every configure (measured), and forcing
`StrictHostKeyChecking=yes` would override an `ansible_host_key_checking: false`
an operator set deliberately in a fleet nobody has pinned.

Read LIVE rather than pinned to the run, unlike the resolved inventory beside
it. The resolved inventory is pinned so the playbook and the host set come from
one commit; a trust anchor has the opposite requirement. A key marked `revoked`
while a run sits queued must be refused by that run, and a snapshot taken at
creation would still trust it. There is also nothing to pin: the route is a pure
function of the rows, so there is no freshness to reason about.

## Why `ANSIBLE_SSH_ARGS`, measured

The anchor has to reach `ssh` as `-o UserKnownHostsFile=`, and every channel
ansible offers for ssh arguments can be set by an inventory VARIABLE, which
outranks the environment. Measured against ansible-core 2.18.3 by reading
`config/manager.py::get_config_value_and_origin`, whose order is `direct` >
`vars` > `keyword` > `cli` > `env` > `ini` > `default`, and confirmed with a
five-host probe:

| channel        | sources                                   | argv position |
|----------------|-------------------------------------------|---------------|
| `ssh_args`     | ini, env, **vars `ansible_ssh_args`**     | **first**     |
| `host_key_checking` | ini, env, **vars**                   | after         |
| `ssh_common_args` | ini, env, **vars**, cli                | after         |
| `ssh_extra_args`  | ini, env, **vars**, cli                | last          |

So there is **no env-only channel** and no cli-only one: `--ssh-common-args` is
beaten by `ansible_ssh_common_args` just as the env var is. What rescues it is
OpenSSH rather than ansible: **for each parameter the FIRST `-o` wins**, measured
with controls both ways on OpenSSH 10.5p1 and 10.0p2. `ssh_args` is emitted
first, so an anchor placed there beats a later `UserKnownHostsFile` from
`ansible_ssh_common_args`, from `ansible_ssh_extra_args`, and beats the
`StrictHostKeyChecking=no` that `ansible_host_key_checking: false` adds -- all
three confirmed in the probe's argv.

`--ssh-common-args` was measured as the alternative and is strictly weaker: it
loses the option outright to `ansible_ssh_common_args`, and loses by POSITION to
both `ansible_ssh_args` and `ansible_host_key_checking`. Three holes against one.

## The hole, and the refusal that closes it

`ansible_ssh_args` sets the same option from a higher-precedence source, so it
does not lose to us on position -- it REPLACES our value, and the probe's argv
for that host carried the operator's path and no trace of the anchor. Nothing
beats it, so shipping without a refusal would be shipping something that looks
enforced and is not.

`refuse_conflicting_variables` refuses it, and refuses the other three shapes
too even though the anchor beats them -- because the question there is not
whether the run is secure but whether a setting an operator can SEE is quietly
doing nothing. The sharpest case is Terrapod's own: a workspace whose
`ansible_host_key_checking` is relaxed reaches the runner as
`ansible_ssh_common_args` on `all` (#2318), so overriding it silently would
leave the workspace page saying verification is relaxed while every run enforced
it. `inventory_resolve._relax_host_key_checking` already states the principle,
about this same setting: *a setting that silently does nothing is worse than one
that refuses.*

The refusal is narrow in the way that matters: it fires only when the workspace
has approved keys -- a surface that ships with #2592 -- so no existing workspace
can meet it, and the only operator who does is one adopting approved keys, who
is exactly the person who should be told the old relaxation is now redundant. A
workspace that pins nothing keeps every one of these as its own business.

## What it does not reach

* **A host that appears only in a repository inventory source**, in the dynamic
  handoff (#2033). Its variables are read by ansible inside the EE and are
  invisible here, so neither its `ansible_host` nor an `ansible_ssh_args` on it
  can be seen. A host can only hold an approved key by being declared, so such
  a host is never pinned; if it is reached by a name this cannot see, OpenSSH
  refuses it, which is the right direction.
* **`GlobalKnownHostsFile`**, deliberately left alone. Our own EE images ship no
  `/etc/ssh/ssh_known_hosts` -- measured: Debian's `openssh-client` and
  `openssh-server` create none -- so it is empty where we control the image, and
  an operator-supplied EE is already trusted code, so overriding a site CA it
  chose to bake in would break a legitimate arrangement for no gain.
* **An older API.** A 404 means an API with no such table, so it has no approved
  keys and there is nothing to enforce; the run proceeds unanchored. The reverse
  skew -- a new API with keys declared and a runner image older than this phase
  -- cannot be detected from here and is an upgrade-ordering requirement
  recorded in `docs/versioning-and-support.md`.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import structlog

from terrapod.runner.phases import configure_inventory
from terrapod.runner.runner_config import RunnerConfig
from terrapod.services.ansible_auth import CREDENTIAL_FILE_MODE
from terrapod.services.known_hosts_render import known_hosts_patterns, render_known_hosts

logger = structlog.get_logger(__name__)

#: The name the rendered anchor is given.
RENDERED_NAME = "terrapod-known-hosts"

#: Where it is written. On the volume the EE shares -- see
#: `job_template._EE_KNOWN_HOSTS_DIR`, which is the authority and which this
#: default matches so a runner-tier test needs no Job.
_DEFAULT_KNOWN_HOSTS_DIR = "/tp/known-hosts"

#: The two ssh options an approved-key anchor owns. A statement about either of
#: them anywhere in the inventory contradicts the anchor -- see
#: `_statements_about_host_keys`, which is where the rule is argued.
ANCHORED_SSH_OPTIONS = ("UserKnownHostsFile", "StrictHostKeyChecking")

#: ansible's OWN default for `ssh_args`, restated because forcing the variable
#: replaces the option rather than adding to it.
#:
#: Read from the connection plugin's `DOCUMENTATION` rather than recalled, and
#: identical across the two ansible-core versions to hand. Dropping it would
#: silently disable SSH multiplexing and compression, which on a many-host
#: playbook is the difference between one connection per host and one per task.
#: `test_the_anchor_keeps_ansibles_own_default` pins it.
ANSIBLE_DEFAULT_SSH_ARGS = "-C -o ControlMaster=auto -o ControlPersist=60s"


class HostKeyAnchorUnavailable(RuntimeError):
    """The approved host keys could not be turned into an enforced anchor.

    Raised rather than degraded, because every degradation available here is the
    failure the feature exists to prevent. Proceeding without the anchor runs a
    playbook against a fleet whose keys an operator declared and which nothing
    checked; proceeding with a partial one refuses the hosts it happens to have
    missed and reports it as a key mismatch. Neither is distinguishable, from
    the run log, from a configure that was verified.
    """


def known_hosts_dir() -> Path:
    """The shared directory the anchor is written to.

    From `TP_EE_KNOWN_HOSTS_DIR`, which the Job sets. Read from the environment
    rather than taken as an argument for the same reason as
    `configure_inventory.inventory_dir`: the path is a property of the Job's
    volume layout, not of the run.
    """
    return Path(os.environ.get("TP_EE_KNOWN_HOSTS_DIR") or _DEFAULT_KNOWN_HOSTS_DIR)


def ssh_args(path: Path) -> str:
    """The `ssh_args` value that makes `path` this run's trust anchor.

    Ours FIRST and ansible's own default after, which is the whole mechanism:
    OpenSSH takes the first value for each parameter, so an operator's later
    `UserKnownHostsFile` or `StrictHostKeyChecking=no` -- wherever it arrives
    from -- is read and discarded. See the module docstring for the measurements.

    `StrictHostKeyChecking=yes` is set rather than left to OpenSSH's own `ask`.
    Non-interactively `ask` refuses too, so the file would be enforced either
    way -- but only by accident of there being no tty, and `ask` loses to an
    `ansible_host_key_checking: false` that `yes` beats on position. Stating the
    policy is also what makes the run log say what was enforced.
    """
    return f"-o UserKnownHostsFile={path} -o StrictHostKeyChecking=yes {ANSIBLE_DEFAULT_SSH_ARGS}"


def hosts_in(inventory: Sequence[Path]) -> dict[str, dict[str, Any]]:
    """Each host's effective variables, from the sources this phase can read.

    Only the files `configure_inventory` wrote, filtered by name. A repository
    source in the dynamic handoff is read by ansible inside the EE and is
    invisible here -- see the module docstring.

    Raises:
        HostKeyAnchorUnavailable: one of our own files could not be read. That
            is a packaging defect rather than an operator's input, and it is
            fatal here because every host's connect address lives in it: a
            silently empty map would key the anchor on inventory names and
            refuse a fleet addressed by `ansible_host`.
    """
    hosts: dict[str, dict[str, Any]] = {}
    for source in inventory:
        if not configure_inventory.is_terrapod_rendered(source):
            continue
        try:
            hosts.update(configure_inventory.hosts_from_rendered(source.read_text()))
        except (OSError, ValueError) as exc:
            raise HostKeyAnchorUnavailable(
                f"this workspace declares approved SSH host keys, and the rendered "
                f"inventory at {source} could not be read back to find each host's "
                f"connect address ({exc}). Terrapod wrote that file moments earlier in "
                f"this same Job, so this is a defect in the runner image rather than "
                f"anything in the workspace."
            ) from exc
    return hosts


def _statements_about_host_keys(variables: Mapping[str, Any]) -> list[str]:
    """Which of this host's variables state a host-key policy of their own.

    Three shapes, and the rule is one rule: once a workspace declares approved
    keys, host-key verification is the platform's, so any of these is a setting
    that cannot take effect -- and `inventory_resolve._relax_host_key_checking`
    says it best, in its own comment about the same situation: *a setting that
    silently does nothing is worse than one that refuses*.

    * **`ansible_ssh_args`, whatever it contains.** It sets the same ansible
      option the anchor uses, from a higher-precedence source, so it REPLACES
      the anchor rather than arriving after it -- measured: that host's argv
      carried the operator's path and no trace of ours. This is the one case
      where the alternative is not merely a silent setting but a silently
      unverified run.
    * **`ansible_ssh_common_args` or `ansible_ssh_extra_args` naming either
      option.** These arrive AFTER the anchor and lose to it, because OpenSSH
      takes the first value -- so the run is secure and their statement is
      inert. Scoped to the two OPTIONS and not to the variables, so a
      `ProxyCommand`, a `ServerAliveInterval` or an `IdentityFile` in the same
      variable is untouched; those are the overwhelmingly common contents and
      they still reach ssh.
    * **`ansible_host_key_checking: false`.** Same: the `-o
      StrictHostKeyChecking=no` it adds arrives after ours and loses. Read
      through `_reads_as_false`, because an inventory variable reaches the
      document uncoerced and `"false"` is the spelling an operator is most
      likely to write.
    """
    found: list[str] = []
    for name in ("ansible_ssh_args", "ansible_ssh_common_args", "ansible_ssh_extra_args"):
        value = variables.get(name)
        if value is None:
            continue
        if name == "ansible_ssh_args" or any(
            option in str(value) for option in ANCHORED_SSH_OPTIONS
        ):
            found.append(name)
    for name in ("ansible_host_key_checking", "ansible_ssh_host_key_checking"):
        value = variables.get(name)
        if value is not None and _reads_as_false(value):
            found.append(name)
    return found


#: What ansible's own `boolean()` reads as false, so a stringified boolean is
#: caught too. An inventory variable is NOT coerced on its way through
#: `ansible-inventory` -- the config layer coerces at `get_option` time -- so a
#: document legitimately carries the string `"false"`, and an `is False` check
#: alone would let the one spelling an operator is most likely to write slip
#: past the refusal while still losing to the anchor. Secure, and the setting
#: silently does nothing, which is the thing being refused.
_FALSE_SPELLINGS = frozenset({"false", "no", "n", "off", "0", "f"})


def _reads_as_false(value: Any) -> bool:
    if value is False:
        return True
    if isinstance(value, bool):
        return False
    return str(value).strip().lower() in _FALSE_SPELLINGS


def refuse_conflicting_variables(hosts: Mapping[str, Mapping[str, Any]]) -> None:
    """Refuse a run whose inventory also states a host-key policy.

    Uniform across the shapes `_statements_about_host_keys` finds, even though
    the anchor beats most of them: the question is not whether the run would be
    secure -- it would -- but whether a setting an operator can see is quietly
    doing nothing. The sharpest instance is Terrapod's own: a workspace with
    `ansible_host_key_checking` relaxed reaches the runner as
    `ansible_ssh_common_args` on `all` (#2318), so without this the workspace
    page would say verification is relaxed while every run enforced it. That is
    a lie in the product's own interface, which is worse than a failed run.

    **Nothing existing can meet this.** It fires only when the workspace has
    approved keys, which is a surface that ships with #2592 -- so the only
    operator who sees it is one adopting the new feature, who is exactly the
    person who should be told that the old workaround is now redundant.

    Only called when there are keys, so a workspace that pins nothing keeps all
    of these as its own business, unchanged.
    """
    conflicting: dict[str, list[str]] = {}
    for name, variables in sorted(hosts.items()):
        statements = _statements_about_host_keys(variables or {})
        if statements:
            conflicting[name] = statements
    if not conflicting:
        return

    named = ", ".join(f"{host} ({', '.join(v)})" for host, v in conflicting.items())
    variables_seen = sorted({v for values in conflicting.values() for v in values})
    raise HostKeyAnchorUnavailable(
        f"this workspace declares approved SSH host keys, so host-key verification is "
        f"the platform's -- and its inventory also states a host-key policy of its own, "
        f"on: {named}. Those two contradict, and rather than leave the losing one "
        f"silently doing nothing the configure is refused here.\n"
        f"To fix it, remove {' / '.join(variables_seen)} from those hosts and groups. "
        f"If a value arrived there from this workspace's `ansible_host_key_checking` "
        f"setting rather than from your own inventory, turn that setting back on: "
        f"approved host keys are what it was a substitute for.\n"
        f"Anything in ansible_ssh_common_args or ansible_ssh_extra_args that is NOT "
        f"about host keys -- a ProxyCommand, an IdentityFile, a ServerAliveInterval -- "
        f"keeps working and is not what this refuses."
    )


def _fetch(cfg: RunnerConfig, client: httpx.Client) -> dict[str, list[dict[str, Any]]] | None:
    """This workspace's approved keys by host name, or None if unserved.

    `None` rather than an empty map for a 404, and the two are different
    answers: an API that does not serve this route has no such table, so it has
    no approved keys and there is nothing to enforce. An empty map from an API
    that DOES serve it means the operator has declared none, which is the same
    outcome by a different route and is handled the same way by the caller.

    Unpaged deliberately -- the route returns the full list when no paging
    params are given -- and the count is checked against `meta` so that a future
    bounded default fails here, naming itself, rather than anchoring the first
    page of hosts and leaving the rest to be refused as unknown.
    """
    url = (
        f"{cfg.api_url}/api/terrapod/v1/workspaces/ws-{cfg.workspace_id.removeprefix('ws-')}"
        f"/inventory/host-keys"
    )
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}
    last = "no attempt made"
    for attempt in (1, 2, 3):
        try:
            resp = client.get(url, headers=headers)
            if resp.status_code == 404:
                logger.info("approved host keys not served by this API", url=url)
                return None
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except json.JSONDecodeError as exc:
                    last = f"200 with a body that is not JSON: {exc}"
                    break
                return _parse(body)
            if 400 <= resp.status_code < 500:
                last = f"HTTP {resp.status_code}: {resp.text[:400]}"
                break
            last = f"HTTP {resp.status_code}"
        except httpx.RequestError as exc:
            last = str(exc)
        if attempt < 3:
            time.sleep(2 ** (attempt - 1))

    raise HostKeyAnchorUnavailable(
        f"could not read this workspace's approved SSH host keys: {last}. Refused rather "
        f"than continuing, because an unanswered read is indistinguishable from a "
        f"workspace that has declared none -- so continuing would run a configure with "
        f"the host keys unchecked on exactly the fleets somebody pinned."
    )


def _parse(body: Any) -> dict[str, list[dict[str, Any]]]:
    """The route's JSON:API collection as `{host name: [key attributes]}`."""
    data = (body or {}).get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise HostKeyAnchorUnavailable(
            "the approved-host-keys route answered 200 with a body that is not a "
            "JSON:API collection, so this run cannot tell which keys are approved."
        )
    meta_total = (((body or {}).get("meta") or {}).get("pagination") or {}).get("total-count")
    if isinstance(meta_total, int) and meta_total != len(data):
        raise HostKeyAnchorUnavailable(
            f"the approved-host-keys route returned {len(data)} of {meta_total} hosts. "
            f"This run reads it unpaged, so a bounded page would anchor some hosts and "
            f"leave the rest to be refused as unknown -- refused here instead, where the "
            f"cause is nameable."
        )
    out: dict[str, list[dict[str, Any]]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("id")
        rows = (entry.get("attributes") or {}).get("keys")
        if isinstance(name, str) and isinstance(rows, list):
            out[name] = [row for row in rows if isinstance(row, dict)]
    return out


def _write(path: Path, text: str) -> None:
    """Write the anchor at the credential mode.

    `0600`, owner-only, which is correct because `_ee_security_context` pins the
    EE's `runAsUser` to the orchestrator's own uid (#1928) -- so the two
    containers exchange files as the same user and the group bit carries none of
    the contract. Group-readable would be a wider grant than the work needs.

    The mode is set at CREATION rather than chmod-ed after, as
    `configure_inventory._write` does and for the same reason: the default would
    be `0644`, leaving a window. It matters less here than for a private key --
    a host's public key is public, which is why it is neither encrypted at rest
    nor redacted from the log -- but a file ssh reads to decide what to trust
    should not be writable by anything that did not write it.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, CREDENTIAL_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def materialise(
    cfg: RunnerConfig,
    *,
    inventory: Sequence[Path],
    client: httpx.Client | None = None,
) -> dict[str, str]:
    """Raise this run's host-key anchor; return the environment ansible needs.

    Returns `{}` when this workspace has declared no approved keys, or when the
    API does not serve them at all. That is not a soft failure -- it is the
    pre-#2592 behaviour, exactly: no file is written, no ssh argument is forced,
    and whatever the operator's own inventory and `ansible.cfg` said still
    decides host-key checking. The gate is what keeps this phase from refusing
    every host in every workspace that pins nothing.

    Returns `{"ANSIBLE_SSH_ARGS": ...}` otherwise, which the configure phase
    merges as a FORCED setting rather than as `extra_env`: the anchor has to
    outrank anything another phase contributed, which is the same ordering the
    Job spec gives our own env over an operator's `extraEnv`.

    `inventory` is the `-i` list `configure_inventory.materialise` returned.
    Taken as an argument rather than re-fetched so there is one download of the
    resolved inventory per run, and so this phase reads the same bytes ansible
    will.

    Raises:
        HostKeyAnchorUnavailable: the keys could not be read, the inventory
            could not be read back, a key cannot be rendered, or the inventory
            sets a variable that would displace the anchor.
    """
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(cfg.upload_timeout_seconds, connect=10.0))
    try:
        keys = _fetch(cfg, client)
    finally:
        if own_client:
            client.close()

    if keys is None:
        return {}
    # Hosts with an empty key list are filtered out rather than rendered as a
    # host with no lines: the route is keyed on host name and only lists hosts
    # that have keys, so this is belt and braces against a future shape -- but
    # an empty list must not make `keys` truthy, or a workspace with none would
    # take the anchored branch and refuse its whole fleet.
    keys = {name: rows for name, rows in keys.items() if rows}
    if not keys:
        logger.info("no approved host keys declared — host-key checking left unchanged")
        return {}

    hosts = hosts_in(inventory)
    refuse_conflicting_variables(hosts)

    patterns = {name: known_hosts_patterns(name, hosts.get(name)) for name in keys}
    try:
        content = render_known_hosts(keys, patterns=patterns)
    except ValueError as exc:
        raise HostKeyAnchorUnavailable(
            f"this workspace's approved SSH host keys could not be rendered as a "
            f"known_hosts file: {exc}"
        ) from exc

    target = known_hosts_dir()
    # `parents=True` because a runner-tier test has no Job to have created it,
    # `exist_ok` because in a Job the init container already has.
    target.mkdir(parents=True, exist_ok=True)
    path = target / RENDERED_NAME
    _write(path, content)

    unpinned = sorted(set(hosts) - set(keys))
    logger.info(
        "host-key anchor raised",
        path=str(path),
        pinned_hosts=len(keys),
        lines=content.count("\n"),
        # Named rather than counted, because under StrictHostKeyChecking=yes
        # these are the hosts the configure will REFUSE: the file is an
        # allow-list, so a host the workspace targets and has not pinned is not
        # merely unverified, it is unreachable. That is the correct direction
        # and it is the thing an operator needs to read.
        unpinned_hosts=unpinned,
    )
    return {"ANSIBLE_SSH_ARGS": ssh_args(path)}
