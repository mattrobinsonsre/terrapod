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
    # Answers every `go env` key the script reads, not just the one it needed
    # when this fixture was written: `artifact_version` reads GOVERSION (#2461)
    # and `bundle` reads GOARCH. A stub that enumerates one key makes every
    # build-mode test fail the moment the script consults another.
    # FIXTURE_GOVERSION lets a test vary the toolchain through `run`'s existing
    # env hook, which is what makes the version-moves property drivable.
    go.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '  "env GOARCH") printf "amd64\\n" ;;\n'
        '  "env GOVERSION") printf "%s\\n" "${FIXTURE_GOVERSION:-go1.27.3}" ;;\n'
        "esac\n"
    )
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
    """Asserted on the RECIPE LINE, not as a substring of the file (#2425 §2.6).

    `"go build -buildvcs=false -trimpath" in makefile` is satisfied by the
    string sitting in a comment -- including the comment that explains why the
    flag is there -- so deleting the flag from the command and leaving the
    rationale above it was green. Without `-buildvcs=false` the binary is
    stamped with the repository's VCS state, so an unrelated dirty file changes
    its bytes and `dev-package` produces a different artifact for the same
    provider source, which is the whole thing this guards.

    A make RECIPE is a line indented with a TAB under a target; a comment starts
    with `#`. Both are exact, so this needs no heuristic.
    """
    makefile = (ROOT / "provider/GNUmakefile").read_text()
    recipes = [
        line.lstrip("\t")
        for line in makefile.splitlines()
        if line.startswith("\t") and not line.lstrip("\t").startswith("#")
    ]
    # A floor, so a reformatted Makefile fails here rather than passing on an
    # empty subject.
    assert len(recipes) >= 3, f"found only {recipes} recipe lines; the reader has drifted"
    builds = [line for line in recipes if line.startswith("go build")]
    assert builds, f"no `go build` recipe line at all; recipes are {recipes}"
    for line in builds:
        for flag in ("-buildvcs=false", "-trimpath"):
            assert flag in line, (
                f"the `go build` recipe is missing {flag}: {line!r}. Without "
                "`-buildvcs=false` an unrelated dirty file changes the binary's bytes, "
                "so the same provider source produces a different artifact."
            )


def test_the_dev_version_moves_when_only_the_go_toolchain_changes(package):
    """#2461: the toolchain is an input to the binary's bytes.

    `provider/go.mod` is already in the digest, but its `go` directive is a
    MINIMUM rather than a pin, so a host Go upgrade inside the declared range
    changes the compiled bytes and leaves every digested file identical. Before
    the fix the derived version did not move, `bundle` refused to overwrite a
    stored artifact whose bytes differed, and the local dev stack stopped
    building -- with a refusal naming neither the toolchain nor the cause.

    Driven through the real script with a stubbed toolchain rather than by
    reading it: the property is what `go env GOVERSION` does to the output.
    """
    _, _, run = package

    first = run("version", FIXTURE_GOVERSION="go1.27.0")
    second = run("version", FIXTURE_GOVERSION="go1.27.3")
    third = run("version", FIXTURE_GOVERSION="go1.28.1")

    for result in (first, second, third):
        assert result.returncode == 0, result.stderr

    versions = {first.stdout.strip(), second.stdout.strip(), third.stdout.strip()}
    assert len(versions) == 3, (
        f"three toolchains produced {len(versions)} distinct version(s): {versions}. "
        "The toolchain is not reaching the digest."
    )
    # Still a well-formed dev version, not merely a different string.
    for version in versions:
        assert re.fullmatch(r"1\.11\.1-dev\.\d+", version), version


def test_the_dev_version_is_stable_for_one_toolchain(package):
    """The companion the moves-property needs to mean anything.

    Without this, "always include the toolchain" would be satisfied by mixing a
    nonce into the digest -- which also makes every version differ, and would
    make `bundle`'s refusal unreachable by producing a new version on every
    single invocation.
    """
    _, _, run = package

    runs = [run("version", FIXTURE_GOVERSION="go1.27.3") for _ in range(3)]
    for result in runs:
        assert result.returncode == 0, result.stderr

    versions = {result.stdout.strip() for result in runs}
    assert len(versions) == 1, f"the same toolchain and source gave {versions}"


def test_the_source_still_decides_the_version(package):
    """The toolchain is folded in ALONGSIDE the per-file hashes, not instead.

    A fold that replaced the file hashes would pass the two tests above while
    making the version blind to the provider's own source, which is the thing it
    was tracking in the first place.
    """
    _, example, run = package

    before = run("version", FIXTURE_GOVERSION="go1.27.3")
    assert before.returncode == 0, before.stderr

    example.write_text(example.read_text() + "\n# a source change\n")

    after = run("version", FIXTURE_GOVERSION="go1.27.3")
    assert after.returncode == 0, after.stderr
    assert before.stdout.strip() != after.stdout.strip(), (
        "a source edit left the version unchanged; the per-file hashes are no "
        "longer reaching the digest"
    )


def test_an_absent_go_is_refused_by_name_rather_than_defaulted(package):
    """A fallback would silently stop covering the toolchain -- which IS the bug.

    So the absence is a refusal. It names `go` and says why the dev version
    needs it, because the Tiltfile calls this subcommand while EVALUATING: a
    Tiltfile that cannot evaluate builds nothing while an already-running API
    goes on answering 200, which reads as a wedged stack rather than as a
    missing prerequisite.

    This adds no prerequisite -- `bundle` already reads `go env GOARCH` and the
    build compiles -- it only moves the failure earlier and makes it legible.
    """
    provider, _, run = package
    # The fixture PREPENDS its stub directory to the inherited PATH, so removing
    # the stub alone just uncovers the real `go` behind it. Build a PATH that has
    # every other tool `version` needs and no `go` anywhere: `sha256sum` here is
    # GNU coreutils rather than a BSD builtin, so a minimal PATH of /usr/bin
    # would fail for the wrong reason.
    commands = provider.parent / "bin"
    (commands / "go").unlink()
    tools = provider.parent / "real-tools"
    tools.mkdir()
    # `bash` is here because subprocess resolves the interpreter through this
    # same PATH; the stubs' own shebangs are absolute and need nothing.
    for tool in ("bash", "sha256sum", "sort", "xargs", "dirname", "realpath"):
        found = shutil.which(tool)
        assert found, f"{tool} is not on PATH; this test cannot build its fixture"
        (tools / tool).symlink_to(found)
    assert shutil.which("go", path=f"{commands}:{tools}") is None

    result = run("version", PATH=f"{commands}:{tools}")

    assert result.returncode != 0, f"expected a refusal, got: {result.stdout!r}"
    assert "go is not on PATH" in result.stderr, result.stderr
    assert not re.search(r"\d+\.\d+\.\d+", result.stdout), (
        f"a version was printed despite the refusal: {result.stdout!r}"
    )
