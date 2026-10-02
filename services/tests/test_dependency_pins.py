"""Guard rails on dependency pins that have bitten us before.

These read pyproject.toml and assert structural invariants about a few
dependencies whose unpinned/under-pinned state has caused (or could
cause) silent framework-level breakage. They are deliberately cheap
source-introspection checks — no install, no import.
"""

from __future__ import annotations

import pathlib
import re
import tomllib

_PYPROJECT = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"


def _deps() -> dict[str, object]:
    data = tomllib.loads(_PYPROJECT.read_text())
    return data["tool"]["poetry"]["dependencies"]


def _version_spec(spec: object) -> str:
    # A dependency value is either a bare version string or a table with a
    # `version` key (e.g. uvicorn = {extras = [...], version = "..."}).
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict):
        return str(spec.get("version", ""))
    return ""


def test_starlette_is_directly_pinned_with_upper_bound() -> None:
    """starlette MUST be a direct dependency with an explicit upper bound.

    fastapi only declares a floor (``starlette>=0.46``, no ceiling), so
    without our own cap starlette floats to the latest release on every
    build — a starlette major/minor can then change framework behaviour
    with no deliberate bump on our side, and a fastapi bump can drag a new
    starlette in silently. Pinning it directly forces every starlette move
    to be an explicit, reviewed edit; a fastapi version needing a starlette
    outside our range then fails to resolve instead of swapping it quietly.
    (fastapi 0.137 + starlette 1.x is exactly the trap this guards.)

    If you are intentionally taking a new starlette, bump the pin in
    pyproject.toml — do not delete the upper bound.
    """
    deps = _deps()
    assert "starlette" in deps, (
        "starlette must be declared as a DIRECT dependency in pyproject.toml, "
        "not left as a floor-only transitive of fastapi"
    )
    spec = _version_spec(deps["starlette"])
    assert "<" in spec, f"starlette needs an explicit upper bound; got {spec!r}"


def test_fastapi_has_upper_bound() -> None:
    """fastapi must keep an explicit upper bound (it ships breaking changes
    in 0.x minors — e.g. the 0.137 include_router refactor)."""
    spec = _version_spec(_deps()["fastapi"])
    assert "<" in spec, f"fastapi needs an explicit upper bound; got {spec!r}"


# ── The committed Python locks (GHSA-rgvw-c74c-h75w) ──────────────────────────
#
# Before these, every image build resolved its own dependency closure from
# floor-only manifests, so what shipped was whatever the index served at build
# time and the pip-audit job audited a different resolution altogether. The locks
# are now committed and installed with --require-hashes; these guards check the
# properties that flag depends on, because a regeneration that lost one of them
# would otherwise only surface as a failed image build much later in CI.

#: Minimal dependency set -> the image that installs it. Keep in step with
#: MINIMAL_SETS in scripts/lock-python-deps.py.
_MINIMAL_SETS = ("listener", "migrations", "runner")

_SERVICES = _PYPROJECT.parent


def _docker_dir() -> pathlib.Path:
    """Find docker/ under either layout.

    The repository has it at ``<repo>/docker/`` with the python under
    ``<repo>/services/``, so one level UP from here. The test image flattens
    ``services/`` into ``/app`` and copies ``docker/`` beside it, so there it is
    one level DOWN. A path hard-coded for either resolves to nothing in the
    other, and a guard that cannot find its input is a guard that silently
    stopped running.
    """
    for cand in (_SERVICES.parent / "docker", _SERVICES / "docker"):
        if (cand / "Dockerfile.api").is_file():
            return cand
    raise AssertionError(
        "docker/ not found beside or above the test root — if the test image "
        "stopped copying docker/, these guards are silently not running."
    )


def _requirement_lines(path: pathlib.Path) -> list[str]:
    """Logical requirement lines, with backslash continuations joined.

    A hashed requirements file spreads one requirement over many physical lines,
    so reading it line-by-line sees ``--hash=...`` fragments as requirements and
    requirements as hash-less. Join first.
    """
    text = path.read_text().replace("\\\n", " ")
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _pinned_versions(path: pathlib.Path) -> dict[str, str]:
    """``{canonical name: version}`` for every requirement in a hashed file.

    Extras are stripped from the key. ``canonicalize_name`` does NOT strip them
    — it leaves ``sqlalchemy[asyncio]`` as it is — and poetry exports an
    extras-bearing direct dependency under its bracketed name. It happens also
    to emit a bare ``sqlalchemy==`` line, because alembic depends on it without
    extras, so keying on the bracketed form passed by luck rather than because
    it was right; ``pyjwt[crypto]`` in the same file has no bare counterpart and
    would have been reported missing.
    """
    from packaging.utils import canonicalize_name

    pins: dict[str, str] = {}
    for line in _requirement_lines(path):
        head = line.split("--hash=")[0].split(";")[0].strip()
        name, _, version = head.partition("==")
        pins[canonicalize_name(name.strip().split("[")[0])] = version.strip()
    return pins


