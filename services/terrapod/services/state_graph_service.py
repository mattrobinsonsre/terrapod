"""Derive a single-workspace state resource graph (#765).

The third WebGL surface in the #736 visualisation initiative, after plan
blast-radius (#762) and estate topology (#763). Estate topology shows how
*workspaces* relate; this is the complement — how the *resources inside one
workspace* relate, as the resource-level dependency DAG that Terraform records
in the workspace's stored state.

For a given `StateVersion` (the workspace's current version by default, or any
older one the caller picks), we load the state blob from object storage,
decrypt it if app-layer state encryption was on when it was written (#635), and
parse the Terraform state v4 JSON:

  - a **node** per resource address (managed `type.name` / data
    `data.type.name`, module-prefixed when nested), collapsing the per-instance
    fan-out of `count`/`for_each` back to one node;
  - a **depends-on edge** per entry in each instance's `dependencies` array
    (unioned across instances), kept only when the target is another node in
    this state.

Nodes carry `type` / `mode` / `module` / `provider` so the client can pick the
grouping axis at view time (the same label-agnostic pivot approach estate uses)
and `indeg` (how many resources depend on this one) so the UI can size hubs
like a VPC or a shared security group.

**RBAC (hard):** gated on ``state:read`` — the *same* capability as downloading
the raw state — because the graph is derived from the secret-bearing state
blob. Seeing the resource graph therefore requires the same trust as reading
the state it comes from.

**Rules 13/14:** the state blob can be tens of MB; ``json.loads`` + the graph
build is CPU work, so the whole parse+assemble step runs in a worker thread
(``asyncio.to_thread``), never on the event loop. We only ever hold the parsed
structure the existing raw-state download endpoint already holds — no new
tempfile, so rule 14 doesn't apply here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.db.models import StateVersion, Workspace
from terrapod.logging_config import get_logger
from terrapod.services.workspace_rbac_service import resolve_workspace_capabilities_for
from terrapod.storage import get_storage
from terrapod.storage.keys import state_key

logger = get_logger(__name__)

# Guardrail: a state with tens of thousands of resources would make the WebGL
# graph unusable and the payload huge. Cap the node count and report the
# truncation honestly in `meta` rather than silently dropping resources.
MAX_NODES = 2000


def _provider_short(provider: str) -> str:
    """`provider["registry.terraform.io/hashicorp/aws"]` -> `aws`."""
    if not provider:
        return ""
    inner = provider
    if "[" in inner:
        inner = inner[inner.find("[") + 1 : inner.rfind("]")].strip().strip('"')
    # inner is now e.g. registry.terraform.io/hashicorp/aws (or just a name)
    return inner.rsplit("/", 1)[-1] if inner else ""


def _resource_address(module: str, mode: str, rtype: str, name: str) -> str:
    """Build the canonical resource address used as the node id.

    Matches the form Terraform records in `dependencies`, so edges resolve:
      managed, root:   aws_instance.web
      data,    root:   data.aws_ami.ubuntu
      managed, module: module.vpc.aws_subnet.private
    """
    base = f"{rtype}.{name}" if mode != "data" else f"data.{rtype}.{name}"
    return f"{module}.{base}" if module else base


def build_graph_from_state(state: dict) -> dict:
    """Pure transform: Terraform state v4 dict -> {nodes, edges, meta}.

    Separated from I/O so it's trivially unit-testable and runs in a thread.
    """
    resources = state.get("resources") or []
    total_resources = len(resources)

    nodes: list[dict] = []
    node_ids: set[str] = set()
    # source address -> set of dependency addresses (unioned across instances)
    deps: dict[str, set[str]] = {}
    truncated = False

    for res in resources:
        if not isinstance(res, dict):
            continue
        module = res.get("module") or ""
        mode = res.get("mode") or "managed"
        rtype = res.get("type") or ""
        name = res.get("name") or ""
        if not rtype or not name:
            continue
        addr = _resource_address(module, mode, rtype, name)
        # instances count drives the nucleus (a count/for_each resource is ONE
        # node drawn as a clump of `instances` pearls). A resource with no
        # instances (count = 0) is still a single node.
        n_instances = len(res.get("instances") or [])
        if addr in node_ids:
            # Same address seen twice (shouldn't happen in valid state) — merge.
            by_id_local = next((nd for nd in nodes if nd["id"] == addr), None)
            if by_id_local is not None:
                by_id_local["instances"] = by_id_local.get("instances", 1) + n_instances
        elif len(node_ids) >= MAX_NODES:
            truncated = True
            continue
        else:
            node_ids.add(addr)
            nodes.append(
                {
                    "id": addr,
                    "kind": "resource",
                    "name": addr,
                    "type": rtype,
                    "mode": mode,
                    # "" for root-module resources (matches the impact graph's
                    # convention so the shared renderer's module-cluster force
                    # skips the root module rather than boxing it).
                    "module": module,
                    "provider": _provider_short(res.get("provider") or ""),
                    "instances": max(n_instances, 1),
                    "indeg": 0,
                }
            )
        bucket = deps.setdefault(addr, set())
        for inst in res.get("instances") or []:
            if not isinstance(inst, dict):
                continue
            for dep in inst.get("dependencies") or []:
                if isinstance(dep, str) and dep != addr:
                    bucket.add(dep)

    edges: list[dict] = []
    for src, targets in deps.items():
        if src not in node_ids:
            continue
        for tgt in sorted(targets):
            if tgt in node_ids:
                edges.append({"source": src, "target": tgt, "kind": "depends-on"})

    by_id = {n["id"]: n for n in nodes}
    for e in edges:
        by_id[e["target"]]["indeg"] += 1

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "counts": {"resources": len(nodes), "edges": len(edges)},
            "truncated": truncated,
            "total_resources": total_resources,
            "max_nodes": MAX_NODES,
        },
    }


#: The root resource every Pulumi deployment carries. It is the stack itself,
#: not infrastructure, and it is the parent of everything else — so drawing it
#: would add one node that every other node points at, which says nothing and
#: makes the layout a hub. Its `outputs` are the stack outputs, read separately.
_PULUMI_STACK_TYPE = "pulumi:pulumi:Stack"

#: Matches `pulumi_service.PULUMI_ENGINE`; the value stored in `Workspace.engine`.
PULUMI_ENGINE = "pulumi"


def _urn_name(urn: str) -> str:
    """The resource's own name: the last `::` segment of its URN."""
    return urn.rsplit("::", 1)[-1] if urn else ""


