#!/usr/bin/env python3
"""Keep a draft security advisory per supported release in step (#1212).

The re-scan finds vulnerabilities in releases people are running. Those details
do not belong in a public issue before a fix exists, and code scanning is
branch-shaped so it cannot hold per-tag findings. A **draft** repository
security advisory can: private to maintainers, arbitrary markdown, and already
the place GitHub expects work-in-progress disclosure to live.

    rescan_advisory.py <findings-dir> <urls-out.json>

**It only ever creates and updates drafts. It never publishes.** Publishing is
irreversible and makes an unfixed vulnerability public, so it stays a deliberate
human act at patch-release time. A scheduled job holding a token that can
publish must not be one keystroke from doing it by accident.

Requires a token with `Repository security advisories: write` —
`GITHUB_TOKEN` cannot do this (verified: 403 on both valid and invalid
payloads). Failures are loud: a security channel that degrades quietly is
worse than one that is obviously broken.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
MARKER = "terrapod-release-rescan-advisory"


def _request(method: str, url: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(
            f"{method} {url} failed: HTTP {e.code}\n{detail}\n\n"
            "If this is 403, the token lacks 'Repository security advisories: "
            "write'. GITHUB_TOKEN cannot create advisories."
        ) from e


def load(directory: pathlib.Path) -> dict[str, list[dict]]:
    """release -> deduplicated findings."""
    out: dict[str, dict[str, dict]] = {}
    for path in sorted(directory.rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not (isinstance(data, dict) and "release" in data and "findings" in data):
            continue
        bucket = out.setdefault(data["release"], {})
        for f in data.get("findings") or []:
            bucket.setdefault(f"{f.get('id')}|{f.get('package')}", f)
    return {r: list(v.values()) for r, v in out.items()}


# Prose kept out of the list literals below: a wrapped string sitting next to a
# comma-separated sibling is indistinguishable from a missing comma, both to a
# reader and to static analysis (CodeQL py/implicit-string-concatenation-in-list).
_RAISED_BY = (
    "raised automatically by the release re-scan "
    "(`.github/workflows/release-rescan.yml`)."
)
_DRAFT_NOTE = (
    "This is a **draft** — private to maintainers, and not published. It becomes "
    "the public record when the patch release that fixes it ships."
)
_FIX_AVAILABLE = (
    "Everything below has a fix available upstream *now* that did not exist, or "
    "was not taken, when this release was cut."
)


def describe(release: str, findings: list[dict], code_count: int) -> str:
    lines = [
        f"<!-- {MARKER}:{release} -->",
        "",
        f"Findings in **{release}**, {_RAISED_BY}",
        "",
        _DRAFT_NOTE,
        "",
        _FIX_AVAILABLE,
        "",
        "| Severity | Package | Installed | Fixed in | Advisory |",
        "|---|---|---|---|---|",
    ]
    for f in sorted(findings, key=lambda x: (x.get("severity", ""), x.get("id", ""))):
        lines.append(
            f"| {f.get('severity', '?')} | `{f.get('package', '?')}` "
            f"| {f.get('installed', '?')} | **{f.get('fixed', '?')}** "
            f"| {f.get('id', '?')} |"
        )
    if code_count:
        note = (
            f"Plus **{code_count} code finding(s)** from static analysis of the "
            f"source at this tag — see code scanning."
        )
        lines += ["", note]
    return "\n".join(lines)


def vulnerabilities(findings: list[dict]) -> list[dict]:
    """The API's required `vulnerabilities` array, one entry per package."""
    seen: dict[str, dict] = {}
    for f in findings:
        name = f.get("package", "?")
        if name in seen:
            continue
        seen[name] = {
            "package": {"ecosystem": f.get("ecosystem", "other"), "name": name},
            "vulnerable_version_range": str(f.get("installed", "")) or None,
            "patched_versions": str(f.get("fixed", "")) or None,
        }
    return list(seen.values())


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    directory, urls_out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])

    by_release = load(directory)
    code_counts: dict[str, int] = {}
    for path in directory.rglob("*.json"):
        try:
            d = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(d, dict) and "code_findings" in d:
            code_counts[d["release"]] = code_counts.get(d["release"], 0) + int(d["code_findings"])

    if not any(by_release.values()) and not any(code_counts.values()):
        print("nothing to record")
        urls_out.write_text("{}")
        return 0

    # Token demanded only once there is something to write. A clean scan must
    # not fail for want of a credential it never needed.
    token = os.environ.get("ADVISORY_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "ADVISORY_TOKEN is empty but there are findings to record. Failing "
            "rather than silently dropping them — a security channel that "
            "degrades quietly is worse than one that is obviously broken."
        )
    repo = os.environ["GITHUB_REPOSITORY"]

    _, existing = _request("GET", f"{API}/repos/{repo}/security-advisories?per_page=100", token)
    drafts = {
        a["summary"]: a
        for a in (existing or [])
        if a.get("state") == "draft" and a.get("summary")
    }

    urls: dict[str, str] = {}
    for release in sorted(set(by_release) | set(code_counts), reverse=True):
        findings = by_release.get(release, [])
        code = code_counts.get(release, 0)
        if not findings and not code:
            continue
        summary = f"Findings in {release}"
        payload = {
            "summary": summary,
            "description": describe(release, findings, code),
            # A package entry is required even when only code findings exist,
            # so name the release itself rather than inventing a package.
            "vulnerabilities": vulnerabilities(findings) or [
                {"package": {"ecosystem": "other", "name": f"terrapod {release}"}}
            ],
            "severity": "high",
        }
        if summary in drafts:
            ghsa = drafts[summary]["ghsa_id"]
            _, adv = _request(
                "PATCH", f"{API}/repos/{repo}/security-advisories/{ghsa}", token, payload
            )
            print(f"{release}: refreshed draft {ghsa}")
        else:
            _, adv = _request(
                "POST", f"{API}/repos/{repo}/security-advisories", token, payload
            )
            print(f"{release}: created draft {adv.get('ghsa_id')}")
        urls[release] = adv.get("html_url", "")

    urls_out.write_text(json.dumps(urls, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
