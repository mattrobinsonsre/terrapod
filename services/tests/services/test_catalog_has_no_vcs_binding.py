"""A catalog-provisioned workspace must carry NO VCS binding (#2019).

This is a guard for a hazard that is currently closed only by an absence, which
is the kind that gets reopened by accident.

**The measurement.** `vcs_poller` selects the workspaces it polls purely on
`Workspace.vcs_connection_id`, with no catalog awareness anywhere in the module.
So if a catalog-managed workspace ever acquired a binding, the poller would
build configuration versions for it from that repository -- over the catalog's
own generated wrapper. And the router-level refusal would not stand in the way:
`config_versions.py` refuses an operator-supplied CV on a catalog workspace, but
the poller reaches the service directly.

**Why this matters now.** #2019 gives a catalog item an Ansible half, and the
obvious way to hand it a playbook is a repository. That reading was considered
and rejected for exactly this reason: the subtree is COPIED out of the module
into the generated configuration version instead. A future change that reaches
for a binding again should fail here, beside the reason, rather than be found by
a provisioned workspace's configuration being silently replaced.

These are source-level and derived, because the property is about what the
provisioning path CAN do rather than about one scenario: a behavioural test with
a fixture that happens not to set a binding proves nothing about the next one.
"""

from __future__ import annotations

import ast
import pathlib

from terrapod.services import catalog_service, vcs_poller


def _assignments_to(src: str, attribute: str) -> list[str]:
    """Every `<something>.<attribute> = ...` and `<attribute>=...` in the source."""
    tree = ast.parse(src)
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == attribute:
                    hits.append(f"line {node.lineno}: attribute assignment")
        elif isinstance(node, ast.keyword) and node.arg == attribute:
            hits.append(f"line {getattr(node.value, 'lineno', '?')}: keyword argument")
    return hits


class TestCatalogProvisioningNeverBindsAVCSConnection:
    def test_the_provisioning_service_assigns_no_vcs_connection(self) -> None:
        src = pathlib.Path(catalog_service.__file__).read_text(encoding="utf-8")
        hits = _assignments_to(src, "vcs_connection_id")
        assert not hits, (
            "catalog_service now sets `vcs_connection_id`. The VCS poller selects on "
            "that column alone with no catalog filter, so a catalog workspace with a "
            "binding would have configuration versions built for it from the "
            "repository, replacing the catalog's generated wrapper. If this is "
            "deliberate, the poller has to learn about catalog workspaces FIRST.\n  "
            + "\n  ".join(hits)
        )

    def test_the_poller_still_has_no_catalog_filter(self) -> None:
        """The other half of the same property, asserted so the two cannot drift.

        If somebody DOES teach the poller about catalog workspaces, this fails
        and the test above can be relaxed in the same change -- which is the
        order that keeps the pair honest. A guard on only one side would let the
        binding be added here while the poller was still blind to it.
        """
        src = pathlib.Path(vcs_poller.__file__).read_text(encoding="utf-8")
        assert "catalog_item_id" not in src, (
            "vcs_poller now mentions `catalog_item_id`, so it may have learned "
            "about catalog-managed workspaces. If it now excludes them (or handles "
            "them deliberately), relax `test_the_provisioning_service_assigns_no_"
            "vcs_connection` in the same change and record why here."
        )

    def test_the_guard_can_actually_fire(self) -> None:
        """The walker finds both shapes it claims to.

        Without this the two tests above pass on a walker that matches nothing,
        which is indistinguishable from the property holding.
        """
        assert _assignments_to("ws.vcs_connection_id = None", "vcs_connection_id")
        assert _assignments_to("Workspace(vcs_connection_id=x)", "vcs_connection_id")
        assert not _assignments_to("ws.name = 'x'", "vcs_connection_id")
