"""A workspace's repo is matched on the PARSED pair, never on a URL suffix (#2199).

`PRSession.repo` is written as `owner/name` from `parse_repo_url`, which strips
`.git` before parsing. Four queries nevertheless filtered
`Workspace.vcs_repo_url.endswith(sess.repo)` in SQL, so a workspace configured
with the clone URL GitHub hands out -- `https://host/acme/infra.git` -- matched
none of them:

* auto-merge never fired (the symptom #2199 set out to fix),
* the PR status comment rendered with no workspace rows,
* merge-commit attribution missed its rows,
* and a closed PR's active runs were never cancelled, leaving an apply-capable
  `planned` run holding the workspace lock against every other PR.

`repo_path`'s own docstring already called the suffix form "the tempting
shortcut" and named the ambiguity, and `_select_workspace_ids` had already
implemented the parsed exact-match with its reasoning written out -- so the
correct pattern existed and four sites did not use it. Hence a gate as well as
the unit tests: the next query must not reintroduce it.
"""

import ast
import pathlib
import types

import pytest

from terrapod.services import vcs_provider
from terrapod.services.vcs_provider import workspace_repo_is

#: Modules that resolve a workspace against a session's repo.
_MODULES = (
    "vcs_auto_merge.py",
    "vcs_status_comment.py",
    "vcs_poller.py",
)
#: Below this the subject has gone missing rather than the rule holding.
_MIN_MODULES = 3


def _conn(provider="github"):
    return types.SimpleNamespace(provider=provider, server_url="")


def _ws(url, provider="github"):
    return types.SimpleNamespace(vcs_connection=_conn(provider), vcs_repo_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/infra.git",  # the clone URL GitHub hands out
        "https://github.com/acme/infra",
        "https://github.com/acme/infra/",
        "git@github.com:acme/infra.git",
        "git@github.com:acme/infra",
    ],
)
def test_every_supported_url_form_matches_its_own_repo(url):
    """All five are documented supported forms; three failed a suffix match."""
    assert workspace_repo_is(_ws(url), "acme/infra") is True


def test_the_git_suffix_form_is_the_one_a_suffix_match_dropped():
    """The regression this exists for, stated as the contrast that proves it."""
    url = "https://github.com/acme/infra.git"
    assert url.endswith("acme/infra") is False  # what the old SQL asked
    assert workspace_repo_is(_ws(url), "acme/infra") is True  # what it should ask


def test_a_longer_owner_is_not_a_match():
    """The suffix form was unanchored: `/xacme/infra` ends with `acme/infra`."""
    url = "https://github.com/xacme/infra"
    assert url.endswith("acme/infra") is True  # the false positive it allowed
    assert workspace_repo_is(_ws(url), "acme/infra") is False


def test_a_different_repo_does_not_match():
    assert workspace_repo_is(_ws("https://github.com/acme/other"), "acme/infra") is False


def test_it_refuses_rather_than_widening_when_it_cannot_tell():
    """`repo_path` returns None for an unparseable URL; refuse, never fall back."""
    assert workspace_repo_is(_ws(""), "acme/infra") is False
    assert workspace_repo_is(_ws("not a url at all"), "acme/infra") is False
    no_conn = types.SimpleNamespace(vcs_connection=None, vcs_repo_url="https://x/a/b")
    assert workspace_repo_is(no_conn, "a/b") is False


def test_no_module_filters_a_session_repo_by_url_suffix():
    """The gate. A new query must not reintroduce the suffix match.

    Derived from the source rather than from a list of line numbers, because the
    defect was four separate call sites of one wrong predicate -- the shape that
    recurs when a rule is restated per site instead of shared.
    """
    root = pathlib.Path(vcs_provider.__file__).parent
    offenders = []
    checked = 0
    for name in _MODULES:
        path = root / name
        assert path.exists(), f"{name} has moved -- this gate is checking nothing"
        checked += 1
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            # `Workspace.vcs_repo_url.endswith(<anything>)`
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "endswith"):
                continue
            if "vcs_repo_url" in ast.dump(fn.value):
                offenders.append(f"{name}:{node.lineno}")
    assert checked >= _MIN_MODULES, f"only inspected {checked} modules"
    assert not offenders, (
        "these filter a workspace by URL suffix; use "
        f"vcs_provider.workspace_repo_is instead (#2199): {offenders}"
    )
