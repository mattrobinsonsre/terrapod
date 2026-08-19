"""Replace SARIF-derived code counts with OPEN code-scanning alert counts (#1375).

The re-scan uploads each release's Semgrep results to code scanning, then
reports how many code findings that release has. It used to report the number
of results in the SARIF -- a number that knows nothing about triage.

Dismissing an alert is how a code finding is accepted; it is the counterpart of
`.trivyignore` for dependencies. Counting the SARIF meant dismissal had no
effect, so a release whose findings had all been reviewed and accepted kept
raising "findings that need attention" on every scan, pointing at a code
scanning view that showed nothing (its default filter is `state=open`). The
issue could not be discharged by any action short of changing the source.

So: ask the API what is still open, and rewrite the counts in the artifacts the
report is built from.

FAILS CLOSED. If the API cannot be reached, or a release's analysis has not
been processed yet (SARIF ingestion is asynchronous), the existing SARIF count
is left untouched. A spurious issue is a nuisance; a missed finding is not.

    rescan_open_alerts.py <findings-dir> <tags-json>
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys


def open_alert_count(tag: str) -> int | None:
    """Alerts still open for a release tag, or None if that cannot be determined."""
    ref = f"refs/tags/{tag}"
    try:
        out = subprocess.run(
            [
                "gh",
                "api",
                f"/repos/{{owner}}/{{repo}}/code-scanning/alerts?ref={ref}&state=open&per_page=100",
                "--jq",
                "length",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"{tag}: could not query alerts ({e}) — keeping the SARIF count")
        return None

    if out.returncode != 0:
        # A 404 here means no analysis for that ref, which is NOT the same as
        # "no findings": it is usually ingestion still in flight.
        print(f"{tag}: alert query failed ({out.stderr.strip()[:120]}) — keeping the SARIF count")
        return None
    try:
        return int(out.stdout.strip())
    except ValueError:
        print(f"{tag}: unparseable alert count {out.stdout.strip()!r} — keeping the SARIF count")
        return None


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    directory = pathlib.Path(sys.argv[1])
    try:
        tags = json.loads(sys.argv[2])
    except json.JSONDecodeError:
        print("tags argument is not JSON — keeping every SARIF count", file=sys.stderr)
        return 0

    counts: dict[str, int] = {}
    for tag in tags:
        n = open_alert_count(str(tag))
        if n is not None:
            counts[str(tag)] = n
            print(f"{tag}: {n} open code alert(s)")

    if not counts:
        return 0

    for path in directory.rglob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not (isinstance(data, dict) and "code_findings" in data):
            continue
        release = str(data.get("release", ""))
        if release not in counts:
            continue
        was, now = int(data["code_findings"]), counts[release]
        if was != now:
            print(f"{path.name}: {release} code_findings {was} -> {now} (open only)")
        data["code_findings"] = now
        path.write_text(json.dumps(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
