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
        # Nested relationship-y object describing the most recent run;
        # the SDK exposes runs as a separate concept rather than
        # embedding them on Workspace.
        "latest-run",
    }
)


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
    # the function.
    attrs_match = re.search(r'"attributes":\s*\{(.*?)\n\s+\}\s*,\s*"relationships"', body, re.S)
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
