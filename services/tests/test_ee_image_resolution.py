"""One producer for an execution-environment image reference (#2154).

`effective_ee_image` resolves three things at once -- an operator label winning
over a line, `""` meaning "the deployment's default", and the repository the
line is a tag in. Each is a place a second implementation could disagree, and
the last time a fallback of this shape had two homes a deployment-wide read got
back in unwritten and nothing failed until someone measured it.

So this gate is derived from the tree rather than from a list: it finds every
module that reads the repository setting or the default line and allows only the
ones that are meant to. A hand-kept list of allowed readers would be the same
class of artifact as the thing it guards.
"""

from __future__ import annotations

import ast
import pathlib

SERVICES = pathlib.Path(__file__).resolve().parent.parent / "terrapod"

#: Only the resolver may turn a line into an image reference. The listing in the
#: same module reads the repository for a different reason -- asking the
#: registry what tags it holds -- which is why the unit is the module.
_RESOLVER_MODULE = "services/ansible_ee_service.py"

#: `config.py` declares the setting; a router may show it. Nothing else has a
#: reason to read it, because reading it is nearly always a prelude to building
#: a reference by hand.
_MAY_READ_REPOSITORY = {_RESOLVER_MODULE, "config.py"}

#: The column defaults live in the model, and workspace/rule create write the
#: default INTO the column -- a different act from resolving a column that may
#: be empty, which is the resolver's job alone.
_MAY_READ_DEFAULT_LINE = {
    _RESOLVER_MODULE,
    "db/models.py",
    "api/routers/tfe_v2.py",
    "api/routers/autodiscovery_rules.py",
}


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in SERVICES.rglob("*.py") if p.name != "__init__.py")


def _rel(path: pathlib.Path) -> str:
    return str(path.relative_to(SERVICES))


def _reads(tree: ast.AST, attribute: str) -> bool:
    """Whether the module reads `settings.ansible.<attribute>` anywhere."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == attribute
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "ansible"
        ):
            return True
    return False


def _mentions(source: str, name: str) -> bool:
    return name in source


class TestOnlyOneProducer:
    def test_the_repository_setting_is_read_only_where_it_should_be(self) -> None:
        offenders = []
        for path in _modules():
            if _rel(path) in _MAY_READ_REPOSITORY:
                continue
            if _reads(ast.parse(path.read_text()), "ee_repository"):
                offenders.append(_rel(path))
        assert not offenders, (
            "These modules read ansible.ee_repository, which is nearly always a "
            "prelude to building an image reference by hand. Call "
            f"ansible_ee_service.effective_ee_image instead: {offenders}"
        )

    def test_the_default_line_is_read_only_where_it_should_be(self) -> None:
        """A new reader of the default is how the empty-string fallback gets a
        second home -- the exact regression this replaces a deleted gate for."""
        offenders = []
        for path in _modules():
            if _rel(path) in _MAY_READ_DEFAULT_LINE:
                continue
            if _mentions(path.read_text(), "DEFAULT_ANSIBLE_LINE"):
                offenders.append(_rel(path))
        assert not offenders, (
            "These modules read DEFAULT_ANSIBLE_LINE. Resolving an empty "
            f"ansible_version is effective_ee_image's job alone: {offenders}"
        )

    def test_the_allowed_readers_all_still_exist(self) -> None:
        """A renamed module would otherwise silently widen both allow-lists."""
        for name in _MAY_READ_REPOSITORY | _MAY_READ_DEFAULT_LINE:
            assert (SERVICES / name).is_file(), f"{name} is listed as allowed and does not exist"

    def test_the_resolver_really_does_read_both(self) -> None:
        """Otherwise the gate could pass by inspecting nothing at all — the
        floor that distinguishes "checked and clean" from "matched nothing"."""
        source = (SERVICES / _RESOLVER_MODULE).read_text()
        assert _reads(ast.parse(source), "ee_repository")
        assert _mentions(source, "DEFAULT_ANSIBLE_LINE")

    def test_enough_modules_were_inspected(self) -> None:
        assert len(_modules()) > 100, (
            f"only {len(_modules())} modules found; the glob has stopped matching"
        )
