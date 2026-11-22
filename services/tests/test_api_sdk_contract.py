"""Source-introspection enforcement for the API ↔ SDK ↔ provider contract.

The "Adding a new attribute → add to go-terrapod + provider in the same PR"
rule in CLAUDE.md was historically guidance, not enforced. Twice in 24h we
shipped server attributes that were never wired through (`trigger-prefixes`
since PR #152, `drift-latest-run-id` in v0.35.3). #480 reconciles the
workspace contract; this test exists so the next drift fails CI loudly the
moment it's introduced rather than years later when an operator hits a
missing field.

The test is **source-introspection**, not behavioural: it reads
`tfe_v2.py` to extract the API attribute keys emitted by
`_workspace_json`, then reads `go-terrapod/workspaces.go` and the
provider's workspace resource + datasource to extract their attribute
sets, and asserts equivalence. No process is started; no HTTP is
involved. The cost is a `Path.read_text()` per file and a regex pass.

When this test fails, the message names the missing attribute(s) and
points at the file the contributor needs to edit. The right fix is
nearly always "extend the SDK/provider to surface the attribute", not
"add the attribute to the allowlist below". The allowlist is reserved
for attributes that are intentionally API-side-only (computed flags
emitted for cloud-block compatibility, per-request permission blocks,
nested relationships, UI-only health rollups).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

# Locate the API + SDK + provider source trees. The repo lays them out
# under `services/`, `go-terrapod/`, `provider/`, but the Docker test
# image flattens to `/app/terrapod`, `/app/go-terrapod`,
# `/app/provider`. This helper tries each candidate path until it
# finds a file that exists.
_TESTS_DIR = Path(__file__).resolve().parent

_API_TFE_V2 = next(
    p
    for p in (
        _TESTS_DIR.parent.parent / "services/terrapod/api/routers/tfe_v2.py",  # local
        _TESTS_DIR.parent / "terrapod/api/routers/tfe_v2.py",  # docker
    )
    if p.exists()
)
_SDK_WORKSPACES = next(
    p
    for p in (
        _TESTS_DIR.parent.parent / "go-terrapod/workspaces.go",  # local
        _TESTS_DIR.parent / "go-terrapod/workspaces.go",  # docker
    )
    if p.exists()
)
_PROVIDER_WS_MODEL = next(
    p
    for p in (
        _TESTS_DIR.parent.parent / "provider/internal/resources/workspace/model.go",
        _TESTS_DIR.parent / "provider/internal/resources/workspace/model.go",
    )
    if p.exists()
)
_PROVIDER_WS_DATASOURCE = next(
    p
    for p in (
        _TESTS_DIR.parent.parent / "provider/internal/datasources/workspace/data_source.go",
        _TESTS_DIR.parent / "provider/internal/datasources/workspace/data_source.go",
    )
    if p.exists()
)

# Attributes that appear in the API workspace serializer but are
# intentionally NOT mirrored in the SDK Workspace struct or the
# provider's workspace resource. Each entry should be documented so
# future contributors don't need to re-discover why.
WORKSPACE_API_ONLY: frozenset[str] = frozenset(
    {
        # Derived from `execution_mode` ("agent" → True). Cosmetic flag
        # for the TFE cloud-backend handshake. Recomputable by clients.
        "operations",
        # Cloud-block tag compat — generated from `labels` to satisfy
        # OpenTofu's `cloud { workspaces { tags = [...] } }` block. Not
        # a user-settable attribute; the SDK gets it via `Labels`.
        "tag-names",
        # JSON object — per-request permission rollup computed from the
        # caller's effective role on the workspace. Out of scope for a
        # storage-shaped SDK struct.
        "permissions",
        # Per-request action gating; same rationale as `permissions`.
        "actions",
        # Per-request health rollup the UI consumes — derived from
        # state-diverged / vcs-last-error / drift-status, all of which
        # ARE in the SDK individually.
        "health-conditions",
        # The SDK reads it (`Workspace.LatestRun`); the provider does not,
        # since run state is not configuration.
        "latest-run",
        # TFE-surface only, and DERIVED: `resolve_auto_apply_mode(ws) !=
        # "never"`. The stored column is gone (#2503) -- `auto_apply_mode` is
        # the setting, and that IS mirrored in the SDK and both provider
        # surfaces. This stays on `/api/tfe/v2` because `go-tfe` and the cloud
        # backend read it, and it is popped on the native door; mirroring it
        # would hand a practitioner a second way to express one setting, which
        # is the whole thing #2503 removed.
        "auto-apply",
        # Not in the SDK or provider yet: those consumers follow in the consumers slice.
        "storage-location",
    }
)


def _either_layout(*candidates: str) -> Path:
    """The first candidate that exists, so one path works locally and in the test image.

    `docker/Dockerfile.test` flattens `services/` into `/app`, so the test tree
    sits one directory shallower there than it does here. Counting `parents[N]`
    is therefore right in at most one of the two layouts.
    """
    for rel in candidates:
        for base in (_TESTS_DIR.parent.parent, _TESTS_DIR.parent):
            p = base / rel
            if p.exists():
                return p
    raise AssertionError(f"none of {candidates} resolves in either layout")


#: The SDK file holding `RunActions`, through `_either_layout` rather than a
#: second copy of the same idiom -- its siblings above predate that helper.
_SDK_RUNS = _either_layout("go-terrapod/runs.go")


#: Every consumer file that would carry `storage-location` once it is mirrored,
#: with the surface it belongs to. Both surfaces emit the attribute: the
#: workspace serializer and the autodiscovery rule's.
_STORAGE_LOCATION_CONSUMERS: tuple[tuple[str, str], ...] = (
    ("go-terrapod/workspaces.go", "SDK workspace"),
    ("go-terrapod/autodiscovery_rules.go", "SDK autodiscovery rule"),
    ("provider/internal/resources/workspace/model.go", "provider workspace"),
    ("provider/internal/resources/autodiscovery_rule/resource.go", "provider autodiscovery rule"),
    ("mcp/internal/mcpserver/crud.go", "MCP workspace"),
    ("mcp/internal/mcpserver/autodiscovery.go", "MCP autodiscovery rule"),
)


class TestTheStorageLocationExemptionExpires:
    """`storage-location` is exempted because no consumer carries it YET.

    That is a reason with an end, and nothing was enforcing the end: the entry
    would have outlived it silently, and an exemption whose reason has expired
    reads as a decision rather than as a gap -- which is the one state that stops
    anyone looking (#2623).

    So this asserts the reason rather than the entry, in both directions. It also
    covers the autodiscovery rule, which emits and accepts the same attribute and
    had no SDK/provider/MCP gate of any kind, because the contract test above
    reads the workspace serializer only.
    """

    @staticmethod
    def _carried() -> list[str]:
        """The surfaces that now carry the field, by reading their source.

        Comment lines are dropped first. A file's own comment explaining why it
        does NOT carry the attribute would otherwise register as carrying it,
        which is the failure direction that matters here: it would retire a live
        exemption.
        """
        carried = []
        for rel, surface in _STORAGE_LOCATION_CONSUMERS:
            text = _either_layout(rel).read_text()
            code = "\n".join(
                line for line in text.split("\n") if not line.lstrip().startswith("//")
            )
            lowered = code.lower().replace("-", "_")
            if "storagelocation" in lowered or "storage_location" in lowered:
                carried.append(surface)
        return carried

    def test_the_exemption_stands_only_while_no_consumer_carries_it(self):
        carried = self._carried()
        if carried:
            assert "storage-location" not in WORKSPACE_API_ONLY, (
                "the exemption's reason -- that no consumer carries this yet -- has "
                "expired: " + ", ".join(carried) + " now does. Remove the entry from "
                "WORKSPACE_API_ONLY and let the contract test above check the mirroring."
            )
        else:
            assert "storage-location" in WORKSPACE_API_ONLY, (
                "no consumer carries `storage-location`, so the contract test needs "
                "the exemption back -- removing it without mirroring the attribute "
                "makes this suite fail for the right reason in the wrong place."
            )

    def test_the_matcher_sees_the_field_it_looks_for(self):
        """The gate's own fixtures, because a count over real files cannot show
        that the matcher still matches -- a narrowed one would simply report
        nothing carried, which is the state that keeps the exemption alive."""
        import re as _re

        samples = {
            'StorageLocation string `json:"storage-location"`': True,
            'StorageLocation types.String `tfsdk:"storage_location"`': True,
            "Storage_Location string": True,
            "// storage-location is deliberately not mirrored here": False,
            "    // StorageLocation: see the API-only exemption": False,
            'Name string `json:"name"`': False,
        }
        for text, expected in samples.items():
            code = "\n".join(
                line for line in text.split("\n") if not line.lstrip().startswith("//")
            )
            lowered = code.lower().replace("-", "_")
            got = "storagelocation" in lowered or "storage_location" in lowered
            assert got is expected, f"matcher disagrees on {text!r}: got {got}"
        assert _re is not None  # keep the import honest if the samples shrink

    def test_every_named_consumer_file_resolves(self):
        """A path that stops resolving would make the gate report nothing carried,
        which is indistinguishable from the exemption being correct."""
        for rel, _surface in _STORAGE_LOCATION_CONSUMERS:
            assert _either_layout(rel).is_file(), rel
        assert len(_STORAGE_LOCATION_CONSUMERS) >= 6


def _extract_workspace_api_attrs() -> set[str]:
    """Return the set of attribute keys emitted by `_workspace_json`.

    Parses `tfe_v2.py` source as text rather than importing it
    (the importable module's `_workspace_json` builds a runtime dict,
    not a static one — and source parsing is exactly the level of
    coupling we want: this test fails when the literal source changes).
    """
    src = _API_TFE_V2.read_text()
    # Locate `def _workspace_json(` and consume until the next top-level def.
    m = re.search(r"^def _workspace_json\(.*?(?=^\ndef |^\nasync def )", src, re.M | re.S)
    if m is None:
        raise AssertionError("Could not locate `_workspace_json` in tfe_v2.py")
    body = m.group(0)
    # Pull the `"attributes": { ... }` block out so we don't pick up
    # `relationships` keys or unrelated dict literals defined later in
    # the function. Comment lines are allowed to sit between the closing
    # `},` and `"relationships"` (a comment is not API surface, so it must
    # not fail this gate — see #1063, which documents the house style there).
    attrs_match = re.search(
        r'"attributes":\s*\{(.*?)\n\s+\}\s*,\s*(?:#[^\n]*\n\s*)*"relationships"', body, re.S
    )
    if attrs_match is None:
        raise AssertionError("Could not locate attributes block inside _workspace_json")
    attrs_block = attrs_match.group(1)
    # Only top-level keys. The attributes block is indented 12 spaces
    # (inside `"data": { ... "attributes": { ... } }`), so each direct
    # key sits at 16 spaces. Nested dicts (permissions, actions) push
    # their keys to 20 spaces and must be excluded — the contract test
    # is about top-level attribute names, not per-permission sub-keys.
    return set(re.findall(r'^ {16}"([a-z][a-z0-9-]+)":', attrs_block, re.M))


def _extract_sdk_workspace_tags() -> set[str]:
    """Return the set of `json:"..."` tags on the SDK's Workspace struct."""
    src = _SDK_WORKSPACES.read_text()
    # Slice out the Workspace struct body.
    m = re.search(r"type Workspace struct \{(.*?)^\}", src, re.M | re.S)
    if m is None:
        raise AssertionError("Could not locate `type Workspace struct` in workspaces.go")
    body = m.group(1)
    # `json:"<key>,omitempty"` or just `json:"<key>"` — strip trailing
    # ",omitempty" or whitespace.
    return {tag.split(",", 1)[0] for tag in re.findall(r'json:"([a-z][a-z0-9-]+)', body)}


def _extract_provider_workspace_tfsdk_tags(source_path: Path, type_name: str) -> set[str]:
    """Return `tfsdk:"..."` tags from a provider Go struct."""
    src = source_path.read_text()
    m = re.search(rf"type {type_name} struct \{{(.*?)^\}}", src, re.M | re.S)
    if m is None:
        raise AssertionError(f"Could not locate `type {type_name} struct` in {source_path}")
    body = m.group(1)
    return set(re.findall(r'tfsdk:"([a-z][a-z0-9_]+)"', body))


# Maps the provider's Terraform attribute names (underscored) to the
# corresponding API attribute names (hyphenated). For drift detection
# only — when the API has an attribute, both the SDK (json tag) AND
# the provider's resource/datasource (tfsdk tag, with `_`→`-`) need to
# expose it.
def _provider_to_api(tfsdk_tag: str) -> str:
    return tfsdk_tag.replace("_", "-")


def _provider_attrs_as_api(tfsdk_tags: set[str]) -> set[str]:
    return {_provider_to_api(t) for t in tfsdk_tags}


def _go_struct_tags(path: Path, struct: str) -> set[str]:
    """The `json:"..."` tags of one Go struct, `,omitempty` stripped.

    The sibling above is hardcoded to `Workspace`; this takes the struct, since
    the nested contract needs two more.
    """
    m = re.search(r"^type " + struct + r" struct \{(.*?)^\}", path.read_text(), re.M | re.S)
    if m is None:
        raise AssertionError(f"`type {struct} struct` not found in {path.name}")
    return {tag.split(",", 1)[0] for tag in re.findall(r'json:"([a-z][a-z0-9-]+)', m.group(1))}


def _latest_run_literal() -> ast.Dict:
    """The `latest_run_attr = {...}` dict in `tfe_v2.py`.

    By AST, and it REFUSES rather than guesses if the literal stops being one:
    a reader that quietly returned nothing would report the SDK complete.
    """
    tree = ast.parse(_API_TFE_V2.read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Dict)
            and any(isinstance(t, ast.Name) and t.id == "latest_run_attr" for t in node.targets)
        ):
            return node.value
    raise AssertionError("`latest_run_attr = {...}` not found as a dict literal in tfe_v2.py")