def _pulumi_package(rtype: str) -> str:
    """`aws:s3/bucket:Bucket` -> `aws`.

    Taken from the type rather than the `provider` URN because the type is
    always present, while `provider` is absent on component resources and on
    anything using the default provider implicitly.
    """
    return rtype.split(":", 1)[0] if rtype else ""


def build_graph_from_deployment(deployment: dict) -> dict:
    """Pure transform: a Pulumi deployment dict -> {nodes, edges, meta}.

    Same shape as `build_graph_from_state`, deliberately: the graph is rendered
    by one component, and a second shape would mean a second renderer to keep in
    step with it.

    The vocabulary maps rather than translates. A URN is the node id, because it
    is what `dependencies` and `parent` reference — the same reason Terraform's
    resource address is the id there. `mode` carries Pulumi's own distinction,
    `custom` (a real provider resource) against a component (a grouping the
    program declares), which is the nearest true thing to Terraform's
    managed/data split rather than a pretence of it.
    """
    resources = deployment.get("resources") or []
    total_resources = len(resources)

    nodes: list[dict] = []
    node_ids: set[str] = set()
    deps: dict[str, set[str]] = {}
    truncated = False
    # Every resource parents to the root stack; an edge to it would be noise.
    roots = {
        r.get("urn")
        for r in resources
        if isinstance(r, dict) and r.get("type") == _PULUMI_STACK_TYPE
    }

    for res in resources:
        if not isinstance(res, dict):
            continue
        urn = res.get("urn") or ""
        rtype = res.get("type") or ""
        if not urn or rtype == _PULUMI_STACK_TYPE:
            continue
        if len(nodes) >= MAX_NODES:
            truncated = True
            break
        nodes.append(
            {
                "id": urn,
                "kind": "resource",
                "name": _urn_name(urn),
                "type": rtype,
                "mode": "managed" if res.get("custom") else "component",
                "module": "",
                "provider": _pulumi_package(rtype),
                # Pulumi resources are individual; there is no count/for_each
                # fan-out collapsed behind one address as in Terraform.
                "instances": 1,
                "indeg": 0,
            }
        )
        node_ids.add(urn)

        targets = {d for d in (res.get("dependencies") or []) if isinstance(d, str)}
        parent = res.get("parent")
        if isinstance(parent, str) and parent and parent not in roots:
            # A child genuinely cannot exist without its parent, so this is a
            # dependency — but only where the parent is a real resource.
            targets.add(parent)
        if targets:
            deps[urn] = targets

    edges: list[dict] = []
    for src, targets in deps.items():
        if src not in node_ids:
            continue
        for tgt in sorted(targets):
            if tgt in node_ids:
                edges.append({"source": src, "target": tgt, "kind": "depends-on"})

    by_id = {n["id"]: n for n in nodes}
    for e in edges:
        by_id[e["target"]]["indeg"] += 1

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "counts": {"resources": len(nodes), "edges": len(edges)},
            "truncated": truncated,
            "total_resources": total_resources,
            "max_nodes": MAX_NODES,
        },
    }


