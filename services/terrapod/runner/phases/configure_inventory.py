"""Materialising the inventory a configure targets (#1972, #2021).

The API resolved it at run creation and stored it as the run's own artifact,
pinned to the configuration version the run is running -- so the playbook and
the inventory come from one commit by construction. This phase fetches that
document and renders it back to a YAML source for `ansible-playbook`.

**It renders rather than forwarding, because the stored document is not an
ansible source.** It is Terrapod's own shape -- a host map, a group map of
DIRECT members and the nesting -- and JSON is valid YAML, so handing it to
`-i` would have ansible's YAML plugin read `hosts`, `groups` and `children` as
three group NAMES and resolve an inventory of nothing. A render is not a
convenience here; the alternative silently targets an empty set.

**The host variables in the document are already EFFECTIVE**, merged by the
`ansible-inventory` run the API performed -- `group_vars`, `host_vars`, the
`all` group and the declared rows all collapsed per host. So they are emitted
at host level, which is both faithful and precedence-safe: host variables are
ansible's highest precedence, so nothing the rendered file is read alongside
can quietly outrank a value the operator can see in the artifact.

**It fails closed.** A configure whose target set cannot be read must not run:
a playbook pointed at a partial inventory does not fail, it succeeds against
the wrong hosts, which is the one outcome worth refusing a runner slot over.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import structlog

from terrapod.runner.download import download_to_file
from terrapod.runner.runner_config import RunnerConfig
from terrapod.services.ansible_auth import (
    CREDENTIAL_FILE_MODE,
    substitute,
    unresolved,
)
from terrapod.services.inventory_render import build_inventory_document

logger = structlog.get_logger(__name__)

#: The name the rendered file is given.
RENDERED_NAME = "terrapod-resolved-inventory.yml"

#: Where it is written. On the volume the EE SHARES with the orchestrator, which
#: is not a detail: ansible runs in the EE, and the EE mounts only this volume
#: and the workspace -- so a file written to the orchestrator's own `/tmp`, which
#: is where every other substantial runner tempfile goes, is invisible to the
#: process that has to read it and the configure fails on a missing inventory.
#:
#: And not on the workspace volume either, though that is shared too. The file is
#: Terrapod's, not part of the operator's fetched tree: inside `config_directory`
#: ansible would read it AS A SECOND SOURCE the moment their own `-i` names a
#: directory, which is the ordinary arrangement (`inventory = hosts.d`).
#:
#: `job_template` creates it and passes the path; this default matches its
#: constant so a runner-tier test needs no Job.
_DEFAULT_INVENTORY_DIR = "/tp/inventory"


class InventoryUnavailable(RuntimeError):
    """The run's resolved inventory could not be read.

    Raised rather than degraded for the reason in the module docstring: every
    fallback available here targets a set nobody reviewed. Falling back to the
    repository's own inventory files would be the worst of them -- it looks
    like it worked, and it re-reads sources the API already merged, so the
    operator sees a successful configure against a host set that is neither
    what they declared nor what they committed.
    """


def inventory_dir() -> Path:
    """The shared directory the rendered inventory is written to.

    From `TP_EE_INVENTORY_DIR`, which the Job sets. Read from the environment
    rather than taken as an argument, for the same reason as
    `ansible_collections.collections_dir`: the path is a property of the Job's
    volume layout, not of the run.
    """
    return Path(os.environ.get("TP_EE_INVENTORY_DIR") or _DEFAULT_INVENTORY_DIR)


def document_to_source(document: dict[str, Any], *, resolved: dict[str, str] | None = None) -> str:
    """The stored document as a YAML inventory source, with placeholders resolved.

    Shares `build_inventory_document` with the API rather than rendering its own
    YAML: the shape carries group nesting, which is what `--limit` expands
    through, so two renderers that drifted would make a limit mean different
    things on the two sides of the same run.

    `resolved` maps `"<name>.<field>"` to what a `${terrapod:...}` placeholder
    standing for it substitutes (#1933) -- the path of a file the credential
    phase wrote, or a value. Substitution happens HERE and nowhere else: the
    stored artifact carries the placeholder, because that is what the inventory
    said, and this render is local to the Job.

    Raises:
        InventoryUnavailable: a placeholder could not be resolved. Refused
            rather than left in place or blanked -- see `_refuse_unresolved`.
    """
    hosts = document.get("hosts") or {}
    groups = document.get("groups") or {}
    children = document.get("children") or {}

    memberships = [
        (group, host) for group, members in sorted(groups.items()) for host in sorted(members or [])
    ]
    nesting = [
        (parent, child) for parent, kids in sorted(children.items()) for child in sorted(kids or [])
    ]
    # Every group is declared even when it holds nothing, so a playbook naming
    # an empty group reports "no hosts matched" rather than failing to resolve
    # a group that genuinely exists. Group vars are NOT passed: they are
    # already inside each host's variables, and re-emitting them at group level
    # would let a later read apply them twice with the lower precedence.
    group_rows: list[tuple[str, dict[str, Any]]] = [(name, {}) for name in sorted(groups)]
    # A group that appears only as a parent or a child still has to exist.
    for extra in sorted({p for p, _ in nesting} | {c for _, c in nesting}):
        if extra not in groups:
            group_rows.append((extra, {}))

    built = build_inventory_document(
        hosts=sorted((name, vars_ or {}) for name, vars_ in hosts.items()),
        groups=sorted(group_rows),
        memberships=memberships,
        children=nesting,
        global_vars={},
    )
    # Substituted over the WHOLE built document rather than per variable, so a
    # placeholder is resolved wherever the inventory happened to put it -- a host
    # variable, a group variable, or inside a longer string such as
    # `ansible_ssh_common_args="-o IdentityFile=${terrapod:web.private_key}"`,
    # which is how a real estate expresses a per-group key.
    built = substitute(built, resolved or {})
    _refuse_unresolved(built)
    # JSON, not YAML, and that is not a shortcut: **JSON is valid YAML**, and
    # `requirements-runner.txt` deliberately ships no PyYAML -- so serialising
    # here is what keeps this module importable in the runner image at all.
    # Measured against ansible-core 2.21.5: a JSON-serialised inventory is
    # accepted on `.yml`, resolves the same hosts, and expands `--limit` through
    # the same group nesting. `sort_keys` so the same document renders the same
    # bytes, as the API's YAML does.
    return json.dumps(built, indent=2, sort_keys=True)


def _refuse_unresolved(built: dict[str, Any]) -> None:
    """Refuse a render still carrying a placeholder.

    Neither left in place nor blanked, and both alternatives are worse in the
    same direction. Left in place, ansible hands ssh the literal
    `${terrapod:web.private_key}` as a key path and the run fails as an
    unreachable host -- naming neither the credential nor the reference.
    Blanked, it connects with no key at all and the failure is the same shape.
    Either way the operator is told about the symptom rather than the typo.

    The message names every unresolved reference, because the fix is to correct
    that string or declare that credential, and a count tells them neither.
    """
    missing = unresolved(built)
    if not missing:
        return
    raise InventoryUnavailable(
        "this run's inventory refers to credentials that were not delivered: "
        + ", ".join(missing)
        + ". Each `${terrapod:<name>.<field>}` names a variable of that category "
        "whose `key` is <name>; check the name is spelled the same in both places, "
        "that the variable exists on this workspace or a variable set it uses, and "
        "that <field> is a field that credential actually carries."
    )


def materialise(
    cfg: RunnerConfig,
    *,
    client: Any = None,
    resolved: dict[str, str] | None = None,
) -> Path:
    """Fetch the run's resolved inventory and write it as a YAML source.

    Returns the path to pass as the single `-i`. Raises `InventoryUnavailable`
    if the artifact cannot be fetched, is not the document it must be, or a
    placeholder in it cannot be resolved.

    `resolved` carries what each `${terrapod:...}` placeholder stands for
    (#1933). Substitution happens in the RENDERED file and nowhere else -- the
    stored artifact carries the placeholder, which is what the inventory said --
    so a password delivered this way exists on the Job's shared volume and in no
    durable surface.
    """
    url = f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/artifacts/resolved-inventory"
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}

    # The raw download lands in the ORCHESTRATOR's own temp space, deliberately:
    # only the rendered result has to be visible to the EE, and this is deleted
    # either way.
    fd, raw_name = tempfile.mkstemp(prefix="resolved-inventory-", suffix=".json")
    os.close(fd)
    raw = Path(raw_name)
    try:
        result = download_to_file(
            url,
            raw,
            headers=headers,
            api_url=cfg.api_url,
            retries=cfg.download_retries,
            retry_delay_seconds=cfg.download_retry_delay_seconds,
            client=client,
        )
        if not result.ok or not raw.exists():
            raise InventoryUnavailable(
                f"could not download the resolved inventory for run {cfg.run_id} "
                f"(HTTP {result.status}). The API writes it at run creation and refuses "
                "to create a configure it cannot resolve, so an absent artifact means "
                "the object is gone rather than never written -- queue a new run."
            )
        try:
            document = json.loads(raw.read_text())
        except ValueError as exc:
            raise InventoryUnavailable(
                f"the resolved inventory for run {cfg.run_id} is not readable JSON: {exc}"
            ) from exc
    finally:
        raw.unlink(missing_ok=True)

    if not isinstance(document, dict) or "hosts" not in document:
        raise InventoryUnavailable(
            f"the resolved inventory for run {cfg.run_id} is not an inventory document "
            f"(got keys {sorted(document)[:5] if isinstance(document, dict) else type(document).__name__})"
        )

    target = inventory_dir()
    # `parents=True` because a runner-tier test has no Job to have created it,
    # `exist_ok` because in a Job the init container already has.
    target.mkdir(parents=True, exist_ok=True)
    rendered = target / RENDERED_NAME
    # ALWAYS written at the credential mode, not only when a placeholder resolved to a
    # secret. A machine credential's connection password reaches ansible as a
    # VARIABLE -- there is no `ansible_password_file`, so a password can only be
    # one -- which makes this document secret-bearing whenever one is declared.
    # Unconditional rather than conditional because a mode chosen by a branch is
    # a mode that can be chosen wrongly, and nothing but the EE reads this file:
    # it is Terrapod's own render, not part of the operator's fetched tree.
    #
    # `os.open` with the mode at CREATION rather than `write_text` then chmod:
    # the default would be `0644`, so a narrowed-afterwards file is readable for
    # the window in between. Measured before fixing -- the file was `0644` with
    # the password in plaintext.
    fd = os.open(rendered, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, CREDENTIAL_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(document_to_source(document, resolved=resolved))
    logger.info(
        "resolved inventory materialised",
        run_id=cfg.run_id,
        path=str(rendered),
        hosts=len(document.get("hosts") or {}),
        groups=len(document.get("groups") or {}),
    )
    return rendered
