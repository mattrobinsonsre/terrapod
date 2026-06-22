"""Unit tests for the Terragrunt runner phase (#534).

The load-bearing piece is the tf-wrapper: when Terragrunt invokes it as the
terraform binary, it must drop Terrapod's local-backend override into the tofu
working dir and then exec the real binary with the original args. These tests
exercise that wrapper directly (with a fake "real" binary) so the
backend-reconciliation logic is proven without a live terragrunt/tofu.
"""

import os
import subprocess
import sys
from pathlib import Path

from terrapod.runner.phases.terragrunt import _OVERRIDE_NAME, write_wrappers


def _fake_real_tf(tmp_path: Path) -> Path:
    """A stand-in 'real' tofu binary that records the argv it was exec'd with."""
    record = tmp_path / "argv.txt"
    script = tmp_path / "fake-tofu"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"open({str(record)!r}, 'w').write(' '.join(sys.argv[1:]))\n"
    )
    script.chmod(0o755)
    return script


def test_tf_wrapper_drops_local_backend_override_and_execs_real(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    real = _fake_real_tf(tmp_path)
    tg_wrapper = write_wrappers(
        terragrunt_bin="/nonexistent/terragrunt", real_tf_bin=real, dest_dir=bin_dir
    )
    tf_wrapper = bin_dir / "tp-tf-wrapper"
    assert tg_wrapper.exists() and tf_wrapper.exists()
    assert os.access(tf_wrapper, os.X_OK)

    # Run the tf-wrapper from a fresh "tofu working dir" as Terragrunt would.
    workdir = tmp_path / "cache" / "module"
    workdir.mkdir(parents=True)
    subprocess.run(
        [sys.executable, str(tf_wrapper), "init", "-input=false"],
        cwd=workdir,
        check=True,
        timeout=30,
    )

    # The local-backend override landed in the working dir...
    override = workdir / _OVERRIDE_NAME
    assert override.exists()
    assert 'backend "local"' in override.read_text()
    # ...and the real binary was exec'd with the original args.
    assert (tmp_path / "argv.txt").read_text() == "init -input=false"


def test_tf_wrapper_honors_chdir_flag(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    real = _fake_real_tf(tmp_path)
    write_wrappers(terragrunt_bin="/x/terragrunt", real_tf_bin=real, dest_dir=bin_dir)
    tf_wrapper = bin_dir / "tp-tf-wrapper"

    # When tofu is invoked with -chdir=<dir>, the override must land THERE,
    # not in the launcher's CWD.
    chdir_target = tmp_path / "elsewhere"
    chdir_target.mkdir()
    launch_cwd = tmp_path / "launch"
    launch_cwd.mkdir()
    subprocess.run(
        [sys.executable, str(tf_wrapper), f"-chdir={chdir_target}", "plan"],
        cwd=launch_cwd,
        check=True,
        timeout=30,
    )
    assert (chdir_target / _OVERRIDE_NAME).exists()
    assert not (launch_cwd / _OVERRIDE_NAME).exists()


def test_tg_wrapper_invokes_terragrunt_with_tf_path(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    # Fake 'terragrunt' that records its argv so we can assert --tf-path wiring.
    record = tmp_path / "tg_argv.txt"
    fake_tg = tmp_path / "fake-terragrunt"
    fake_tg.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"open({str(record)!r}, 'w').write(' '.join(sys.argv[1:]))\n"
    )
    fake_tg.chmod(0o755)

    write_wrappers(terragrunt_bin=fake_tg, real_tf_bin="/x/tofu", dest_dir=bin_dir)
    tg_wrapper = bin_dir / "tp-tg-wrapper"
    tf_wrapper = bin_dir / "tp-tf-wrapper"

    subprocess.run(
        [sys.executable, str(tg_wrapper), "plan", "-input=false"],
        check=True,
        timeout=30,
    )
    argv = record.read_text()
    # terragrunt was called with --tf-path=<tf-wrapper> then the subcommand.
    assert argv == f"--tf-path {tf_wrapper} plan -input=false"
