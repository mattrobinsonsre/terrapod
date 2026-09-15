"""Vault file delivery: path rules, targets and collisions (#1619).

`terrapod.runner.vault_files` is the single definition of where a file-mode
Vault variable may land. The API validates with it on write and again when a
run is claimed; the listener validates with it before building any mount. So
every accepted and rejected form is pinned here, with the exact reason text the
422 detail and the run error carry.
"""

import pytest

from terrapod.runner import vault_files as vf

# ── Accepted names ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "adc.json",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "gcp/adc.json",
        "a/b/c/d.pem",
        ".hidden",
        "a..b",  # only a whole `.` / `..` segment is special
        "...",
        "A-Z_0.9",
        ".ssh/id_rsa",  # relative: lands under the files dir, not HOME
        "x" * 255,  # the limit itself
        "~/.aws/credentials",
        "~/.kube/config",
        "~/x",
        "~/.config/gcloud/application_default_credentials.json",
        "~/.configx",  # a sibling of a managed path, not inside it
        "~/.sshx/key",
        "~/.config/other",
        "~/" + "x" * 253,  # 255 characters including `~/`
    ],
)
def test_accepted_names(name):
    assert vf.validate_name(name) == name


# ── Rejected names, with the exact reason ─────────────────────────────

_ABSOLUTE = (
    "must be a relative path, or start with ~/ for a path in the runner's home "
    "directory; absolute paths are not allowed"
)
_EMPTY_SEG = "has an empty path segment"
_DOT_SEG = "has a '.' or '..' path segment"


def _chars(seg: str) -> str:
    return f"has a path segment with characters outside [A-Za-z0-9._-]: {seg!r}"


def _managed(p: str) -> str:
    return f"targets ~/{p}, which the runner manages itself; choose another path"


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        (123, "must be a string"),
        (None, "must be a string"),
        ("", "must not be empty"),
        ("a\x00b", "contains a NUL character"),
        ("~/a\x00", "contains a NUL character"),
        ("x" * 256, "is longer than 255 characters"),
        ("~/" + "x" * 254, "is longer than 255 characters"),
        ("/etc/passwd", _ABSOLUTE),
        ("/", _ABSOLUTE),
        ("~//etc/passwd", _ABSOLUTE),
        ("a//b", _EMPTY_SEG),
        ("a/", _EMPTY_SEG),
        ("~/", _EMPTY_SEG),
        ("~/a/", _EMPTY_SEG),
        (".", _DOT_SEG),
        ("..", _DOT_SEG),
        ("./a", _DOT_SEG),
        ("a/../b", _DOT_SEG),
        ("a/./b", _DOT_SEG),
        ("~/..", _DOT_SEG),
        ("~/../etc/passwd", _DOT_SEG),
        ("~", _chars("~")),
        ("~user/x", _chars("~user")),
        ("a\\b", _chars("a\\b")),
        ("..\\..\\etc", _chars("..\\..\\etc")),
        ("café.json", _chars("café.json")),
        ("ﬁle", _chars("ﬁle")),  # a ligature that NFKC would turn into "file"
        ("a b", _chars("a b")),
        ("a:b", _chars("a:b")),
        ("a*", _chars("a*")),
        ("$HOME/x", _chars("$HOME")),
        ("a\nb", _chars("a\nb")),
        # Everything the runner writes or configures under HOME, and the
        # directories above them (a file there would block the directory).
        ("~/.ssh", _managed(".ssh")),
        ("~/.ssh/id_rsa", _managed(".ssh")),
        ("~/.ssh/known_hosts", _managed(".ssh")),
        ("~/.gitconfig", _managed(".gitconfig")),
        ("~/.config/terrapod-git", _managed(".config/terrapod-git")),
        ("~/.config/terrapod-git/gitconfig", _managed(".config/terrapod-git")),
        ("~/.config/terrapod-git/ssh/config", _managed(".config/terrapod-git")),
        ("~/.config", _managed(".config/terrapod-git")),
        ("~/.terraformrc", _managed(".terraformrc")),
        ("~/.terraform.rc", _managed(".terraform.rc")),
        ("~/.terraform.d", _managed(".terraform.d")),
        ("~/.terraform.d/credentials.tfrc.json", _managed(".terraform.d")),
        ("~/.terraform.d/plugin-cache/x", _managed(".terraform.d")),
        # The Pulumi CLI's home on a multi-engine line (plugins, workspace state).
        ("~/.pulumi", _managed(".pulumi")),
        ("~/.pulumi/plugins/resource-aws", _managed(".pulumi")),
    ],
)
def test_rejected_names_carry_the_exact_reason(name, reason):
    with pytest.raises(vf.FilePathError) as e:
        vf.validate_name(name)
    assert str(e.value) == reason