def _dict_keys(d: ast.Dict) -> set[str]:
    out = set()
    for k in d.keys:
        if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
            raise AssertionError(f"unreadable key: {ast.dump(k) if k is not None else '**spread'}")
        out.add(k.value)
    return out


#: `latest-run` sub-keys deliberately not on `WorkspaceLatestRun`, each with its reason.
LATEST_RUN_API_ONLY: frozenset[str] = frozenset()


class TestTheNestedLatestRunReachesTheSDK:
    """Every sub-key of `latest-run` is on `WorkspaceLatestRun`, or exempted.

    Nothing forced this, by construction rather than by oversight.
    `WORKSPACE_API_ONLY` exempts `latest-run` as a WHOLE, and
    `_extract_workspace_api_attrs` parses top-level keys only, so a nested
    sub-key fell outside both halves of the test that exists for exactly this
    contract. `surface.golden` is additive-only, so it stays green when a field
    is simply never added, and `wire_completeness_test.go` covers request
    structs rather than this one. The last attribute to go in was carried
    because the API-consumer contract says to carry it, not because anything
    would have failed (#2630).
    """

    def test_every_latest_run_sub_key_is_on_the_sdk_struct(self):
        missing = sorted(
            _dict_keys(_latest_run_literal())
            - _go_struct_tags(_SDK_WORKSPACES, "WorkspaceLatestRun")
            - LATEST_RUN_API_ONLY
        )
        assert missing == [], (
            "the workspace document's `latest-run` carries these and "
            "`WorkspaceLatestRun` does not: " + ", ".join(missing) + ". Add the field "
            "and its provider/MCP consumers, or exempt it in LATEST_RUN_API_ONLY with "
            "the reason."
        )

    def test_the_nested_actions_block_is_covered_too(self):
        """One level further, where the same blindness repeats: `actions` is an
        object inside an object, so neither reader above sees its keys either."""
        literal = _latest_run_literal()
        actions = next(
            (
                v
                for k, v in zip(literal.keys, literal.values, strict=True)
                if isinstance(k, ast.Constant) and k.value == "actions" and isinstance(v, ast.Dict)
            ),
            None,
        )
        assert actions is not None, "`actions` inside latest_run_attr is no longer a dict literal"
        missing = sorted(_dict_keys(actions) - _go_struct_tags(_SDK_RUNS, "RunActions"))
        assert missing == [], f"`RunActions` is missing: {missing}"

    def test_both_readers_see_something(self):
        """Floors. A reader that stops matching reports nothing missing, which is
        indistinguishable from the SDK being complete -- the direction that lets
        a carry be forgotten, which is the whole defect."""
        assert len(_dict_keys(_latest_run_literal())) >= 7
        assert len(_go_struct_tags(_SDK_WORKSPACES, "WorkspaceLatestRun")) >= 7
        assert len(_go_struct_tags(_SDK_RUNS, "RunActions")) == 4

    def test_the_tag_reader_strips_omitempty_and_stays_inside_its_struct(self):
        """Its own fixtures: `phase-started-at` carries `,omitempty`, so a reader
        keeping the suffix would report it missing and invite a spurious
        exemption -- a gate inventing work is as bad as one missing it."""
        tags = _go_struct_tags(_SDK_WORKSPACES, "WorkspaceLatestRun")
        assert "phase-started-at" in tags
        assert not any("," in t for t in tags)
        assert "name" not in tags, "the reader escaped WorkspaceLatestRun into a sibling struct"


