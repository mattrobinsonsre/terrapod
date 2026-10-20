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
from terrapod.services.inventory_render import render_inventory_yaml

logger = structlog.get_logger(__name__)

#: The name the rendered file is given. Inside the run's own scratch directory,
#: never in the fetched tree: writing into `config_directory` would put a file
#: ansible then reads AS INVENTORY beside the operator's own, and a directory
#: source would pick it up as a second source of the same hosts.
RENDERED_NAME = "terrapod-resolved-inventory.yml"


class InventoryUnavailable(RuntimeError):
    """The run's resolved inventory could not be read.

    Raised rather than degraded for the reason in the module docstring: every
    fallback available here targets a set nobody reviewed. Falling back to the
    repository's own inventory files would be the worst of them -- it looks
    like it worked, and it re-reads sources the API already merged, so the
    operator sees a successful configure against a host set that is neither
    what they declared nor what they committed.
    """


def _resolve_tmpdir() -> str | None:
    """The configured ephemeral directory, or None for the system default.

    A resolved inventory for a large fleet is a substantial document and the
    runner's `/tmp` is not guaranteed to be disk-backed, so it follows the same
    four-line resolver every other substantial tempfile here does.
    """
    configured = os.environ.get("TP_SCRATCH_DIR")
    if configured and os.path.isdir(configured):
        return configured
    return None


def document_to_source(document: dict[str, Any]) -> str:
    """The stored document as a YAML inventory source.

    Shares `render_inventory_yaml` with the API rather than rendering its own
    YAML: the shape carries group nesting, which is what `--limit` expands
    through, so two renderers that drifted would make a limit mean different
    things on the two sides of the same run.
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

    return render_inventory_yaml(
        hosts=sorted((name, vars_ or {}) for name, vars_ in hosts.items()),
        groups=sorted(group_rows),
        memberships=memberships,
        children=nesting,
        global_vars={},
    )


def materialise(cfg: RunnerConfig, *, client: Any = None) -> Path:
    """Fetch the run's resolved inventory and write it as a YAML source.

    Returns the path to pass as the single `-i`. Raises `InventoryUnavailable`
    if the artifact cannot be fetched or is not the document it must be.
    """
    url = f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/artifacts/resolved-inventory"
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}

    fd, raw_name = tempfile.mkstemp(
        prefix="resolved-inventory-", suffix=".json", dir=_resolve_tmpdir()
    )
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

    rendered = Path(tempfile.mkdtemp(prefix="inventory-", dir=_resolve_tmpdir())) / RENDERED_NAME
    rendered.write_text(document_to_source(document))
    logger.info(
        "resolved inventory materialised",
        run_id=cfg.run_id,
        path=str(rendered),
        hosts=len(document.get("hosts") or {}),
        groups=len(document.get("groups") or {}),
    )
    return rendered
