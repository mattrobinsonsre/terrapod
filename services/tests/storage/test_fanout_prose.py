"""Every surface describing the cross-location fan-out names the real prefixes.

`prefix_may_hold_workspace_keys` decides whether a listing reads one store or
all of them, and four operator-facing surfaces describe that rule in prose: the
Helm values comment, the values schema description, the `Settings` field
description an operator sees in their editor, and the warning logged when
locations are configured with nothing assigned to them.

All four said something looser than the code. The values comment said listings
"outside one workspace's prefix ... read all", which is wrong in the direction
that costs an operator money and nerves: it claims the fan-out covers `cache/`,
`registry/`, `oci/`, `policies/`, `uploads/`, `vcs_archives/` and
`module_overrides/`, none of which fan out at all (#2629).

A sentence listing prefixes has nothing keeping it in step with the tuple it
describes, so this derives the tuple from the source and holds each surface to
it. It is deliberately not a wording check -- the surfaces phrase it differently
for different readers -- only that whatever prefixes a surface names are exactly
the ones that fan out.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parents[1]


def _either_layout(rel: str) -> Path:
    """`docker/Dockerfile.test` flattens `services/` into `/app`, so the tree sits
    one directory shallower there; counting parents is right in at most one."""
    for base in (_TESTS_DIR.parent.parent, _TESTS_DIR.parent):
        p = base / rel
        if p.exists():
            return p
    raise AssertionError(f"{rel} resolves in neither layout")


def _fanning_prefixes() -> set[str]:
    """`WORKSPACE_KEY_PREFIXES`, read from the source as text.

    By AST rather than by import so this says nothing about what else
    `keys.py` happens to pull in, and refuses rather than guesses if the tuple
    stops being a literal.
    """
    src = _either_layout("terrapod/storage/keys.py")
    if not src.exists():
        src = _either_layout("services/terrapod/storage/keys.py")
    tree = ast.parse(src.read_text())
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if not any(isinstance(t, ast.Name) and t.id == "WORKSPACE_KEY_PREFIXES" for t in targets):
            continue
        out = set()
        for el in node.value.elts:  # type: ignore[union-attr]
            if isinstance(el, ast.Constant):
                out.add(el.value)
            elif isinstance(el, ast.Name):
                out.add(_named_constant(tree, el.id))
            else:
                raise AssertionError(f"unreadable entry in WORKSPACE_KEY_PREFIXES: {ast.dump(el)}")
        return out
    raise AssertionError("WORKSPACE_KEY_PREFIXES not found in keys.py")


def _named_constant(tree: ast.Module, name: str) -> str:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (
                    isinstance(t, ast.Name)
                    and t.id == name
                    and isinstance(node.value, ast.Constant)
                ):
                    return str(node.value.value)
    raise AssertionError(f"{name} is not a module-level string constant in keys.py")


#: The four surfaces, and how to get the prose out of each.
def _values_comment() -> str:
    text = _either_layout("helm/terrapod/values.yaml").read_text()
    m = re.search(r"\n([ \t]*#[^\n]*Named storage locations.*?)\n[ \t]*locations:", text, re.S)
    assert m, "the locations comment block moved in values.yaml"
    return m.group(1)


def _schema_description() -> str:
    d = json.loads(_either_layout("helm/terrapod/values.schema.json").read_text())
    return d["$defs"]["apiConfig"]["properties"]["storage"]["properties"]["locations"][
        "description"
    ]


def _settings_description() -> str:
    text = _either_layout("terrapod/config.py").read_text()
    m = re.search(
        r"locations: dict\[str, StorageLocationConfig\] = Field\((.*?)\n    \)", text, re.S
    )
    assert m, "the `locations` Field moved in config.py"
    return m.group(1)


def _warning_log() -> str:
    text = _either_layout("terrapod/storage/__init__.py").read_text()
    m = re.search(r'logger\.warning\(\s*(".*?")\s*,\s*locations=', text, re.S)
    assert m, "the no-assignment warning moved in storage/__init__.py"
    return m.group(1)


_SURFACES = {
    "helm values comment": _values_comment,
    "values.schema.json description": _schema_description,
    "Settings field description": _settings_description,
    "no-assignment warning": _warning_log,
}

#: Prefixes that exist and must never be named as fanning out. Derived would be
#: better and is not available: there is no enumeration of "every prefix", only
#: of the ones that fan out. These are the seven the review measured.
_NON_FANNING = (
    "cache/",
    "registry/",
    "oci/",
    "policies/",
    "uploads/",
    "vcs_archives/",
    "module_overrides/",
)


class TestTheFanOutProse:
    def test_the_tuple_is_readable_and_not_empty(self):
        prefixes = _fanning_prefixes()
        assert len(prefixes) >= 6, prefixes
        assert "state/" in prefixes and "plans/" in prefixes

    @staticmethod
    def _fanout_clause(prose: str) -> str:
        """The sentences that describe what DOES fan out.

        Scoped deliberately. A surface may name a non-fanning prefix -- saying
        `cache/` is read from the default store alone is the useful half of the
        correction -- so a check for the prefix ANYWHERE in the prose fires on
        correct text, which is the shape that teaches people to delete a gate.
        The claim is what matters, and it lives in the clause that says `fan`.
        """
        sentences = re.split(r"(?<=[.])\s+", prose.replace("\n", " "))
        return " ".join(s for s in sentences if "fan" in s.lower())

    def test_the_fan_out_clause_names_no_prefix_that_is_read_from_one_store(self):
        """The direction that was wrong, and the expensive one.

        Claiming a prefix fans out when it does not tells an operator a
        misconfigured bucket breaks something it cannot break, which is how a
        location gets configured defensively and wrongly.
        """
        offenders = []
        for label, read in _SURFACES.items():
            clause = self._fanout_clause(read())
            for p in _NON_FANNING:
                if p in clause:
                    offenders.append(f"{label} names {p}")
        assert offenders == [], (
            "these surfaces claim a prefix fans out across storage locations when "
            "`prefix_may_hold_workspace_keys` reads it from the default store alone: "
            + "; ".join(offenders)
        )

    def test_that_clause_finder_reads_the_claim_and_not_its_denial(self):
        """Its own fixtures: a narrowed clause finder would simply report nothing,
        which is indistinguishable from every surface being correct."""
        claims = "Only a listing whose prefix can hold keys fans out: state/, plans/."
        denies = "cache/ and registry/ are read from the default store alone."
        assert "state/" in self._fanout_clause(claims + " " + denies)
        assert "cache/" not in self._fanout_clause(claims + " " + denies)
        assert "cache/" in self._fanout_clause("A listing of cache/ fans out too.")
        assert self._fanout_clause(denies) == ""

    def test_a_surface_that_lists_prefixes_lists_the_fanning_ones(self):
        """The converse: a surface that enumerates must enumerate completely.

        Only the two that actually list prefixes are held to this -- the warning
        log deliberately states the rule without naming them, which is right for
        a log line and must not be turned into a list by a gate.
        """
        fanning = {p for p in _fanning_prefixes() if p.count("/") == 1}
        for label in ("helm values comment", "values.schema.json description"):
            prose = _SURFACES[label]()
            missing = sorted(p for p in fanning if p not in prose)
            assert missing == [], f"{label} enumerates prefixes but omits: {missing}"