def stack_outputs(deployment: dict) -> dict:
    """The stack's outputs, with secrets masked (#1568).

    They live on the root stack resource, which is the one resource in a
    deployment that is not infrastructure. A secret wears Pulumi's own
    ciphertext envelope, and this reports that a value exists without revealing
    it — the same bargain the rest of Terrapod strikes with a sensitive value.
    """
    from terrapod.services.pulumi_state_service import SECRET_SIG, SECRET_SIG_KEY

    def _sealed(value: object) -> bool:
        return isinstance(value, dict) and value.get(SECRET_SIG_KEY) == SECRET_SIG

    for res in deployment.get("resources") or []:
        if isinstance(res, dict) and res.get("type") == _PULUMI_STACK_TYPE:
            outputs = res.get("outputs") or {}
            if not isinstance(outputs, dict):
                return {}
            return {
                key: ("(sensitive)" if _sealed(value) else value) for key, value in outputs.items()
            }
    return {}


async def derive_state_graph(
    db: AsyncSession,
    user: AuthenticatedUser,
    workspace_id: str,
    state_version_id: str | None = None,
) -> dict:
    """Build the resource graph for a workspace's state version.

    `state_version_id` (accepts the `sv-` prefix) selects a specific version;
    when omitted, the current (highest-serial) version is used. Returns a graph
    plus `meta.versions` (the picker list) and `meta.state_version` (the one
    rendered). Never raises on a workspace that has no state yet — returns an
    empty graph with an empty version list.
    """
    # The engine-agnostic lookup, not `tfe_v2._get_workspace_by_id` (#1568).
    # That one applies `_engine_filter`, which pins the TFE-compatible surface
    # to Terraform — correct there, and the reason this endpoint answered 404
    # for every Pulumi workspace rather than rendering an empty graph.
    from terrapod.api.routers.workspace_extensions import _native_workspace

    ws: Workspace = await _native_workspace(workspace_id, db)

    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.STATE_READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires state:read permission on workspace",
        )

    versions = (
        (
            await db.execute(
                select(StateVersion)
                .where(StateVersion.workspace_id == ws.id)
                .order_by(StateVersion.serial.desc())
            )
        )
        .scalars()
        .all()
    )

    version_list = [
        {
            "id": f"sv-{sv.id}",
            "serial": sv.serial,
            "created_at": sv.created_at.isoformat().replace("+00:00", "Z"),
            "is_current": i == 0,
        }
        for i, sv in enumerate(versions)
    ]

    def _empty(sv_meta: dict | None) -> dict:
        return {
            "nodes": [],
            "edges": [],
            "meta": {
                "counts": {"resources": 0, "edges": 0},
                "truncated": False,
                "total_resources": 0,
                "max_nodes": MAX_NODES,
                "versions": version_list,
                "state_version": sv_meta,
            },
        }

    if not versions:
        return _empty(None)

    # Resolve which version to render.
    if state_version_id:
        wanted = state_version_id.removeprefix("sv-")
        target = next((sv for sv in versions if str(sv.id) == wanted), None)
        if target is None:
            raise HTTPException(status_code=404, detail="State version not found")
    else:
        target = versions[0]

    sv_meta = next(v for v in version_list if v["id"] == f"sv-{target.id}")

    # A version row can exist before its /content PUT landed (state_size == 0);
    # treat that as an empty graph rather than a 404.
    if target.state_size == 0:
        return _empty(sv_meta)

    storage = get_storage()
    key = state_key(str(target.workspace_id), str(target.id))
    try:
        data = await storage.get(key)
    except Exception:
        # Metadata row without a backing object — surface as empty, not a 500.
        logger.warning("state_graph_blob_missing", state_version_id=str(target.id))
        return _empty(sv_meta)

    from terrapod.crypto.state import decrypt_state_bytes

    data = await decrypt_state_bytes(data)

    def _parse_and_build(raw: bytes) -> dict:
        import json

        document = json.loads(raw)
        # Each engine's state is read by the builder that understands it. A
        # Pulumi deployment put through the Terraform builder does not fail —
        # it finds no `mode`/`name`/`instances` and returns an empty graph,
        # which is a silent wrong answer rather than an error (#1568).
        if (ws.engine or "").strip().lower() == PULUMI_ENGINE:
            return build_graph_from_deployment(document)
        return build_graph_from_state(document)

    try:
        graph = await asyncio.to_thread(_parse_and_build, data)
    except (ValueError, TypeError):
        logger.warning("state_graph_parse_failed", state_version_id=str(target.id))
        return _empty(sv_meta)

    graph["meta"]["versions"] = version_list
    graph["meta"]["state_version"] = sv_meta
    return graph


