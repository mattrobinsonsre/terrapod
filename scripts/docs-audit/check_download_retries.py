#!/usr/bin/env python3
"""Every workflow step that downloads from a remote host retries (#2425).

AGENTS.md makes bounded backoff/retry a **hard requirement** for every outbound
call, and the reason is not theoretical politeness: a download step with no
retry fails a pull request on somebody else's outage, with a message that blames
whatever the step was fetching.

That happened three times in one day. A GitHub release-CDN incident returned 504
to every `releases/download/` URL, and it took out `Python checks` (the test
image's OPA), three `Pulumi SDK` legs (the Pulumi CLI) and `Go modules`
(golangci-lint) — none of which had anything to do with the diff under review.
Worse, `check-platform-tool-artifacts.py` reported a published trivy release as
**unpublished**, sending the reader to fix a version pin that was fine.

`docker/Dockerfile.test-base` already had the idiom
(`--retry 6 --retry-delay 4 --retry-all-errors`), so the repository knew the
answer; seven workflow sites simply did not use it.

## What counts as remote, and why the distinction is the whole gate

A `curl` of `127.0.0.1`, `localhost` or the dev hostname is a **poll against a
stack this job just started** — usually inside its own `until … sleep` loop,
which is a retry by another name. Requiring `--retry` there would flag a dozen
correct steps, and a gate whose findings are mostly false positives is a gate
that gets muted. So the subject is a curl naming a host this job does not
control.

`${{ … }}` and `$VAR` hosts are treated as remote when the surrounding text
names a real one, and skipped otherwise: an interpolated host cannot be resolved
here, and guessing in either direction is worse than saying so.
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

#: A floor, so a matcher that stops matching fails rather than passing on
#: nothing. Seven sites were fixed when this was written; the floor sits below
#: that because a step may legitimately be removed.
MIN_DOWNLOADS = 5

#: Hosts a job serves itself. A curl of one of these is a readiness poll, not a
#: download — see the module docstring.
LOCAL_HOST = re.compile(
    r"^(localhost|127\.\d+\.\d+\.\d+|\[::1\]|0\.0\.0\.0|web|api|"
    r"terrapod\.(local|test)|\$host|\$\{?host)",
    re.I,
)

_CURL = re.compile(r"\bcurl\b")
_URL = re.compile(r"https?://([A-Za-z0-9._${}:-]+)")
_RETRY = re.compile(r"--retry\b")

#: `(run body, expected remote-host verdict)` — asserted BEFORE the scan,
#: because what this can get wrong is the local/remote split, and that produces
#: a silently smaller subject rather than an error.
SELF_TEST: list[tuple[str, bool]] = [
    ("curl -fsSLo x https://github.com/o/r/releases/download/v1/x", True),
    ('curl -fsSL -o x.tgz \\\n  "https://github.com/o/r/releases/download/v${v}/x"', True),
    ("curl -fsSLo k https://kind.sigs.k8s.io/dl/v0.32.0/kind-linux-amd64", True),
    ("curl -sSfL -o i.sh https://raw.githubusercontent.com/a/b/c/install.sh", True),
    # Readiness polls — not downloads, and each is already in a retry loop.
    ("until curl -fsS http://localhost:3000/health; do sleep 2; done", False),
    ('curl -fsS -o /dev/null "http://127.0.0.1:18080/login" && break', False),
    ('code=$(curl -s -o /dev/null -w "%{http_code}" "https://$host/login")', False),
    ("curl -fsS http://web:3000/ready", False),
    # No curl at all.
    ("docker pull ghcr.io/o/r:tag", False),
]


def remote_hosts(run: str) -> set[str]:
    """The hosts this step curls that it does not serve itself."""
    if not _CURL.search(run):
        return set()
    return {h for h in _URL.findall(run) if not LOCAL_HOST.match(h)}


def self_test() -> list[str]:
    out = []
    for body, want in SELF_TEST:
        got = bool(remote_hosts(body))
        if got is not want:
            out.append(f"classified {body!r} as remote={got}, want {want}")
    return out


def main() -> int:
    problems = self_test()
    if problems:
        print("FAIL: the local/remote classifier is wrong before it is used.\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    missing: list[str] = []
    checked = 0
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        data = yaml.safe_load(wf.read_text())
        for jid, job in (data.get("jobs") or {}).items():
            for i, step in enumerate(job.get("steps") or []):
                run = str(step.get("run") or "")
                hosts = remote_hosts(run)
                if not hosts:
                    continue
                checked += 1
                if not _RETRY.search(run):
                    name = step.get("name") or f"step {i}"
                    missing.append(
                        f"{wf.name}:{jid}: {name} downloads from "
                        f"{', '.join(sorted(hosts))} with no --retry"
                    )

    for line in (f"  ok  {checked} remote download step(s) carry --retry",):
        print(line)

    if checked < MIN_DOWNLOADS:
        print(
            f"FAIL: only {checked} remote download steps found, expected at least "
            f"{MIN_DOWNLOADS}. Either the workflows stopped downloading things or "
            f"this check has stopped recognising them — it must not pass by "
            f"checking nothing.",
            file=sys.stderr,
        )
        return 1

    if missing:
        print(f"\nFAIL: {len(missing)} download step(s) have no retry.\n", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        print(
            "\nAdd the idiom the repository already uses in "
            "docker/Dockerfile.test-base:\n"
            "  curl --retry 6 --retry-delay 4 --retry-all-errors ...\n"
            "Without it a publisher's outage fails this pull request with a "
            "message blaming whatever the step was fetching. If the URL is "
            "actually a stack this job serves itself, it belongs in LOCAL_HOST "
            "rather than here.",
            file=sys.stderr,
        )
        return 1

    print("check_download_retries: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
