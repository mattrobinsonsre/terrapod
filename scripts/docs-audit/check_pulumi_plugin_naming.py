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
from the YAML, rendered the way GoReleaser renders it, and fed to the actual
parser. A test asserting both files contain the same literal would pass while
the parser's regex had changed underneath it.

Run from the repository root.
"""

from __future__ import annotations

import pathlib
import sys

GORELEASER = pathlib.Path("pulumi-provider/.goreleaser.yml")

#: Every platform the config builds, so the gate fails if the matrix and the
#: naming stop agreeing rather than only checking one lucky combination.
#: Derived from the config below; this is the floor.
MIN_PLATFORMS = 6

#: A version with a prerelease and a build component, because those are the
#: parts of semver a filename regex is most likely to refuse. GoReleaser strips
#: the leading `v` for `{{ .Version }}`, which is why the template spells it in.
SAMPLE_VERSIONS = ("1.2.3", "1.11.1", "2.0.0-rc.1", "1.0.0+build.5")


def main() -> int:
    sys.path.insert(0, "services")
    try:
        import yaml
    except ModuleNotFoundError:
        print("FAIL: PyYAML is required; the docs-audit job installs it", file=sys.stderr)
        return 1
    from terrapod.services.package_cache import pulumi_plugins

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
            parts = pulumi_plugins.parse_filename(rendered)
            if parts is None:
                problems.append(f"{rendered}: the cache's parser REFUSES this name")
                continue
            if parts["version"] != version:
                problems.append(
                    f"{rendered}: parsed version {parts['version']!r}, expected {version!r}"
                )
            if parts["name"] != pulumi_plugins.FIRST_PARTY_PLUGIN:
                problems.append(
                    f"{rendered}: parsed name {parts['name']!r}, so it would NOT resolve "
                    f"against the first-party base (expected {pulumi_plugins.FIRST_PARTY_PLUGIN!r})"
                )
            if parts["kind"] != pulumi_plugins.FIRST_PARTY_KIND:
                problems.append(
                    f"{rendered}: parsed kind {parts['kind']!r}, expected "
                    f"{pulumi_plugins.FIRST_PARTY_KIND!r}"
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