def terraform_outputs(state: dict) -> dict:
    """A Terraform state's outputs, with sensitive ones masked (#1568).

    State v4 records `{"outputs": {"name": {"value": ..., "sensitive": bool}}}`,
    so unlike Pulumi the state says outright which are sensitive and the marker
    needs no decoding.
    """
    outputs = state.get("outputs")
    if not isinstance(outputs, dict):
        return {}
    masked: dict = {}
    for key, entry in outputs.items():
        if not isinstance(entry, dict):
            masked[key] = entry
            continue
        masked[key] = "(sensitive)" if entry.get("sensitive") else entry.get("value")
    return masked


#: go-tfe's coarse `type` string, by the head of the cty type.
#:
#: Measured against OpenTofu v1.13.1 rather than assumed, because the obvious
#: guesses are wrong: an HCL LIST literal is recorded as `["tuple", [...]]`, not
#: `["list", "string"]`, and a MAP literal as `["object", {...}]`, not
#: `["map", ...]`. `list`/`set`/`map` still appear when an output or variable
#: declares an explicit type, so all six heads are handled.
_COARSE_TYPE: dict[str, str] = {
    "list": "array",
    "set": "array",
    "tuple": "array",
    "object": "object",
    "map": "object",
}


@dataclass(frozen=True)
class StateOutput:
    """One root output, with everything the TFE output document needs.

    Separate from `terraform_outputs` rather than replacing it: that one masks
    into the literal string `"(sensitive)"` and drops both the type AND the
    per-output `sensitive` boolean, and the web stack-outputs panel matches that
    literal and states that a sensitive value never reaches the browser. Two
    readers over one blob, each honest about what it is for.
    """

    name: str
    value: object
    sensitive: bool
    #: The cty type as JSON, verbatim from state -- go-tfe's `detailed-type`.
    #: `None` only when the state does not record one, which v4 always does;
    #: the CLI reads a nil here as "fall back to reading full state", a path
    #: Terrapod serves, so degrading is safe rather than fatal.
    detailed_type: object | None
    #: go-tfe's coarse `type`, derived from the above.
    type: str


def typed_terraform_outputs(state: dict) -> list[StateOutput]:
    """A Terraform state's outputs WITH their types and sensitivity, unmasked.

    Terraform-only by construction: a Pulumi stack's outputs carry no type at
    all, so there is nothing to put in `detailed-type` for them, which is one of
    the reasons the TFE output routes are not dual-mounted.

    `sensitive` is read with a default because state v4 OMITS the key entirely
    for a non-sensitive output -- measured; only the sensitive one carries it.
    """
    outputs = state.get("outputs")
    if not isinstance(outputs, dict):
        return []
    out: list[StateOutput] = []
    for name in sorted(outputs):
        entry = outputs[name]
        if not isinstance(entry, dict):
            # Not a shape state v4 produces; report it as an untyped scalar
            # rather than dropping the output, which would read as "no such
            # output" to a caller that can see it in the state file.
            out.append(
                StateOutput(name=name, value=entry, sensitive=False, detailed_type=None, type="")
            )
            continue
        detailed = entry.get("type")
        out.append(
            StateOutput(
                name=name,
                value=entry.get("value"),
                sensitive=bool(entry.get("sensitive", False)),
                detailed_type=detailed,
                type=coarse_output_type(detailed),
            )
        )
    return out


