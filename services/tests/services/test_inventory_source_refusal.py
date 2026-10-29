"""A failed parse says WHICH sources were read and what chose them (#2265).

**The behaviour under test is a message, and the reason that is worth a test
file is that the message is all an operator gets.** `ansible-inventory` is
driven by a platform that chose its `-i` arguments on the operator's behalf, so
its own parser error names no file, does not say the config directory is being
read as an inventory source at all, and offers no thread to pull:

    YAML inventory has invalid structure, it should be a dictionary, got:
    <class 'ansible.module_utils._internal._datatag._AnsibleTaggedList'>

That arrives as a 503 on a workspace somebody has just created, from the layout
that needs no configuration at all -- a playbook beside an inventory under
`config_directory`, with no `inventory-paths` and no `ansible.cfg`.

**And ansible's text is version-dependent, which is the sharpest argument for a
lead of our own.** Measured: 2.18.3 says `Failed to parse <abs path> with yaml
plugin` and names the file; the 2.21 line that produced #2265 says `Failed to
parse inventory with 'yaml' plugin` and names nothing. So nothing may be
asserted about whether the DETAIL identifies the source -- the assertions below
split on `ansible said:` and hold our own half to account.

**Driven through `_resolve_uncached`, never through `run_ansible_inventory`.**
`provenance` is a new optional parameter, so calling the subprocess helper
directly with one tests a path production might not take; only the real caller
proves the tier is threaded. Everything below the fetch is real, including the
ansible subprocess, because the message quotes ansible's own text and a fake
cannot produce it.

The second test is the one that earns its keep: it takes the remedy the message
gives and checks it WORKS. An error that recommends something untrue is worse
than one that recommends nothing.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from terrapod.services import inventory_resolve as ir


def _working_ansible() -> str | None:
    """A usable `ansible-inventory`, or None.

    Probed rather than merely located, for the reason
    `test_inventory_resolve.py` records at length: a stale shim whose `ansible`
    module is gone dies with `ModuleNotFoundError`, which looks like the code
    under test and is not. Skipping on a developer's machine is right because
    the test image gates its own install at BUILD time.
    """
    found = shutil.which("ansible-inventory")
    if found is None:
        return None
    try:
        probe = subprocess.run(  # noqa: S603 -- the path came from `which`
            [found, "--version"], capture_output=True, timeout=30, check=False
        )
    except OSError:
        return None
    return found if probe.returncode == 0 else None


_ANSIBLE = _working_ansible()
needs_ansible = pytest.mark.skipif(_ANSIBLE is None, reason="no working ansible-inventory")


#: `(the token the refusal uses, the ansible.cfg to commit, the workspace's
#: inventory_paths)` -- one row per remedy the message offers, applied to the
#: same fixture repository so the only variable is the advice itself.
#:
#: `ignore_patterns` is FIRST because it is the narrowest: it excludes the one
#: file and leaves the default tier -- the one that needs no configuration at
#: all -- doing its job. The values are REGEXES, which is the thing that bites.
_REMEDIES = (
    ("ignore_patterns", "[inventory]\nignore_patterns = site\\.yml\n", []),
    ("`inventory` key", "[defaults]\ninventory = hosts.yml\n", []),
    ("inventory-paths", "", ["hosts.yml"]),
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The layout that produced this live: a playbook BESIDE the inventory.

    `site.yml` is a LIST, so it is not an inventory -- and under the default
    tier the whole config directory is one source, which means ansible reads it
    as one and `any_unparsed_is_failed` turns that into a hard failure. Both
    halves of that are deliberate and stay; only the message was the defect.
    """
    cfg = tmp_path / "repo" / "ansible"
    cfg.mkdir(parents=True)
    (cfg / "site.yml").write_text("- hosts: all\n  tasks: []\n")
    (cfg / "hosts.yml").write_text("all:\n  hosts:\n    web1: {}\n")
    return cfg


@pytest.fixture
def driven(monkeypatch, repo: Path):
    """`_resolve_uncached` with the fetch and the declared rows stubbed.

    Only those two: the tier choice, the provenance, the subprocess and the
    refusal are all the real thing, which is the whole point of driving the
    caller rather than the helper.
    """

    async def _fake_fetch(db, ws, git_sha, workdir):
        # A `_ScreenedRoot`, not a bare `Path`, and the distinction is load-bearing
        # rather than pedantic: `Path.root` EXISTS and is the string `"/"`, so a
        # fake returning a path makes `screened.root` silently `"/"` and the
        # failure arrives as `TypeError: unsupported operand type(s) for /: 'str'
        # and 'str'` from inside the tier chooser, naming nothing about the fake.
        return ir._ScreenedRoot(root=repo, skipped=[], executables=set())

    async def _fake_declared(db, workspace_id):
        return {
            "hosts": [],
            "groups": [],
            "memberships": [],
            "children": [],
            "global_vars": {},
        }

    monkeypatch.setattr(ir, "_fetch_vcs_source", _fake_fetch)
    monkeypatch.setattr(ir, "_declared_document", _fake_declared)

    async def _drive(inventory_paths: list[str]):
        ws = SimpleNamespace(
            inventory_paths=inventory_paths,
            config_directory="ansible",
            # Both switches off, which is the default and the only state this
            # file's subject -- a refusal message -- is about (#2033).
            dynamic_inventory_enabled=False,
            inventory_scripts_enabled=False,
        )
        return await ir._resolve_uncached(None, uuid.uuid4(), ws=ws, git_sha="deadbeef", limit=None)

    return _drive


