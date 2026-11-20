"""Every settable autodiscovery-rule attribute reaches every surface that offers them.

`test_workspace_setting_parity.py` is the same idea for WORKSPACE settings, and
#2621's ledger records why this file had to exist: "the autodiscovery-rule
attribute has no SDK/provider/MCP gate at all -- the contract test covers
workspaces only." That is why `propagate` sat on the API and in go-terrapod for
a whole feature's lifetime with neither provider carrying it, and why nobody
noticed that MCP has no rule tools whatsoever.

THE SUBJECT IS DERIVED, never listed. `_coerce_attrs` is the one function both
the create and the update route call, so the attributes it reads ARE the
settable set. It is read two ways because it writes them two ways:

  * `if "kebab-key" in attrs:` for an attribute with its own validation;
  * `for _key, _col in (("kebab-key", "column"), ...)` for the plain ones.

Reading only the first gives **37 of 72**, and the 35 it misses are not the
obscure ones -- `agent-pool-id`, `labels` and `parallelism` are all in a loop
table. A gate built on that half would have reported parity while saying nothing
about half the surface, which is the shape this file exists to prevent.

`test_the_derivation_sees_every_propagated_field` is what keeps that honest.
`PROPAGATED_FIELDS` is maintained independently of this gate, by the propagation
service, and every one of its 59 entries must appear in the derived set. If a
future refactor moves attributes into a construct neither reader understands,
that test fails rather than the gate quietly shrinking.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re
import textwrap
from collections.abc import Callable

from terrapod.api.routers.autodiscovery_rules import _coerce_attrs
from terrapod.services.rule_propagation_service import PROPAGATED_FIELDS
from tests.api.test_workspace_setting_parity import _surface

#: The derived set is this large today. A floor, not an assertion about the
#: number: it fails when the derivation stops seeing a construct, which is the
#: failure mode that makes a derived gate pass on nothing.
_MIN_ATTRS = 65


def _settable_attrs() -> set[str]:
    """Every attribute `_coerce_attrs` reads, from its own source."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(_coerce_attrs)))
    found: set[str] = set()
    for node in ast.walk(tree):
        # `if "kebab-key" in attrs:`
        if isinstance(node, ast.Compare) and node.ops and isinstance(node.ops[0], ast.In):
            left, right = node.left, node.comparators[0]
            if (
                isinstance(left, ast.Constant)
                and isinstance(left.value, str)
                and isinstance(right, ast.Name)
                and right.id == "attrs"
            ):
                found.add(left.value)
        # `for _key, _col in (("kebab-key", "column"), ...)`
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple | ast.List):
            for element in node.iter.elts:
                if isinstance(element, ast.Tuple | ast.List) and element.elts:
                    first = element.elts[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        found.add(first.value)
    return found


# ── the surfaces ────────────────────────────────────────────────────────────

_PROVIDER = _surface("provider/internal/resources/autodiscovery_rule/resource.go")
_WEB = _surface("web/src/app/admin/autodiscovery/page.tsx")
#: Where the MCP rule tools will live. NOT resolved through `_surface`, which
#: refuses a path that does not exist -- deliberately, so a gate cannot read an
#: absent file and pass on nothing. The absence is asserted instead, by
#: `test_mcp_still_has_no_rule_tools`, so whoever creates this file is told to
#: wire the surface into this gate in the same change.
_MCP_REL = "mcp/internal/mcpserver/autodiscovery.go"


def _optional_surface(rel: str) -> pathlib.Path | None:
    """Resolve `rel` the way `_surface` does, or None when it is absent.

    It has to search the SAME two bases `_surface` searches -- the working tree
    and the test image's flattened `/app` -- rather than counting `.parents[n]`
    off another resolved file. The first version of this did the latter, took
    `_PROVIDER.parents[3]` (which is `provider/`, not the repo root) and so
    asked whether `provider/mcp/internal/...` existed. It never could, so the
    absence assertion below could not fail, in the gate whose own docstring is
    about derivations that quietly check nothing.
    """
    tests_dir = pathlib.Path(__file__).resolve().parent
    for base in (tests_dir.parents[2], tests_dir.parents[1]):
        candidate = base / rel
        if candidate.exists():
            return candidate
    return None


def _aliases() -> dict[str, frozenset[str]]:
    """Attributes that are alternative spellings of one setting, DERIVED.

    Two attributes that write the same `out["column"]` are two spellings of one
    thing, and a surface offering either offers the setting. That is not a
    judgement: the router says so in as many words for the pair that exists
    today -- `auto-apply` is "accepted as an INPUT spelling and no longer
    stored (#2503)", and both it and `auto-apply-mode` write
    `out["auto_apply_mode"]`.

    Derived rather than declared because a declared table is a second thing to
    drift, and because the existing workspace gate records what a one-way alias
    table cost: `auto-apply` carried an exemption reading "deliberately not
    fleet-settable from a form: it arms unattended applies", which was false --
    the form offered the mode selector all along. A false reason reads as
    considered, which is the worse of the two to leave standing.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(_coerce_attrs)))
    by_column: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and test.ops
            and isinstance(test.ops[0], ast.In)
            and isinstance(test.left, ast.Constant)
            and isinstance(test.left.value, str)
            and isinstance(test.comparators[0], ast.Name)
            and test.comparators[0].id == "attrs"
        ):
            continue
        attr = test.left.value
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Subscript):
                continue
            if not (isinstance(inner.value, ast.Name) and inner.value.id == "out"):
                continue
            key = inner.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                by_column.setdefault(key.value, set()).add(attr)

    out: dict[str, frozenset[str]] = {}
    for spellings in by_column.values():
        if len(spellings) < 2:
            continue
        for attr in spellings:
            out[attr] = frozenset(spellings - {attr})
    return out


def _tfsdk(attr: str) -> tuple[str, ...]:
    return (f'tfsdk:"{attr.replace("-", "_")}"',)


def _camel(attr: str) -> str:
    head, *rest = attr.split("-")
    return head + "".join(word.capitalize() for word in rest)


def _web_rule(attr: str) -> tuple[str, ...]:
    """The forms the rule's `.tsx` form can offer an attribute in.

    Deliberately NOT `_web_tsx` from the workspace gate, which anchors its
    unquoted form on `.attr =`. This page builds its payload with **object
    shorthand** -- a bare `branch,` inside an object literal -- which that
    anchor cannot see, so `branch` read as missing while the page has offered it
    all along: a form field, a `useState`, a `setBranch(a.branch)` and the
    shorthand in the payload.
    //
    That blind spot is in the shared helper too, and is left there rather than
    widened from here: changing it would re-judge 46 attributes across five
    surfaces in a gate with 25 tests, which is its own change.

    The shorthand form is a REGEX (see `_matches`), because the bare substring
    `branch,` also appears in `const [branch, setBranch] = useState('')` -- a
    destructuring, which is not the form offering the setting.
    """
    camel = _camel(attr)
    return (
        f"'{attr}'",
        f'"{attr}"',
        f"'{camel}'",
        f'"{camel}"',
        f".{camel} =",
        f"{camel}:",
        rf"(?m)^\s*{re.escape(camel)},\s*$",
    )


_SPELLING: dict[str, Callable[[str], tuple[str, ...]]] = {
    "Terraform provider": _tfsdk,
    "web rule form": _web_rule,
}

_SURFACE_FILES: dict[str, pathlib.Path] = {
    "Terraform provider": _PROVIDER,
    "web rule form": _WEB,
}


def _matches(form: str, text: str) -> bool:
    """A form is a plain substring unless it is marked as a regex with `(?m)`."""
    if form.startswith("(?m)"):
        return re.search(form, text) is not None
    return form in text


#: go-terrapod is NOT a surface here, and that is a property of the SDK rather
#: than a gap. `CreateAutodiscoveryRule` and `UpdateAutodiscoveryRule` take
#: `attrs map[string]any` and pass it through, so the SDK enumerates no rule
#: attribute and a new one reaches it for free. A typed struct and an untyped
#: bag have opposite fan-outs, and asserting against the untyped one would
#: report 71 of 72 missing for ever.
_SDK_IS_UNTYPED = True


#: Measured on 2026-11-20, each entry a REAL gap rather than a matcher artefact
#: -- `engine` and `branch` were both checked against the column list and the
#: page before landing here, and `branch` turned out to be present.
#:
#: Every entry must name a reason. An entry whose attribute is in fact present
#: is a stale excuse, which `test_no_debt_entry_is_stale` refuses.
_SURFACE_DEBT: dict[str, dict[str, str]] = {
    "Terraform provider": {
        "ansible-host-key-checking": (
            "#2621: not ported. The rule templates it onto the workspaces it creates, "
            "and terrapod_workspace does not carry it either, so porting it here first "
            "would let a rule set something no workspace resource can then manage."
        ),
        "default-run-kind": (
            "#2621: not ported. Refused by the server against the RESULTING row, so a "
            "provider attribute needs the operations booleans alongside it to be usable."
        ),
        "dynamic-inventory-enabled": (
            "#2621: not ported. Permits inventory plugins on the runner (#2033), which "
            "is a trust decision an operator should make deliberately rather than "
            "inherit from a rule's HCL."
        ),
        "engine": (
            "#2621: not ported. The provider carries engine_version but not the engine "
            "itself, so a rule cannot yet declare a Pulumi or Ansible workspace in HCL."
        ),
        "hard-limit": (
            "#2621: not ported. The ceiling on how many workspaces one rule may create; "
            "absent from HCL, an operator cannot bound a rule they manage as code."
        ),
        "inventory-scripts-enabled": (
            "#2621: not ported, and the same trust decision as "
            "dynamic-inventory-enabled -- both let repository code execute on a runner."
        ),
        "pulumi-bind-plan": (
            "#2621: not ported. Only meaningful once `engine` is, since it is a Pulumi "
            "setting, so these two want porting together."
        ),
        "storage-location": (
            "#2621: not ported on ANY consumer -- absent from the web form too, so this "
            "is a feature with one surface rather than a provider oversight."
        ),
    },
    "web rule form": {
        "security-scan-skip-rules": (
            "#2621: the form renders no control. The engine, enforcement level and "
            "severity threshold are all offered, so the skip list is the one part of "
            "the scanning block an operator cannot set from the UI."
        ),
        "storage-location": (
            "#2621: no control, matching the provider -- see that entry. One surface."
        ),
    },
}

#: True while MCP has no rule tools. Separate from `_SURFACE_DEBT` because the
#: debt there is the SURFACE, not the attributes: enumerating 72 entries would
#: read as 72 decisions when it is one.
_MCP_SURFACE_ABSENT = True


# ── the gate ────────────────────────────────────────────────────────────────


class TestTheDerivationItself:
    """The subject is derived, so the derivation is the first thing to pin."""

    def test_it_sees_every_propagated_field(self) -> None:
        # `PROPAGATED_FIELDS` is maintained by the propagation service, not by
        # this gate, so it is an INDEPENDENT statement of what a rule attribute
        # is. Reading only `if "x" in attrs` sees 37 of 72 and misses 35 of
        # these, so this is what caught that and what will catch the next
        # construct neither reader understands.
        missed = sorted(set(PROPAGATED_FIELDS) - _settable_attrs())
        assert not missed, (
            f"{len(missed)} propagated field(s) are not in the derived set, so the "
            f"derivation has stopped seeing how `_coerce_attrs` reads them: {missed}"
        )

    def test_the_derived_set_has_not_shrunk(self) -> None:
        attrs = _settable_attrs()
        assert len(attrs) >= _MIN_ATTRS, (
            f"the derivation found only {len(attrs)} attributes, below the floor of "
            f"{_MIN_ATTRS}. A derived gate that reads nothing passes everything."
        )

    def test_every_derived_attribute_is_a_real_column(self) -> None:
        from terrapod.db.models import AutodiscoveryRule

        columns = {c.name for c in AutodiscoveryRule.__table__.columns}
        # kebab on the wire, snake in the column; a handful are deliberately
        # not columns of their own (they feed a helper that writes several).
        not_columns = sorted(a for a in _settable_attrs() if a.replace("-", "_") not in columns)
        assert len(not_columns) <= 6, (
            "more derived attributes than expected do not map to a column, which "
            f"suggests the derivation is picking up unrelated string literals: {not_columns}"
        )


class TestEverySettableAttributeReachesEverySurface:
    def test_surfaces(self) -> None:
        attrs = _settable_attrs()
        aliases = _aliases()
        problems: list[str] = []
        for surface, path in _SURFACE_FILES.items():
            text = path.read_text()
            spell = _SPELLING[surface]
            debt = _SURFACE_DEBT.get(surface, {})
            for attr in sorted(attrs):
                candidates = (attr, *sorted(aliases.get(attr, ())))
                if any(_matches(form, text) for c in candidates for form in spell(c)):
                    continue
                if attr in debt:
                    continue
                problems.append(
                    f"{surface} does not offer {attr!r} (looked for: {', '.join(spell(attr))})"
                )
        assert not problems, "\n".join(problems)

    def test_mcp_still_has_no_rule_tools(self) -> None:
        """Asserted so that CREATING the surface completes this gate.

        MCP has 95 tools, four of them for MODULE autodiscovery and none for
        these, against eight API routes — so `propagate` has nowhere to go and
        neither does anything else. (95 is `tool_catalogue.json`'s own count and
        what `llms.txt` states; a count written from memory is worth nothing.) That is one missing surface rather than 72
        missing attributes, and recording it as 72 ledger entries would read as
        72 decisions.

        When the tools land, this test fails and says what to do: add
        `"MCP rule tools"` to `_SURFACE_FILES` and `_SPELLING`, which puts all
        72 attributes under the gate at once.
        """
        assert _MCP_SURFACE_ABSENT, "_MCP_SURFACE_ABSENT is stale; wire the surface in"
        assert _optional_surface(_MCP_REL) is None, (
            f"{_MCP_REL} now exists. Add 'MCP rule tools' to _SURFACE_FILES and "
            "_SPELLING (with _go_json_tag), drop _MCP_SURFACE_ABSENT, and let the "
            "gate judge all 72 attributes against it."
        )


class TestTheLedgerIsHonest:
    """A ledger a gate trusts needs its checkable half checked."""

    def test_no_debt_entry_is_stale(self) -> None:
        attrs = _settable_attrs()
        aliases = _aliases()
        stale: list[str] = []
        for surface, debt in _SURFACE_DEBT.items():
            path = _SURFACE_FILES.get(surface)
            if path is None:
                continue
            text = path.read_text()
            spell = _SPELLING[surface]
            for attr in sorted(debt):
                candidates = (attr, *sorted(aliases.get(attr, ())))
                if attr in attrs and any(_matches(f, text) for c in candidates for f in spell(c)):
                    stale.append(
                        f"{surface}: {attr!r} IS present (itself or an accepted "
                        "alias) — remove the entry"
                    )
        assert not stale, "\n".join(stale)

    def test_every_debt_entry_names_a_real_attribute(self) -> None:
        attrs = _settable_attrs()
        bogus = sorted(
            f"{surface}: {attr!r}"
            for surface, debt in _SURFACE_DEBT.items()
            for attr in debt
            if attr not in attrs
        )
        assert not bogus, (
            "debt entries for attributes that are not settable at all — either a "
            f"typo or an attribute that has since been removed: {bogus}"
        )

    def test_every_debt_entry_carries_a_reason(self) -> None:
        thin = sorted(
            f"{surface}: {attr!r}"
            for surface, debt in _SURFACE_DEBT.items()
            for attr, reason in debt.items()
            if len(reason.strip()) < 20
        )
        assert not thin, f"debt entries with no usable reason: {thin}"


class TestEverySurfaceFileReachesTheTestImage:
    """A `_surface()` path with no COPY line is a collection error in CI.

    `_surface` refuses a path it cannot find rather than skipping, which is
    right — a gate that cannot read the file it checks proves nothing. The cost
    is that the refusal lands at IMPORT time, so one missing COPY line turns the
    whole module into four collection errors in a shard that otherwise passed
    2804 tests, and the local run cannot see it because the working tree has
    every file.

    That is exactly what this gate's own first CI run did, with
    `provider/internal/resources/autodiscovery_rule/resource.go` uncopied and
    `_surface`'s message naming the remedy. So the class is checked here rather
    than the instance being fixed: a future `_surface()` path fails this test,
    which names the line to add, instead of failing collection.

    `docker/` is itself copied into the image (`COPY docker ./docker`), so this
    runs in CI as well as locally.
    """

    @staticmethod
    def _surface_paths() -> set[str]:
        paths: set[str] = set()
        tests_root = pathlib.Path(__file__).parent.parent
        for path in tests_root.rglob("test_*.py"):
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:  # pragma: no cover - not our file to fix
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_surface"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    paths.add(node.args[0].value)
        return paths

    def test_every_surface_path_has_a_copy_line(self) -> None:
        paths = self._surface_paths()
        # A floor, because a matcher that stops finding `_surface()` calls
        # would otherwise pass on nothing.
        assert len(paths) >= 5, (
            f"only found {len(paths)} _surface() paths, which suggests the AST "
            "walk has stopped matching rather than that the gates have shrunk"
        )
        # `_optional_surface`, not `.parents[n]` — and this gate is why that
        # helper's docstring says so. The commit before this one fixed exactly
        # this arithmetic in the MCP absence check; this test, written in the
        # same breath, repeated it. `parents[3]` is the repo root locally and
        # `/` in the test image, whose layout is one directory shallower, so it
        # passed here and failed CI on `/docker/Dockerfile.test`.
        dockerfile = _optional_surface("docker/Dockerfile.test")
        assert dockerfile is not None, (
            "docker/Dockerfile.test is not readable from here, so this gate can "
            "check nothing. The image copies `docker/`, so its absence is a "
            "packaging change rather than something to skip over."
        )
        copied = dockerfile.read_text()
        missing: list[str] = []
        for rel in sorted(paths):
            directory = str(pathlib.PurePosixPath(rel).parent)
            suffix = pathlib.PurePosixPath(rel).suffix
            # Either the file by name, or a glob over its own directory.
            if rel in copied or f"{directory}/*{suffix}" in copied:
                continue
            # Or an ancestor directory copied wholesale (`COPY helm ./helm`).
            parts = pathlib.PurePosixPath(rel).parts
            if any(f"COPY {'/'.join(parts[: i + 1])} ." in copied for i in range(len(parts) - 1)):
                continue
            missing.append(rel)
        assert not missing, (
            "these files are read by a parity gate and are NOT in the test image, so "
            "the module will fail to collect in CI while passing locally. Add a COPY "
            f"line to docker/Dockerfile.test for each: {missing}"
        )
