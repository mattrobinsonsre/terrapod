"""Exercise the local dev package's generated module through its build command."""

import os
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def package(tmp_path):
    provider = tmp_path / "provider"
    example = provider / "examples/modules/terrapod_self_contained/main.tf"
    example.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "provider/dev-package.sh", provider / "dev-package.sh")
    shutil.copy2(ROOT / "provider/examples/modules/terrapod_self_contained/main.tf", example)

    commands = tmp_path / "bin"
    commands.mkdir()
    make = commands / "make"
    make.write_text(
        "#!/bin/sh\n"
        '[ "${FAIL_BUILD:-}" != 1 ] || exit 1\n'
        'printf "fixture provider binary\\n" > "$2/terraform-provider-terrapod"\n'
    )
    make.chmod(0o755)
    git = commands / "git"
    git.write_text(
        "#!/bin/sh\n"
        "printf 'provider/dev-package.sh\\0provider/examples/modules/terrapod_self_contained/main.tf\\0'\n"
    )
    git.chmod(0o755)
    go = commands / "go"
    go.write_text('#!/bin/sh\n[ "$1 $2" = "env GOARCH" ] && printf "amd64\\n"\n')
    go.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "PATH": f"{commands}:{os.environ['PATH']}"}

    def run(mode="build", **extra_env):
        return subprocess.run(
            ["bash", str(provider / "dev-package.sh"), mode, "1.11.1"],
            env={**env, **extra_env},
            capture_output=True,
            text=True,
            check=False,
        )

    return provider, example, run


def test_dev_package_build_generates_exact_pinned_module_without_changing_source(
    package,
):
    provider, example, run = package
    source = example.read_text()
    output = provider / ".dev-build/terrapod_self_contained-test/main.tf"

    version_only = run("version")
    assert version_only.returncode == 0, version_only.stderr
    assert not output.exists()

    built = run()
    assert built.returncode == 0, built.stderr
    version = (provider / ".dev-build/current-version").read_text().strip()
    assert re.fullmatch(r"1\.11\.1-dev\.\d+", version)
    assert version_only.stdout.strip() == version
    assert example.read_text() == source
    assert sorted(path.name for path in output.parent.iterdir()) == ["main.tf"]
    assert output.read_text() == source.replace(
        '      source = "terrapod.local/default/terrapod"\n',
        f'      source  = "terrapod.local/default/terrapod"\n      version = "{version}"\n',
    )
    rebuilt = run()
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert example.read_text() == source
    assert f'version = "{version}"' in output.read_text()
    assert output.read_text().count("version = ") == 1

    example.write_text(source + "\n# Next module version\n")
    changed = run()
    assert changed.returncode == 0, changed.stderr
    next_version = (provider / ".dev-build/current-version").read_text().strip()
    assert next_version != version
    assert example.read_text() == source + "\n# Next module version\n"
    assert f'version = "{next_version}"' in output.read_text()
    assert output.read_text().count("version = ") == 1


def test_dev_package_does_not_generate_when_build_fails(package):
    provider, _, run = package
    result = run(FAIL_BUILD="1")
    assert result.returncode != 0
    assert not (provider / ".dev-build/terrapod_self_contained-test").exists()


def test_dev_package_preserves_unexpected_files_and_existing_output(package):
    provider, _, run = package
    assert run().returncode == 0
    directory = provider / ".dev-build/terrapod_self_contained-test"
    output = directory / "main.tf"
    original = output.read_text()
    (directory / "notes.txt").write_text("do not discard")

    refused = run()
    assert refused.returncode != 0
    assert "Unexpected file" in refused.stderr
    assert output.read_text() == original
    assert (directory / "notes.txt").read_text() == "do not discard"


def test_dev_package_rejects_pinned_source_without_overwriting_output(package):
    provider, example, run = package
    assert run().returncode == 0
    directory = provider / ".dev-build/terrapod_self_contained-test"
    output = directory / "main.tf"
    original = output.read_text()
    example.write_text(
        example.read_text().replace(
            '      source = "terrapod.local/default/terrapod"\n',
            '      source  = "terrapod.local/default/terrapod"\n      version = "1.0.0"\n',
        )
    )

    refused = run()
    assert refused.returncode != 0
    assert "Expected one unpinned" in refused.stderr
    assert output.read_text() == original
    assert sorted(path.name for path in directory.iterdir()) == ["main.tf"]


def test_dev_provider_build_does_not_stamp_unrelated_git_changes():
    makefile = (ROOT / "provider/GNUmakefile").read_text()
    assert "go build -buildvcs=false -trimpath" in makefile
