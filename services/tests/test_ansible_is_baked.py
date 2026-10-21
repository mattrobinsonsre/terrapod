"""The API's ansible is BAKED INTO ITS IMAGE, and nothing fetches one at run time.

An earlier design made the API's ansible-core a per-workspace, Helm-defaulted,
run-time acquisition: it created a virtualenv under the PVC tempdir on first
use and `pip install ansible-core==<the workspace's version>`-ed into it, served
through an in-process PEP 503 shim over the PyPI pull-through cache. Three
things were wrong with it, and each is guarded below:

* **what the API ran depended on a workspace's pin.** Parsing a static
  inventory file is one stable operation, done once in the API, which then
  hands the runner a finalised document -- there is nothing for a workspace to
  choose. A workspace's `ansible_version` selects its execution-environment
  IMAGE, where the core line genuinely matters because it decides the supported
  target Pythons.
* **it needed to reach an index at run time**, so an inventory read could fail
  for a reason that had nothing to do with the inventory.
* **it layered ansible-specific machinery on a cache that exists for Pulumi's
  language SDKs.** That cache stays, and these tests assert it stays, so the
  guard cannot be satisfied by deleting it.

These are structural rather than behavioural on purpose: the regression is a
module coming back or a parameter reappearing, which no request can see. They
are AST-based rather than textual because the prose explaining the old design
still mentions `pip install ansible-core==` -- a text match would be satisfied
by a comment and defeated by one.
"""

from __future__ import annotations

import ast
import pathlib
import re
import tomllib

import pytest

_SERVICES = pathlib.Path(__file__).resolve().parents[1]
_TERRAPOD = _SERVICES / "terrapod"
_RESOLVE = _TERRAPOD / "services" / "inventory_resolve.py"

#: The modules the strip removed. Their return IS the regression.
_REMOVED = (
    _TERRAPOD / "services" / "api_ansible.py",
    _TERRAPOD / "runner" / "phases" / "ansible_env.py",
)


def _tree(path: pathlib.Path) -> ast.Module:
    assert path.exists(), f"{path} is missing, so this gate would pass vacuously"
    return ast.parse(path.read_text())


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"{name} not found — renamed? This gate pins its signature, so fix the gate."
    )


class TestNothingFetchesAnsible:
    def test_the_removed_modules_have_not_come_back(self):
        present = [str(p.relative_to(_SERVICES)) for p in _REMOVED if p.exists()]
        assert not present, (
            f"{present} is back. The API's ansible-core is pinned in pyproject.toml and "
            "installed at image build time; the runner's comes from its execution-"
            "environment image. Neither is fetched at run time, and a module that "
            "acquires one is the design this gate exists to keep out."
        )

    def test_the_binary_lookup_takes_no_arguments(self):
        """The structural guarantee that no per-workspace version gets back in.

        A `version` parameter is exactly how it got in last time: the function
        was `ansible_inventory_binary(version)` and keyed a staged venv by it.
        Zero parameters means there is nothing for a caller to pass.
        """
        fn = _function(_tree(_RESOLVE), "ansible_inventory_binary")
        args = fn.args
        taken = (
            [a.arg for a in args.posonlyargs]
            + [a.arg for a in args.args]
            + [a.arg for a in args.kwonlyargs]
        )
        assert taken == [], (
            f"ansible_inventory_binary now takes {taken}. It must take nothing: the "
            "binary is a property of the image, so there is no per-call choice to make."
        )

    def test_the_inventory_run_has_no_version_parameter(self):
        fn = _function(_tree(_RESOLVE), "run_ansible_inventory")
        args = fn.args
        taken = [a.arg for a in args.args + args.kwonlyargs]
        offenders = [a for a in taken if "version" in a]
        assert not offenders, (
            f"run_ansible_inventory takes {offenders}. Resolving an inventory does not "
            "depend on an ansible version — the image's ansible is the only one."
        )

    def test_inventory_resolution_imports_nothing_from_the_package_cache(self):
        """It is the pull-through cache's only ex-consumer on the ansible side.

        Checked over imports rather than text: the module's own comments
        describe the removed shim, so a textual gate would fire on the
        explanation and miss a lazily-imported re-addition.
        """
        bad: list[str] = []
        for node in ast.walk(_tree(_RESOLVE)):
            if isinstance(node, ast.ImportFrom) and node.module:
                if "package_cache" in node.module:
                    bad.append(f"from {node.module} import …")
            elif isinstance(node, ast.Import):
                bad += [a.name for a in node.names if "package_cache" in a.name]
        assert not bad, (
            f"inventory_resolve imports {bad}. The PyPI pull-through exists for Pulumi's "
            "language SDKs and for a collection's own Python dependencies; serving the "
            "API its own ansible through it is the design that was removed."
        )

    def test_no_setting_configures_an_ansible_version(self):
        """`api.config.default_ansible_version` is gone and must not return.

        What a workspace selects is an execution-environment image, so there is
        nothing deployment-wide to configure; the API's own copy is pinned in
        pyproject.toml, where a dependency belongs.
        """
        from terrapod.config import Settings

        offenders = sorted(
            f
            for f in Settings.model_fields
            if "ansible" in f and ("version" in f or "default" in f)
        )
        assert not offenders, (
            f"Settings declares {offenders}. The API's ansible-core is a build-time "
            "dependency, not an operator setting."
        )


