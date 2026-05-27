"""Behavioural tests for the runner entrypoint's backend-override logic
(#346).

The runner used to edit user code in place with an anchored-regex awk
program to remove ``cloud {}`` and top-level ``backend "x" {}`` blocks.
That was structurally fragile (multi-line openers, comments on the
opener, heredocs/strings containing braces). #346 replaced it with a
terraform-native override file: ``zzzz_terrapod_backend_override.tf``,
which declares ``terraform { backend "local" {} }`` and gets merged by
terraform/tofu with replacement semantics, displacing whatever the main
config declared.

The runner MUST execute on the local backend — a remote backend inside
the Job recurses back into Terrapod — so our override has to win
unconditionally. It wins because override files merge in lexical order
with the *last* file taking precedence, and the ``zzzz`` prefix sorts
after ``override.tf`` and any realistic ``*_override.tf``. There is no
detect-and-defer: deferring to a user-supplied ``cloud {}`` /
``backend "remote"`` would hand the runner a remote backend. A
user-supplied backend override is logged (visibility) but ours is still
written and still wins.

These tests extract the write fragment directly from
``docker/runner-entrypoint.sh`` (so a future change to the logic is
exercised) and run it against fixture workdirs.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

OVERRIDE_NAME = "zzzz_terrapod_backend_override.tf"


def _find_entrypoint() -> Path:
    """Locate ``docker/runner-entrypoint.sh`` from either layout.

    - Local dev: ``<repo-root>/docker/runner-entrypoint.sh`` (this file
      sits at ``services/tests/runner/test_backend_override.py``)
    - Docker test image: ``/app/docker/runner-entrypoint.sh`` (copied in
      by ``Dockerfile.test`` alongside ``services/``).
    """
    here = Path(__file__).resolve()
    for cand in (here.parents[3] / "docker", here.parents[2] / "docker"):
        path = cand / "runner-entrypoint.sh"
        if path.is_file():
            return path
    pytest.skip("runner-entrypoint.sh not reachable from the test environment")
    raise AssertionError("unreachable: pytest.skip() raises Skipped")  # pragma: no cover


def _extract_override_fragment() -> str:
    """Pull the override-write shell fragment out of the entrypoint.

    Spans from the ``TP_OVERRIDE_FILE=`` assignment through the ``log``
    line that announces the write. We extract by structural markers, not
    whole-script position, so unrelated edits elsewhere in the script
    don't break the test.
    """
    text = _find_entrypoint().read_text()
    match = re.search(
        r'(TP_OVERRIDE_FILE="\$STRIP_DIR/zzzz_terrapod_backend_override\.tf".*?\nTPOVR\n'
        r'\s*log "\[entrypoint\] Wrote \$TP_OVERRIDE_FILE[^\n]*)',
        text,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(
            "Could not locate the override-write fragment in runner-entrypoint.sh — "
            "structural markers (TP_OVERRIDE_FILE / TPOVR heredoc / Wrote log line) "
            "have changed; update this test."
        )
    return match.group(1)


def _run_fragment(workdir: Path) -> subprocess.CompletedProcess[str]:
    """Run the extracted fragment under ``sh`` against a fixture workdir.

    ``log`` is stubbed to echo so the test can assert on log lines;
    ``STRIP_DIR`` points at the fixture.
    """
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("sh not available")
    fragment = _extract_override_fragment()
    # The fragment lives inside an `if [ -f /tmp/config.tar.gz ]` block
    # in the real entrypoint and is indented eight spaces. Outdent so a
    # plain `sh -c` accepts it; otherwise the heredoc terminator (which
    # must be at column 0) won't match.
    fragment = "\n".join(
        line[8:] if line.startswith("        ") else line for line in fragment.splitlines()
    )
    script = f"""
set -eu
log() {{ echo "$@"; }}
STRIP_DIR={workdir!s}
{fragment}
"""
    return subprocess.run(
        [sh, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )


def test_writes_override_when_no_user_override(tmp_path: Path) -> None:
    """No user override file in the workdir → our override is written."""
    (tmp_path / "main.tf").write_text(
        "terraform {\n"
        "  cloud {\n"
        '    organization = "default"\n'
        '    workspaces { name = "w" }\n'
        "  }\n"
        "}\n"
        'resource "null_resource" "n" {}\n'
    )
    _run_fragment(tmp_path)
    ov = tmp_path / OVERRIDE_NAME
    assert ov.is_file(), "expected the entrypoint to write our override file"
    body = ov.read_text()
    assert 'backend "local" {}' in body
    assert "terraform {" in body


def test_writes_override_even_when_user_override_has_backend(tmp_path: Path) -> None:
    """User's ``override.tf`` already declares a ``backend`` → we do NOT
    defer to it. Ours is still written (and wins the merge by sorting
    later), and the user's override is logged for visibility."""
    (tmp_path / "main.tf").write_text("terraform {\n  cloud {}\n}\n")
    (tmp_path / "override.tf").write_text(
        'terraform {\n  backend "remote" {\n    organization = "x"\n  }\n}\n'
    )
    result = _run_fragment(tmp_path)
    assert (tmp_path / OVERRIDE_NAME).is_file(), (
        "ours must always be written — deferring to a user backend override "
        "could hand the runner a remote backend"
    )
    assert "takes precedence" in result.stdout, (
        "a user-supplied backend override should be logged for visibility"
    )


