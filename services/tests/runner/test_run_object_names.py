"""Per-run Kubernetes object names cannot collide between runs (#2209).

The names were `run_id[:16]`, and a run id is a UUIDv7: the first thirteen
characters of the string are the millisecond timestamp plus the version nibble,
so the prefix carried exactly ONE random hex digit. One VCS event fanning out
to several workspaces creates the runs together, and one of them then died on
`jobs.batch "tprun-01a15e7a-1809-75-plan" already exists` -- owned by a
different run of the same millisecond.

Three things are pinned here, because the defect needs all three to stay fixed:
the slug separates same-millisecond ids, every name comes from the one
producer, and the names still fit Kubernetes' limit.
"""

from __future__ import annotations

import ast
import pathlib
import re
import uuid

import pytest

from terrapod.runner import listener as listener_mod
from terrapod.runner.job_template import run_object_slug

# The two run ids from the incident, which collided.
_COLLIDED = (
    "01a15e7a-1809-75b0-a959-bf19b9471c3c",
    "01a15e7a-1809-7584-b88d-6120c508ed69",
)

# Every module that names a per-run Kubernetes object.
_NAMING_MODULES = (
    "terrapod/runner/job_template.py",
    "terrapod/runner/listener.py",
    "terrapod/services/vault_lease_service.py",
)


def _services_root() -> pathlib.Path:
    """The `services/` directory, found from this file rather than the CWD."""
    return pathlib.Path(__file__).resolve().parents[2]


class TestTheSlugSeparatesRuns:
    def test_the_two_ids_that_actually_collided_no_longer_do(self):
        a, b = _COLLIDED
        # The precondition: the old derivation really did collide on these, so
        # this test is about the fix rather than about two unrelated ids.
        assert a[:16] == b[:16] == "01a15e7a-1809-75"
        assert run_object_slug(a) != run_object_slug(b)

    def test_ids_generated_in_one_millisecond_are_separated(self):
        """A real fan-out, not hand-written ids.

        `uuid7` is what the run id column uses, so generating a batch as fast
        as the interpreter allows reproduces the condition: a module PR
        creating one run per consuming workspace.
        """
        if not hasattr(uuid, "uuid7"):  # pragma: no cover - 3.14 has it
            pytest.skip("uuid7 needs Python 3.14")
        ids = [str(uuid.uuid7()) for _ in range(64)]
        # Prove the batch really is same-millisecond-dense, or the test is
        # asserting nothing about the defect.
        assert len({i[:16] for i in ids}) < len(ids), (
            "the batch was not dense enough to exercise the collision"
        )
        assert len({run_object_slug(i) for i in ids}) == len(ids)

    def test_the_slug_is_deterministic(self):
        """The Job and its three Secrets are named by separate calls."""
        a = _COLLIDED[0]
        assert run_object_slug(a) == run_object_slug(a)

    def test_the_slug_is_a_dns_label_safe_hex_string(self):
        slug = run_object_slug(_COLLIDED[0])
        assert re.fullmatch(r"[0-9a-f]{16}", slug), slug


class TestTheNamesStillFit:
    """Kubernetes caps a name at 63 characters, and a Job's name has to leave
    room for the suffix the controller appends to derive a pod name."""

    @pytest.mark.parametrize("phase", ["plan", "apply", "configure"])
    @pytest.mark.parametrize("suffix", ["", "-auth", "-vars", "-ca"])
    def test_every_name_this_produces_is_within_the_limit(self, phase, suffix):
        name = f"tprun-{run_object_slug(_COLLIDED[0])}-{phase}{suffix}"
        assert len(name) <= 63 - 6, name


class TestOneProducer:
    """The Job, its Secrets and the API's reconstruction must agree.

    A Secret named for a different run than its Job is a cross-run credential
    mount; the only reason that has never happened is that the listener creates
    the Job first, so a collision fails there before a Secret is written. That
    ordering is incidental, so the agreement is pinned structurally.
    """

    def test_every_naming_module_uses_the_shared_producer(self):
        for rel in _NAMING_MODULES:
            src = (_services_root() / rel).read_text()
            assert "run_object_slug" in src, (
                f"{rel} names a per-run object without the shared producer"
            )

    def test_no_naming_module_truncates_a_run_id(self):
        """`run_id[:16]` in any form is the defect itself.

        AST, not text. A text scan matches the prose that *describes* the
        defect -- the helper's own docstring says "Not `run_id[:16]`" -- and a
        gate whose findings are its own documentation is worse than no gate.
        An `ast.Subscript` carrying a slice cannot appear in a comment.
        """

        def is_the_bare_run_id(node: ast.AST) -> bool:
            """Whether this expression IS the run id, not merely derived from it.

            The distinction is the whole fix: slicing a digest OF the id is
            correct and slicing the id itself is the defect, so a predicate
            that walks the subtree flags `run_object_slug`'s own
            `sha256(run_id...).hexdigest()[:16]`. Unwrap one `str()` and
            require what is left to be the identifier itself.
            """
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "str" and node.args:
                node = node.args[0]
            if isinstance(node, ast.Name):
                return node.id == "run_id"
            if isinstance(node, ast.Attribute):
                return node.attr == "id"
            return False

        offenders = []
        for rel in _NAMING_MODULES:
            tree = ast.parse((_services_root() / rel).read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Subscript):
                    continue
                if not isinstance(node.slice, ast.Slice):
                    continue
                if is_the_bare_run_id(node.value):
                    offenders.append(f"{rel}:{node.lineno}")
        assert offenders == [], (
            "a run id is being truncated -- use run_object_slug():\n  " + "\n  ".join(offenders)
        )

    def test_the_listener_and_the_job_builder_share_one_function(self):
        """Not two functions that happen to agree today."""
        assert listener_mod.run_object_slug is run_object_slug

    def test_every_tprun_name_is_built_from_the_slug_variable(self):
        """Derived, with a floor: find the `tprun-` f-strings and require each
        to interpolate a name the module bound from `run_object_slug`."""
        sites = 0
        for rel in _NAMING_MODULES:
            path = _services_root() / rel
            src = path.read_text()
            tree = ast.parse(src)
            # Names assigned from the producer, plus a direct call inside the
            # f-string itself (which the vault fallback uses).
            bound = {
                t.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                for t in node.targets
                if isinstance(t, ast.Name)
                and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", "") == "run_object_slug"
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.JoinedStr):
                    continue
                literal = "".join(v.value for v in node.values if isinstance(v, ast.Constant))
                if "tprun-" not in literal:
                    continue
                sites += 1
                used = {
                    getattr(v.value, "id", "")
                    for v in node.values
                    if isinstance(v, ast.FormattedValue)
                } | {
                    getattr(getattr(v.value, "func", None), "id", "")
                    for v in node.values
                    if isinstance(v, ast.FormattedValue)
                }
                assert used & (bound | {"run_object_slug"}), (
                    f"{rel}: a `tprun-` name at line {node.lineno} is not built "
                    f"from the shared slug (interpolates {sorted(n for n in used if n)})"
                )
        # A matcher that stops matching must fail rather than pass on nothing.
        assert sites >= 5, f"expected every per-run object name, found {sites}"
