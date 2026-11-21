"""`backup.prefix` in values.schema.json must refuse what `shared_prefix` refuses.

Helm lints with the schema and the backup job validates with `shared_prefix`; without
this, a bad prefix installs cleanly and the job fails at its next scheduled run.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import jsonschema
import pytest

from terrapod.storage.keys import AGE_SWEPT_PREFIXES, WORKSPACE_KEY_PREFIXES, shared_prefix

_HELM_ROOT = Path("/app/helm/terrapod")
if not _HELM_ROOT.exists():  # local checkout fallback (running outside the image)
    _HELM_ROOT = Path(__file__).resolve().parents[3] / "helm" / "terrapod"


@functools.cache
def _validator() -> jsonschema.Draft7Validator:
    """Read and parse the schema ONCE.

    `_schema_accepts` used to do both on every call. That is invisible at a dozen
    cases and is minutes at the cross product below, which is the kind of cost that
    gets a thorough test deleted rather than fixed.
    """
    schema = json.loads((_HELM_ROOT / "values.schema.json").read_text())
    return jsonschema.Draft7Validator(schema["properties"]["backup"]["properties"]["prefix"])


def _schema_accepts(prefix: str) -> bool:
    return _validator().is_valid(prefix)


def _model_accepts(prefix: str) -> bool:
    try:
        shared_prefix("backup.prefix", prefix)
    except ValueError:
        return False
    return True


#: Every spelling of a prefix one of the two legs must refuse. Derived from both
#: constants, so a seventh workspace prefix or a second age-swept one is covered the
#: day it is added, and spelled every way the normaliser accepts a prefix in --
#: `/state/` is refused only because `shared_prefix` strips the leading slash before
#: comparing, so a schema pattern that forgot that would pass the old list and let
#: the bad prefix install cleanly.
_REFUSED = [
    spelling
    for p in (*WORKSPACE_KEY_PREFIXES, *AGE_SWEPT_PREFIXES)
    for spelling in (
        p,
        p + "db/",
        p.rstrip("/"),
        "./" + p,
        "././" + p.rstrip("/"),
        "/" + p,
        "//" + p.rstrip("/"),
    )
]
#: `""` is NOT here any more (#2621). It used to be blessed as ACCEPTED by both
#: legs, where the model returned `/` -- an absolute key the filesystem store
#: refuses, after a full pg_dump has run -- while the chart's `| default` silently
#: rewrote it to `backups/`. Three legs, three answers.
_REFUSED_AS_EMPTY = ["", ".", "./", "/", "././", "//", ".//"]

_ACCEPTED = ["backups/", "backups", "dr/state/", "statements/", "./backups/", ".backups/"]


def test_the_tables_are_not_empty() -> None:
    """A derived parametrize that stops deriving skips rather than fails."""
    assert len(_REFUSED) >= 56
    assert len(_REFUSED_AS_EMPTY) >= 7
    assert len(_ACCEPTED) >= 6


@pytest.mark.parametrize("prefix", _REFUSED)
def test_both_refuse_a_prefix_terrapod_owns(prefix: str) -> None:
    assert not _schema_accepts(prefix)
    assert not _model_accepts(prefix)


@pytest.mark.parametrize("prefix", _REFUSED_AS_EMPTY)
def test_both_refuse_a_prefix_naming_nothing(prefix: str) -> None:
    assert not _schema_accepts(prefix)
    assert not _model_accepts(prefix)


@pytest.mark.parametrize("prefix", _ACCEPTED)
def test_both_accept_a_shared_prefix(prefix: str) -> None:
    assert _schema_accepts(prefix)
    assert _model_accepts(prefix)


def test_the_two_legs_agree_on_every_spelling_of_every_prefix() -> None:
    """The property, rather than three lists that happen to agree.

    The lists above are the readable cases; this is the one that cannot be
    satisfied by a pattern that merely covers them. It is the whole cross product
    of leading junk x body x trailing slash, so a schema that handles `./state/`
    and forgets `//state` fails here and names the spelling.
    """
    bodies = [
        "",
        ".",
        "backups",
        "dr/state",
        "statements",
        "stateful",
        ".backups",
        "cache",
        "registry",
        "uploads",
        *(p.rstrip("/") for p in WORKSPACE_KEY_PREFIXES),
        *(p.rstrip("/") for p in AGE_SWEPT_PREFIXES),
        *(p.rstrip("/") + "/db" for p in AGE_SWEPT_PREFIXES),
    ]
    leads = ["", "./", "/", "././", "//", ".//"]
    spellings = [lead + body + tail for lead in leads for body in bodies for tail in ("", "/")]
    assert len(spellings) >= 200
    disagree = [p for p in spellings if _schema_accepts(p) != _model_accepts(p)]
    assert not disagree, f"schema and model disagree on: {disagree[:10]}"
