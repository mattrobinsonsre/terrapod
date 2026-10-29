#!/usr/bin/env python3
"""The Pulumi plugin's archive name is a protocol, and two files must agree on it.

`pulumi-provider/.goreleaser.yml` DECIDES the published filename.
`services/terrapod/services/package_cache/pulumi_plugins.py` PARSES it -- and
parses rather than interpolates, deliberately, because the filename is client
input on the way to both an upstream URL and a storage key.

So a cosmetic edit to the archive template publishes a plugin the cache cannot
parse, and the failure surfaces to an operator as a 404 **from their own
Terrapod instance** for a plugin that is sitting on our release page. Neither
file's own tests can see the other: the Go module's tests do not know about the
Python parser, and the Python tests cannot read the template at all, because
`docker/Dockerfile.test` does not copy `pulumi-provider/` into the test image.

That is why this is a docs-audit gate rather than a pytest one -- this job runs
against a full checkout.

**It checks the real pairing, not a copy of the string.** The template is read
from the YAML, rendered the way GoReleaser renders it, and matched against the
parser's OWN regex -- extracted from `pulumi_plugins.py` by `ast` rather than
re-typed here. A gate asserting both files contain the same literal would pass
while the regex changed underneath it.

**Extracted rather than imported**, and that is not a shortcut: importing
`terrapod.services.package_cache.pulumi_plugins` drags in `settings`, which
drags in SQLAlchemy and the rest of the API's dependency closure. The docs-audit
job installs a handful of packages, not that closure -- so the import version of
this gate failed in CI with `ModuleNotFoundError: No module named 'sqlalchemy'`
while passing locally, where the venv has everything. Extraction uses the same
bytes with no dependency at all, and fails loudly if the construction changes.

Run from the repository root.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

GORELEASER = pathlib.Path("pulumi-provider/.goreleaser.yml")
PARSER = pathlib.Path("services/terrapod/services/package_cache/pulumi_plugins.py")

#: Every platform the config builds, so the gate fails if the matrix and the
#: naming stop agreeing rather than only checking one lucky combination.
#: Derived from the config below; this is the floor.
MIN_PLATFORMS = 6

#: A version with a prerelease and a build component, because those are the
#: parts of semver a filename regex is most likely to refuse. GoReleaser strips
#: the leading `v` for `{{ .Version }}`, which is why the template spells it in.
SAMPLE_VERSIONS = ("1.2.3", "1.11.1", "2.0.0-rc.1", "1.0.0+build.5")


def from_parser() -> tuple[re.Pattern[str], str, str]:
    """The parser's own regex and its first-party constants, by AST.

    Adjacent string literals are concatenated by Python's parser, so the
    multi-line `re.compile(r"..." r"...")` arrives as ONE `ast.Constant` and
    needs no reassembly. Anything else -- an f-string, a variable, a computed
    pattern -- raises, which is the behaviour wanted: a construction this cannot
    read is one it cannot vouch for.
    """
    tree = ast.parse(PARSER.read_text())
    pattern: str | None = None
    consts: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id == "_FILENAME":
            call = node.value
            if not isinstance(call, ast.Call) or not call.args:
                raise ValueError("_FILENAME is not a call with arguments")
            arg = call.args[0]
            if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                raise ValueError(
                    "_FILENAME's pattern is not a single string constant, so it cannot be "
                    "extracted -- if it is now built dynamically this gate needs rewriting "
                    "rather than relaxing"
                )
            pattern = arg.value
        elif target.id in ("FIRST_PARTY_PLUGIN", "FIRST_PARTY_KIND"):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                consts[target.id] = node.value.value

    if pattern is None:
        raise ValueError(f"no _FILENAME assignment found in {PARSER}")
    for name in ("FIRST_PARTY_PLUGIN", "FIRST_PARTY_KIND"):
        if name not in consts:
            raise ValueError(f"no {name} string constant found in {PARSER}")
    return re.compile(pattern), consts["FIRST_PARTY_PLUGIN"], consts["FIRST_PARTY_KIND"]


def main() -> int:
    try:
        import yaml
    except ModuleNotFoundError:
        print("FAIL: PyYAML is required; the docs-audit job installs it", file=sys.stderr)
        return 1

    if not PARSER.is_file():
        print(f"FAIL: {PARSER} is missing", file=sys.stderr)
        return 1
    try:
        filename_re, first_party_plugin, first_party_kind = from_parser()
    except ValueError as exc:
        print(f"FAIL: cannot read the parser's pattern: {exc}", file=sys.stderr)
        return 1

    if not GORELEASER.is_file():
        print(f"FAIL: {GORELEASER} is missing", file=sys.stderr)
        return 1

    cfg = yaml.safe_load(GORELEASER.read_text())
    archives = cfg.get("archives") or []
    if len(archives) != 1:
        print(
            f"FAIL: expected exactly one archive definition, found {len(archives)} -- "
            "this gate renders one template and would silently ignore the others",
            file=sys.stderr,
        )
        return 1

    template = archives[0].get("name_template", "")
    if not template:
        print("FAIL: the archive has no name_template", file=sys.stderr)
        return 1

    build = (cfg.get("builds") or [{}])[0]
    oses = build.get("goos") or []
    arches = build.get("goarch") or []
    platforms = [(o, a) for o in oses for a in arches]
    if len(platforms) < MIN_PLATFORMS:
        print(
            f"FAIL: only {len(platforms)} platform(s) in the build matrix "
            f"({oses} x {arches}); expected at least {MIN_PLATFORMS}. Either the matrix "
            "shrank or this gate has stopped reading it.",
            file=sys.stderr,
        )
        return 1

    problems: list[str] = []
    checked = 0
    for version in SAMPLE_VERSIONS:
        for goos, goarch in platforms:
            rendered = (
                template.replace("{{ .Version }}", version)
                .replace("{{ .Os }}", goos)
                .replace("{{ .Arch }}", goarch)
                + ".tar.gz"
            )
            checked += 1
            m = filename_re.match(rendered)
            if m is None:
                problems.append(f"{rendered}: the cache's parser REFUSES this name")
                continue
            parts = m.groupdict()
            if parts["version"] != version:
                problems.append(
                    f"{rendered}: parsed version {parts['version']!r}, expected {version!r}"
                )
            if parts["name"] != first_party_plugin:
                problems.append(
                    f"{rendered}: parsed name {parts['name']!r}, so it would NOT resolve "
                    f"against the first-party base (expected {first_party_plugin!r})"
                )
            if parts["kind"] != first_party_kind:
                problems.append(
                    f"{rendered}: parsed kind {parts['kind']!r}, expected {first_party_kind!r}"
                )
            if (parts["os"], parts["arch"]) != (goos, goarch):
                problems.append(
                    f"{rendered}: parsed platform {parts['os']}-{parts['arch']}, "
                    f"expected {goos}-{goarch}"
                )

    if problems:
        print(
            "FAIL: the published plugin name and the cache's parser disagree. A plugin the "
            "cache cannot parse cannot be downloaded by any agent run, and an operator sees "
            "a 404 from their own instance for a file that is on our release page.\n",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(
            f"\nTemplate: {template!r}\n"
            "Parser:   services/terrapod/services/package_cache/pulumi_plugins.py::_FILENAME",
            file=sys.stderr,
        )
        return 1

    print(
        f"PASS — the archive template parses as a first-party plugin for all "
        f"{checked} (version, platform) combinations."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