class TestTheImageCarriesIt:
    def test_ansible_core_is_pinned_in_the_api_manifest(self):
        data = tomllib.loads((_SERVICES / "pyproject.toml").read_text())
        spec = data["tool"]["poetry"]["dependencies"].get("ansible-core")
        assert spec, (
            "ansible-core is not a dependency of services/pyproject.toml, so the API "
            "image carries no `ansible-inventory` and every inventory read 503s."
        )
        assert re.fullmatch(r"~\d+\.\d+", str(spec)), (
            f"ansible-core is pinned as {spec!r}. Pin the LINE (`~2.21`) so each rebuild "
            "takes that line's newest patch, as every sibling default is written."
        )

    @pytest.mark.parametrize("image", ["listener", "migrations", "runner"])
    def test_the_minimal_images_do_not_carry_it(self, image: str):
        """Only the API resolves an inventory, so only the API needs ansible.

        Pins the claim the strip made rather than leaving it to the lock diff:
        these three images resolve from their own manifests, so adding
        ansible-core to the main one must not reach them.
        """
        manifest = _SERVICES / f"pyproject-{image}.toml"
        assert manifest.exists(), f"{manifest} is missing — this gate would pass vacuously"
        data = tomllib.loads(manifest.read_text())
        deps = data.get("project", {}).get("dependencies", [])
        offenders = [d for d in deps if "ansible" in str(d)]
        assert not offenders, (
            f"pyproject-{image}.toml declares {offenders}. The {image} image resolves no "
            "inventory; the runner gets ansible from its execution-environment image."
        )

    def test_the_default_line_is_a_line(self):
        """`X.Y`, not `X.Y.Z` — and here that is load-bearing, not cosmetic.

        Terrapod publishes one execution-environment image per ansible-core
        line, tagged by the line, so a default carrying a patch names no image
        and would have to be reduced before it could select one. A line also
        keeps the fleet on that line's newest patch as the image is rebuilt.
        """
        from terrapod.db.models import DEFAULT_ANSIBLE_LINE

        assert re.fullmatch(r"\d+\.\d+", DEFAULT_ANSIBLE_LINE), (
            f"DEFAULT_ANSIBLE_LINE is {DEFAULT_ANSIBLE_LINE!r}. It names an "
            "execution-environment image tag, which is a line."
        )

    def test_the_pypi_pull_through_still_exists(self):
        """So the import gate above cannot be satisfied by deleting the cache.

        Pulumi Python programs resolve their closure through it, and a
        collection's own Python dependencies need it too — removing it to make
        a guard pass would break both.
        """
        cache = _TERRAPOD / "services" / "package_cache"
        for module in ("pypi.py", "substrate.py", "galaxy.py"):
            assert (cache / module).exists(), (
                f"{module} is gone. The ansible-specific machinery was removed from "
                "ON TOP of this cache; the cache itself serves Pulumi's language SDKs "
                "and Ansible Galaxy collections and must stay."
            )
