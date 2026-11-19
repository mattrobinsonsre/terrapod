"""Sensitive inventory values do not leave on the read surface (GHSA-23jf-9q8w-5f37).

Reported by a third party and correct: `_var_json` masks a sensitive variable on
the per-variable routes, and the RESOLVED view did not -- while being gated on
`INVENTORY_READ`, which is in the READ preset. So a read-tier principal could
read `ansible_become_password` by asking for the resolved inventory instead of
the variable.

The asymmetry is the recurring shape here: one route masks, its sibling does
not, and nothing holds the two together. So the gate at the bottom of this file
asserts the property of the ROUTE rather than of the helper.
"""

from __future__ import annotations

import ast
import pathlib

from terrapod.api.routers import inventory as inventory_router
from terrapod.services import inventory_masking as m


class TestTheMask:
    def test_the_mask_matches_the_per_variable_route(self) -> None:
        """Two spellings of a mask is how one of them stops being recognised."""
        assert m.MASK == inventory_router._MASKED


class TestMaskHosts:
    def test_a_sensitive_key_is_replaced_and_its_neighbours_are_not(self) -> None:
        out = m.mask_hosts({"web1": {"pw": "S3cret!", "port": "22"}}, {"pw"})
        assert out["web1"]["pw"] == m.MASK
        assert out["web1"]["port"] == "22", "masking must not touch other keys"

    def test_the_same_key_is_masked_on_every_host(self) -> None:
        """Masking is by key across the workspace: a value merged from several
        levels has no single sensitive flag, so over-masking is the safe way to
        be wrong."""
        out = m.mask_hosts({"a": {"pw": "x"}, "b": {"pw": "y"}}, {"pw"})
        assert out["a"]["pw"] == out["b"]["pw"] == m.MASK

    def test_no_sensitive_keys_is_a_passthrough(self) -> None:
        hosts = {"web1": {"port": "22"}}
        assert m.mask_hosts(hosts, set()) == hosts

    def test_a_host_with_no_vars_does_not_raise(self) -> None:
        assert m.mask_hosts({"web1": None}, {"pw"}) == {"web1": {}}


class TestMaskProvenance:
    def _doc(self) -> dict:
        return {
            "web1": {
                "pw": {
                    "value": "S3cret!",
                    "chain": [
                        {"level": "all", "value": "S3cret!"},
                        {"level": "web", "value": "S3cret!"},
                    ],
                    "origin": "web",
                },
                "port": {
                    "value": "22",
                    "chain": [{"level": "all", "value": "22"}],
                    "origin": "all",
                },
            }
        }

    def test_the_effective_value_is_masked(self) -> None:
        out = m.mask_provenance(self._doc(), {"pw"})
        assert out["web1"]["pw"]["value"] == m.MASK

    def test_every_level_of_the_chain_is_masked(self) -> None:
        """Masking only the effective value leaves the secret in the chain
        beside it -- the same disclosure, one field along."""
        out = m.mask_provenance(self._doc(), {"pw"})
        values = [e["value"] for e in out["web1"]["pw"]["chain"]]
        assert values == [m.MASK, m.MASK]
        assert "S3cret!" not in str(out), "the secret survived somewhere in the document"

    def test_a_non_sensitive_key_is_untouched(self) -> None:
        out = m.mask_provenance(self._doc(), {"pw"})
        assert out["web1"]["port"]["value"] == "22"
        assert out["web1"]["port"]["chain"][0]["value"] == "22"

    def test_origin_survives_masking(self) -> None:
        """Which level won is not a secret, and an operator needs it to find the
        declaration they have to change."""
        out = m.mask_provenance(self._doc(), {"pw"})
        assert out["web1"]["pw"]["origin"] == "web"


class TestTheRouteAppliesIt:
    """A helper that masks correctly and is never called is the defect intact.

    Source-introspection because the alternative is a full resolved-inventory
    fixture, and the property worth pinning is that the route does not serialise
    an unmasked document -- which is a property of the code, not of one input.
    """

    def _source(self) -> str:
        return pathlib.Path(inventory_router.__file__).read_text()

    def test_the_resolved_route_masks_its_hosts(self) -> None:
        src = self._source()
        tree = ast.parse(src)
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "show_resolved_inventory"
        )
        body = ast.get_source_segment(src, fn) or ""
        assert "mask_hosts(" in body, (
            "show_resolved_inventory must mask host vars -- it is gated on "
            "INVENTORY_READ, which is a READ-tier capability (GHSA-23jf-9q8w-5f37)."
        )
        assert "mask_provenance(" in body, (
            "the provenance document carries the same values at every level and must be masked too."
        )

    def test_it_does_not_hand_the_raw_resolution_straight_out(self) -> None:
        """The pre-fix shape, named so it cannot come back silently."""
        src = self._source()
        assert 'hosts = resolved["hosts"]' not in src, (
            "the raw resolved hosts are being used directly; mask them first."
        )


class TestTheRunnerPathIsNotMasked:
    """The other half of the design, and the one that would break configure.

    A configure run reads the stored artifact through `run_artifacts`, and it
    needs the REAL values -- masking the shared resolver would set every managed
    host's password to `***`. So the masking helpers must not appear there.
    """

    def test_the_artifact_writer_does_not_mask(self) -> None:
        from terrapod.services import inventory_resolve

        src = pathlib.Path(inventory_resolve.__file__).read_text()
        for helper in ("mask_hosts", "mask_provenance"):
            assert helper not in src, (
                f"{helper} reached the shared resolver. A configure run reads its "
                "inventory from there and needs real values; masking belongs on "
                "the HTTP read surface only."
            )

    def test_the_run_artifact_route_does_not_mask(self) -> None:
        from terrapod.api.routers import run_artifacts

        src = pathlib.Path(run_artifacts.__file__).read_text()
        for helper in ("mask_hosts", "mask_provenance"):
            assert helper not in src, f"{helper} reached the runner's download route"