class TestTheDefaultTierExplainsItself:
    """The tier that needs no configuration was the one failing illegibly."""

    @needs_ansible
    @pytest.mark.asyncio
    async def test_it_names_the_source_the_tier_and_the_remedy(self, driven):
        with pytest.raises(ir.InventoryResolutionFailed) as exc:
            await driven([])
        message = str(exc.value)

        # OUR lead, not ansible's detail: the source as the operator would
        # recognise it in their repository, and no absolute scratch path, which
        # would leak a tempdir and name nothing they can find.
        lead = message.split("ansible said:")[0]
        assert "'ansible'" in lead
        assert "/tmp" not in lead and "/var/folders" not in lead

        # What chose it, and what to do about it.
        assert "config directory itself" in message
        assert "inventory-paths" in message
        assert "ansible.cfg" in message

        # ansible's own text survives as the DETAIL rather than as the whole
        # message, so nothing an operator used to see is lost.
        assert "ansible said:" in message
        assert "invalid structure" in message

    @needs_ansible
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("token", "cfg", "paths"), _REMEDIES, ids=[r[0] for r in _REMEDIES])
    async def test_every_remedy_it_names_actually_works(self, driven, repo, token, cfg, paths):
        """Take the message's own advice, each way it offers, and it resolves.

        This is what separates a message that helps from one that merely sounds
        helpful, and it is cheap: the fixture is unchanged, so the ONLY
        difference is the thing the refusal told the operator to do.

        It also caught a real error in the first draft, which named two remedies
        and missed the one `docs/ansible-inventory.md` calls THE control for this
        case -- `ignore_patterns`, which is the narrowest of the three because it
        excludes the one file and leaves the no-configuration-needed default
        intact. Probing it is what established the ini section and that the
        values are regexes rather than globs; a `[defaults] ignore_patterns`
        written as a glob is accepted silently and does nothing.
        """
        if cfg:
            (repo / "ansible.cfg").write_text(cfg)
        document = await driven(paths)
        assert "web1" in document["hosts"], document

    @needs_ansible
    @pytest.mark.asyncio
    async def test_the_message_names_no_remedy_this_file_leaves_untested(self, driven):
        """Ties the list above to the message, in the one direction that is checkable.

        A remedy added to the refusal without a test is the failure this
        prevents: the message would recommend something nobody has run. What it
        CANNOT catch, and this is the honest limit, is a fourth remedy phrased
        without any of the tokens below -- the tie is by identifier, so a novel
        mechanism described only in prose slips past.
        """
        with pytest.raises(ir.InventoryResolutionFailed) as exc:
            await driven([])
        remedy = str(exc.value).split("Every file in that directory")[-1]

        tested = {token for token, _, _ in _REMEDIES}
        for token in ("ignore_patterns", "inventory-paths", "`inventory` key"):
            if token in remedy:
                assert token in tested, (
                    f"the refusal recommends {token!r} and no case below exercises it"
                )
        # And every token we DO test is still named, so a remedy cannot be
        # quietly dropped from the message while its test goes on passing.
        for token in tested:
            assert token in remedy, f"{token!r} is tested but no longer recommended"


class TestAnExplicitTierIsNotLecturedAt:
    """Tiers 1 and 2 are a choice somebody made, so naming the sources is enough."""

    @needs_ansible
    @pytest.mark.asyncio
    async def test_a_declared_source_that_fails_names_itself_without_the_remedy(self, driven):
        with pytest.raises(ir.InventoryResolutionFailed) as exc:
            await driven(["site.yml"])
        message = str(exc.value)

        assert "'ansible/site.yml'" in message
        assert "declared on the workspace" in message
        # The config-root advice would be actively wrong here: `inventory-paths`
        # IS set, so telling them to set it is telling them to do what they did.
        assert "Every file in that directory" not in message


class TestTheAccountCoversEveryTier:
    """A tier with no account renders as a KeyError inside a refusal.

    Derived from the module rather than listed, because a hand-kept list is
    exactly what drifts: the failure would be an exception raised while
    building an error message, i.e. in the one place that must not fail.
    """

    def test_every_tier_the_chooser_can_return_has_an_account(self):
        import ast

        tree = ast.parse(Path(ir.__file__).read_text())
        returned: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "_InventorySources":
                continue
            # The tier is the second positional argument; a keyword form is
            # read too so a later refactor does not silently empty this.
            candidates = list(node.args[1:2]) + [
                kw.value for kw in node.keywords if kw.arg == "tier"
            ]
            for value in candidates:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    returned.add(value.value)
                elif isinstance(value, ast.IfExp):
                    for leaf in ast.walk(value):
                        if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str):
                            returned.add(leaf.value)
                elif isinstance(value, ast.Name):
                    # A variable: resolve the literals assigned to that name.
                    for assign in ast.walk(tree):
                        if isinstance(assign, ast.Assign) and any(
                            isinstance(t, ast.Name) and t.id == value.id for t in assign.targets
                        ):
                            for leaf in ast.walk(assign.value):
                                if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str):
                                    returned.add(leaf.value)

        # A floor, so a renamed constructor fails here rather than passing on
        # nothing -- the recorded shape where a derived gate reads zero sites
        # and reports success.
        assert len(returned) >= 3, f"only found {sorted(returned)} -- the walk is blind"
        missing = returned - set(ir._TIER_ACCOUNT)
        assert not missing, f"tiers with no account in _TIER_ACCOUNT: {sorted(missing)}"