def test_every_image_requirements_file_is_pinned_and_hashed() -> None:
    """Every line must be ``==`` pinned AND carry at least one hash.

    This is precisely what ``pip install --require-hashes`` depends on: it
    refuses a requirement that is not pinned, refuses one with no hash, and
    refuses to pull in a transitive the file does not list. A regeneration that
    dropped ``--hash`` — ``poetry export --without-hashes`` is one flag away —
    would leave the files looking entirely normal while removing the control.
    """
    for name in _MINIMAL_SETS:
        path = _SERVICES / f"requirements-{name}.txt"
        assert path.is_file(), (
            f"{path.name} is missing. docker/Dockerfile.{name} installs it with "
            "--require-hashes; regenerate with scripts/lock-python-deps.sh."
        )
        lines = _requirement_lines(path)
        assert lines, f"{path.name} has no requirements — the image would install nothing"
        for line in lines:
            head = line.split("--hash=")[0]
            assert "==" in head, f"{path.name}: {head.strip()!r} is not == pinned"
            assert "--hash=sha256:" in line, (
                f"{path.name}: {head.strip()!r} carries no hash — "
                "--require-hashes will reject the whole file"
            )


def test_image_requirements_satisfy_their_manifest_floors() -> None:
    """Each manifest's declared floors must hold in the file that image installs.

    The floors are not cosmetic: pyproject-migrations.toml pins Mako and
    cryptography forward past specific CVEs, and pyproject-runner.toml does the
    same for cryptography. Since the build no longer resolves from those floors,
    bumping one without regenerating the lock would leave the image on the
    affected version with nothing to say so. This is that nothing.
    """
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    for name in _MINIMAL_SETS:
        manifest = _SERVICES / f"pyproject-{name}.toml"
        assert manifest.is_file(), f"{manifest.name} is missing — this guard cannot run"
        declared = tomllib.loads(manifest.read_text())["project"]["dependencies"]
        pins = _pinned_versions(_SERVICES / f"requirements-{name}.txt")

        for raw in declared:
            req = Requirement(raw)
            key = canonicalize_name(req.name)
            assert key in pins, (
                f"{manifest.name} declares {req.name!r} but requirements-{name}.txt "
                "does not pin it. Regenerate with scripts/lock-python-deps.sh."
            )
            assert req.specifier.contains(pins[key], prereleases=True), (
                f"requirements-{name}.txt pins {req.name}=={pins[key]}, which does "
                f"NOT satisfy {raw!r} from {manifest.name}. The floor was bumped "
                "without regenerating the lock — run scripts/lock-python-deps.sh."
            )


def test_the_main_lock_is_committed_and_carries_hashes() -> None:
    """``services/poetry.lock`` must exist and hash every package.

    The api image exports its ``main`` group from this lock and installs the
    result with ``--require-hashes``, and the test image installs from it
    directly — both of which need a hash per file. An absent lock used to be
    invisible, because both Dockerfiles globbed it as ``poetry.lock*``.
    """
    lock = _SERVICES / "poetry.lock"
    assert lock.is_file(), (
        "services/poetry.lock is not committed. The api and test images install "
        "from it; regenerate with scripts/lock-python-deps.sh."
    )
    data = tomllib.loads(lock.read_text())
    packages = data["package"]
    assert packages, "poetry.lock lists no packages"
    unhashed = [
        p["name"]
        for p in packages
        if not any(f.get("hash", "").startswith("sha256:") for f in p.get("files", []))
    ]
    assert not unhashed, f"packages in poetry.lock carry no file hashes: {unhashed}"


def test_no_image_resolves_its_python_dependencies_at_build_time() -> None:
    """No Dockerfile may resolve a dependency closure, and every ``-r`` is hashed.

    The source-level form of the fix, so it cannot be quietly undone. Three
    shapes are banned: ``poetry lock`` (resolves at build time), ``pip install .``
    of a dependency manifest (ditto, from floors), and ``pip install -r`` without
    ``--require-hashes`` (installs a lock without verifying it). Each one reads as
    an ordinary build step, and each one reopens GHSA-rgvw-c74c-h75w.
    """
    docker = _docker_dir()
    dockerfiles = sorted(docker.glob("Dockerfile.*"))
    assert dockerfiles, f"no Dockerfiles found in {docker} — guard is inert"

    offences: list[str] = []
    for path in dockerfiles:
        # Join line continuations: every one of these appears mid-RUN.
        text = path.read_text().replace("\\\n", " ")
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("#"):
                continue
            if "poetry lock" in line:
                offences.append(f"{path.name}: resolves at build time: {line!r}")
            install = re.search(r"pip install\b([^|&;]*)", line)
            if not install:
                continue
            args = install.group(1)
            if re.search(r"\s-r\s", args) and "--require-hashes" not in args:
                offences.append(f"{path.name}: -r install without --require-hashes: {line!r}")
            # A bare `.` target builds and installs the adjacent pyproject,
            # resolving its whole closure from floors — what the three minimal
            # images used to do.
            if re.search(r"(?:^|\s)\.(?:\s|$)", args):
                offences.append(f"{path.name}: installs a manifest from floors: {line!r}")
    assert not offences, "\n".join(offences)