def test_every_managed_home_path_is_refused_and_listed():
    """A new managed path must be added to the list, and the list is enforced."""
    for managed in vf.MANAGED_HOME_PATHS:
        with pytest.raises(vf.FilePathError, match="which the runner manages itself"):
            vf.validate_name(f"~/{managed}")
        with pytest.raises(vf.FilePathError, match="which the runner manages itself"):
            vf.validate_name(f"~/{managed}/inner")


def test_the_git_auth_phase_base_dir_is_a_managed_path():
    """Pinned against the phase itself, so moving its base dir fails here."""
    import inspect

    from terrapod.runner.phases import git_auth

    assert '".config" / "terrapod-git"' in inspect.getsource(git_auth.run)
    assert ".config/terrapod-git" in vf.MANAGED_HOME_PATHS


def test_the_runner_home_matches_the_job_template():
    import inspect

    from terrapod.runner import job_template

    src = inspect.getsource(job_template.build_job_spec)
    assert f'{{"name": "HOME", "value": "{vf.RUNNER_HOME}"}}' in src
    assert f'"mountPath": "{vf.RUNNER_HOME}"' in src


# ── Targets ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("adc.json", "/var/run/terrapod/files/adc.json"),
        ("gcp/adc.json", "/var/run/terrapod/files/gcp/adc.json"),
        ("~/.aws/credentials", "/home/runner/.aws/credentials"),
        ("~/x", "/home/runner/x"),
    ],
)
def test_target_path(name, target):
    assert vf.target_path(name) == target


def test_home_parent_dirs_are_every_ancestor_below_home_deduplicated():
    assert vf.home_parent_dirs(
        ["~/.aws/credentials", "~/.aws/config", "~/a/b/c", "~/top", "rel/x"]
    ) == ["/home/runner/.aws", "/home/runner/a", "/home/runner/a/b"]


def test_no_parent_dirs_for_files_directly_in_home_or_relative():
    assert vf.home_parent_dirs(["~/top", "a/b"]) == []


# ── Collisions (after variable-set precedence) ────────────────────────


def test_distinct_paths_do_not_collide():
    vf.check_collisions([("A", "a.json"), ("B", "b/c.json"), ("C", "~/.aws/credentials")])


def test_two_variables_at_one_path_collide_and_both_are_named():
    with pytest.raises(vf.FilePathError) as e:
        vf.check_collisions([("A", "gcp/adc.json"), ("B", "gcp/adc.json")])
    assert str(e.value) == (
        "variables 'A' and 'B' both deliver an OpenBao/Vault file to /var/run/terrapod/files/gcp/adc.json"
    )


def test_two_home_variables_at_one_path_collide():
    with pytest.raises(
        vf.FilePathError, match="both deliver an OpenBao/Vault file to /home/runner/.aws/c"
    ):
        vf.check_collisions([("A", "~/.aws/c"), ("B", "~/.aws/c")])


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        (
            [("A", "a"), ("B", "a/b")],
            "variable 'A' delivers an OpenBao/Vault file to /var/run/terrapod/files/a, which "
            "variable 'B' needs as a directory for /var/run/terrapod/files/a/b",
        ),
        (
            # Order of the input does not matter.
            [("B", "a/b/c"), ("A", "a")],
            "variable 'A' delivers an OpenBao/Vault file to /var/run/terrapod/files/a, which "
            "variable 'B' needs as a directory for /var/run/terrapod/files/a/b/c",
        ),
        (
            [("A", "~/.aws"), ("B", "~/.aws/credentials")],
            "variable 'A' delivers an OpenBao/Vault file to /home/runner/.aws, which "
            "variable 'B' needs as a directory for /home/runner/.aws/credentials",
        ),
    ],
)
def test_a_file_where_another_needs_a_directory_collides(entries, message):
    with pytest.raises(vf.FilePathError) as e:
        vf.check_collisions(entries)
    assert str(e.value) == message


def test_a_string_prefix_that_is_not_a_directory_prefix_is_fine():
    vf.check_collisions([("A", "a"), ("B", "ab"), ("C", "a.json")])


def test_a_home_path_and_a_relative_path_can_never_meet():
    """The two roots are disjoint and neither kind can contain `..`, so the same
    name under each is two different files, not a clash."""
    vf.check_collisions([("A", "x/y"), ("B", "~/x/y")])
    assert vf.target_path("x/y") != vf.target_path("~/x/y")
    assert not vf.target_path("~/x/y").startswith(vf.FILES_DIR)
    assert not vf.FILES_DIR.startswith(vf.RUNNER_HOME)


def test_secret_keys_are_valid_kubernetes_secret_keys_and_distinct():
    import re

    keys = {vf.secret_key(i) for i in range(50)}
    assert len(keys) == 50
    assert all(re.fullmatch(r"[-._a-zA-Z0-9]+", k) for k in keys)