def test_writes_override_when_user_suffix_override_has_cloud(tmp_path: Path) -> None:
    """User's ``*_override.tf`` declares a ``cloud {}`` override → ours is
    still written and the user override is logged. ``_user_dev_override.tf``
    sorts before ``zzzz_terrapod_backend_override.tf`` (``_`` 0x5F < ``z``
    0x7A), so ours is the later file and wins the merge."""
    (tmp_path / "main.tf").write_text('terraform {\n  backend "remote" {}\n}\n')
    (tmp_path / "_user_dev_override.tf").write_text(
        'terraform {\n  cloud {\n    organization = "local"\n  }\n}\n'
    )
    result = _run_fragment(tmp_path)
    assert (tmp_path / OVERRIDE_NAME).is_file()
    assert "takes precedence" in result.stdout


def test_writes_when_user_override_is_unrelated(tmp_path: Path) -> None:
    """User's ``override.tf`` only overrides a resource → ours is written
    and no backend-override note is logged."""
    (tmp_path / "main.tf").write_text(
        'terraform {\n  cloud {}\n}\nresource "null_resource" "n" { triggers = { x = "main" } }\n'
    )
    (tmp_path / "override.tf").write_text(
        'resource "null_resource" "n" {\n  triggers = { x = "override" }\n}\n'
    )
    result = _run_fragment(tmp_path)
    assert (tmp_path / OVERRIDE_NAME).is_file()
    assert "takes precedence" not in result.stdout, (
        "an unrelated resource-only override must not be flagged as a backend override"
    )


def test_override_filename_sorts_after_override_tf() -> None:
    """The override filename must sort lexically after ``override.tf`` so
    it wins the override-file merge — that is the whole mechanism by
    which Terrapod's local backend takes precedence."""
    fragment = _extract_override_fragment()
    assert OVERRIDE_NAME in fragment
    assert OVERRIDE_NAME > "override.tf", (
        f"{OVERRIDE_NAME!r} must sort after 'override.tf' or it loses the merge"
    )
    assert OVERRIDE_NAME.endswith("_override.tf"), (
        "filename must end with _override.tf for terraform/tofu to treat it as an override file"
    )


# --- Post-init backend backstop -------------------------------------------
#
# The override file wins the merge in every realistic case, but merge order
# is not a *mathematical* guarantee (a user file sorting even later than
# zzzz_... would win). The entrypoint therefore reads the backend type that
# init actually configured — recorded in .terraform/terraform.tfstate — and
# fails the run if it is not `local`. A live runner can't easily reach the
# "init succeeded with a non-local backend" state (a remote backend without
# credentials fails at init), so the backstop logic is unit-tested here.


def _extract_backstop_fragment() -> str:
    """Pull the post-init backend backstop fragment from the entrypoint."""
    text = _find_entrypoint().read_text()
    match = re.search(
        r'(TP_CONFIGURED_BACKEND=\$\(jq.*?\nlog "\[entrypoint\] Backend verified: local")',
        text,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(
            "Could not locate the backend-backstop fragment in runner-entrypoint.sh — "
            "structural markers (TP_CONFIGURED_BACKEND / 'Backend verified: local') "
            "have changed; update this test."
        )
    return match.group(1)


def _run_backstop(workdir: Path) -> subprocess.CompletedProcess[str]:
    """Run the backstop fragment with ``workdir`` as CWD.

    The fragment reads ``.terraform/terraform.tfstate`` relative to CWD and
    calls ``exit 1`` on a non-local backend, so ``check`` is False and the
    caller asserts on ``returncode``.
    """
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("sh not available")
    if shutil.which("jq") is None:
        pytest.skip("jq not available")
    fragment = _extract_backstop_fragment()
    script = f'log() {{ echo "$@"; }}\n{fragment}\n'
    return subprocess.run(
        [sh, "-c", script],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )


def test_backstop_passes_on_local_backend(tmp_path: Path) -> None:
    """init configured a local backend → backstop passes (exit 0)."""
    tfdir = tmp_path / ".terraform"
    tfdir.mkdir()
    (tfdir / "terraform.tfstate").write_text('{"backend": {"type": "local"}}')
    result = _run_backstop(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Backend verified: local" in result.stdout


def test_backstop_fails_on_remote_backend(tmp_path: Path) -> None:
    """init configured a non-local backend → backstop fails the run."""
    tfdir = tmp_path / ".terraform"
    tfdir.mkdir()
    (tfdir / "terraform.tfstate").write_text('{"backend": {"type": "remote"}}')
    result = _run_backstop(tmp_path)
    assert result.returncode == 1
    assert "FATAL" in result.stdout
    assert "remote" in result.stdout


def test_backstop_fails_when_state_file_missing(tmp_path: Path) -> None:
    """No ``.terraform/terraform.tfstate`` at all → backstop fails (the
    backend was never configured, which is itself wrong)."""
    result = _run_backstop(tmp_path)
    assert result.returncode == 1
    assert "FATAL" in result.stdout


def test_backstop_fails_on_empty_state_file(tmp_path: Path) -> None:
    """An empty ``.terraform/terraform.tfstate`` → ``jq`` exits 0 with no
    output; the entrypoint normalises that to ``MISSING`` and the backstop
    fails safe with a sensible diagnostic."""
    tfdir = tmp_path / ".terraform"
    tfdir.mkdir()
    (tfdir / "terraform.tfstate").write_text("")
    result = _run_backstop(tmp_path)
    assert result.returncode == 1
    assert "FATAL" in result.stdout
    assert "MISSING" in result.stdout