class TestWorkspaceContract:
    """Pin the Workspace attribute contract end-to-end."""

    def test_api_attrs_present_in_sdk(self):
        """Every API attribute must be in the SDK Workspace struct.

        Failure means the API serializer emits an attribute the Go SDK
        silently drops on read — exactly the failure mode that left
        `trigger-prefixes` invisible to terraform-provider-terrapod for
        years. To fix: add the field + json tag to `Workspace` struct
        and the matching `GetXxxAttr(res, "<api-key>")` call in
        `workspaceFromResource` in go-terrapod/workspaces.go.
        """
        api = _extract_workspace_api_attrs()
        sdk = _extract_sdk_workspace_tags()
        missing = api - sdk - WORKSPACE_API_ONLY
        assert not missing, (
            f"Workspace API attributes missing from go-terrapod SDK: {sorted(missing)}. "
            "Either add them to `Workspace` + `workspaceFromResource` in "
            "go-terrapod/workspaces.go, or — if intentional — extend "
            "WORKSPACE_API_ONLY in this test with a comment explaining why."
        )

    def test_sdk_tags_present_in_api(self):
        """Reverse direction: an SDK field with no API attribute is dead weight.

        Allowlist is intentionally limited to internal-only fields like
        ID (relationship type) and synthetic helper fields the SDK adds.
        """
        api = _extract_workspace_api_attrs()
        sdk = _extract_sdk_workspace_tags()
        sdk_only_ok = frozenset({"id"})  # `id` is the resource identifier, not an attribute
        missing = sdk - api - sdk_only_ok
        assert not missing, (
            f"SDK Workspace fields with no matching API attribute: {sorted(missing)}. "
            "Either the API stopped emitting them (delete from the SDK) or "
            "they were never API attributes (review and add to the allowlist)."
        )

    def test_api_attrs_present_in_provider_resource(self):
        """API attributes must be surfaced on terrapod_workspace resource."""
        api = _extract_workspace_api_attrs()
        provider_tfsdk = _extract_provider_workspace_tfsdk_tags(
            _PROVIDER_WS_MODEL, "workspaceModel"
        )
        provider_api_form = _provider_attrs_as_api(provider_tfsdk)
        # The provider resource doesn't surface tag-names / health
        # conditions / permissions etc., same allowlist applies.
        # `remote_state_consumers` is a provider-specific concept managed
        # via a separate API endpoint, not part of the workspace body.
        provider_only_ok = frozenset({"remote-state-consumers"})
        missing = api - provider_api_form - WORKSPACE_API_ONLY
        assert not missing - provider_only_ok, (
            f"Workspace API attributes missing from terrapod_workspace resource: "
            f"{sorted(missing - provider_only_ok)}. Add them to workspaceModel "
            "+ schema.Attributes + readWorkspaceIntoModel in "
            "provider/internal/resources/workspace/."
        )

    def test_api_attrs_present_in_provider_datasource(self):
        """Same parity check for the data source — it should expose everything
        the resource does so users can read state without managing it."""
        api = _extract_workspace_api_attrs()
        provider_tfsdk = _extract_provider_workspace_tfsdk_tags(
            _PROVIDER_WS_DATASOURCE, "workspaceDataSourceModel"
        )
        provider_api_form = _provider_attrs_as_api(provider_tfsdk)
        # Datasource intentionally omits a few writable-only / nested
        # fields: `auto_merge_strategy` is paired with auto-merge but
        # rarely consulted on read; ai-summary-mode/context are
        # writable knobs documented in the resource.
        ds_only_ok = frozenset(
            {
                "auto-merge",
                "auto-merge-strategy",
                "vcs-workflow",
                "ai-summary-mode",
                "ai-summary-context",
                "resource-cpu",
                "resource-memory",
            }
        )
        missing = api - provider_api_form - WORKSPACE_API_ONLY - ds_only_ok
        assert not missing, (
            f"Workspace API attributes missing from terrapod_workspace data source: "
            f"{sorted(missing)}. Add to workspaceDataSourceModel + Schema + "
            "readDataSourceModel in provider/internal/datasources/workspace/."
        )