def coarse_output_type(detailed: object) -> str:
    """go-tfe's `type` string for a cty type. Empty when it cannot be named."""
    if isinstance(detailed, str):
        return detailed
    if isinstance(detailed, list) and detailed and isinstance(detailed[0], str):
        return _COARSE_TYPE.get(detailed[0], detailed[0])
    return ""


async def load_state_document(sv: StateVersion) -> dict | None:
    """The parsed state document for a state version, or `None`.

    Extracted so the TFE output routes and `derive_state_outputs` share one
    path through storage, decryption and parsing. `None` for every reason a
    document cannot be read -- absent blob, undecryptable envelope, unparseable
    JSON -- because each is the same answer to the caller and each is logged
    here rather than swallowed, so a deployment where this never works does not
    look like one with no outputs.

    `json.loads` runs in a thread: a state document is routinely tens of MB and
    parsing one on the event loop stalls every other request on the replica
    (architecture rule 13).
    """
    if sv.state_size == 0:
        return None
    try:
        data = await get_storage().get(state_key(str(sv.workspace_id), str(sv.id)))
    except Exception:
        logger.warning("state_document_blob_missing", state_version_id=str(sv.id))
        return None

    from terrapod.crypto.state import decrypt_state_bytes

    try:
        data = await decrypt_state_bytes(data)
    except Exception:
        logger.warning("state_document_undecryptable", state_version_id=str(sv.id))
        return None

    def _parse(raw: bytes) -> dict:
        import json

        return json.loads(raw)

    try:
        document = await asyncio.to_thread(_parse, data)
    except (ValueError, TypeError):
        logger.warning("state_document_parse_failed", state_version_id=str(sv.id))
        return None
    return document if isinstance(document, dict) else None


async def derive_state_outputs(
    db: AsyncSession,
    user: AuthenticatedUser,
    workspace_id: str,
) -> dict:
    """A workspace's current state outputs, whatever engine wrote them (#1568).

    Sensitive values are reported as present but not revealed, which is the same
    bargain Terrapod strikes everywhere else with a sensitive value: the reader
    learns the output exists without the API becoming a way to read secrets that
    `state:read` was not meant to hand over.

    Empty — never an error — for a workspace with no state yet, an unreadable
    blob, or a state that simply declares no outputs. A view that 500s because
    nothing has been applied yet would be worse than one that says "none".
    """
    from terrapod.api.routers.workspace_extensions import _native_workspace

    ws: Workspace = await _native_workspace(workspace_id, db)

    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.STATE_READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires state:read permission on workspace",
        )

    target = (
        (
            await db.execute(
                select(StateVersion)
                .where(StateVersion.workspace_id == ws.id)
                .order_by(StateVersion.serial.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if target is None or target.state_size == 0:
        return {"outputs": {}}

    try:
        data = await get_storage().get(state_key(str(target.workspace_id), str(target.id)))
    except Exception:
        logger.warning("state_outputs_blob_missing", state_version_id=str(target.id))
        return {"outputs": {}}

    from terrapod.crypto.state import decrypt_state_bytes

    data = await decrypt_state_bytes(data)
    is_pulumi = (ws.engine or "").strip().lower() == PULUMI_ENGINE

    def _parse(raw: bytes) -> dict:
        import json

        document = json.loads(raw)
        return stack_outputs(document) if is_pulumi else terraform_outputs(document)

    try:
        outputs = await asyncio.to_thread(_parse, data)
    except (ValueError, TypeError):
        logger.warning("state_outputs_parse_failed", state_version_id=str(target.id))
        return {"outputs": {}}
    return {"outputs": outputs, "state_version": f"sv-{target.id}", "serial": target.serial}
