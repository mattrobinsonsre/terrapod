#!/usr/bin/env python3
"""`extended-support.json` is the one place that says which release lines are
held in support beyond the version-relative window, and this holds the three
readers of that statement to each other.

Why it needs a gate at all: the window in `SECURITY.md` is *version-relative*,
so a line leaves support because newer ones shipped rather than because anyone
decided. Read literally at a MAJOR boundary, every 1.x line drops as soon as 2.x
has two minors -- silently. `release-rescan.yml`'s `discover` job could not
express the replacement either, because it takes "the N most recent minors" and
a COUNT cannot hold a line still while the majors move past it (#2193).

The support set is the newest minor, the one before it, **and any held line**:

| after | supported |
|---|---|
| 2.0.0 | 2.0.x, 1.11.x |
| 2.1.0 | 2.1.x, 2.0.x, 1.11.x |
| 2.2.0 | 2.2.x, 2.1.x, 1.11.x |

So the held line does not move as 2.x advances. Raising `minors` to 3 would buy
one release and then drop 1.11 again, one release later, for the same reason.

**An entry does NOT expire.** It is honoured for as long as it is present, and
retiring a held line is a deliberate human act -- deleting the entry. An earlier
draft of this made entries self-expiring on a sunset date, which is wrong in the
one direction that matters: a line would leave support because a date passed
with nobody deciding to drop it, which is the failure the mechanism exists to
prevent. `earliest_retirement` is therefore a **floor** on when that act may
happen, not a deadline, and `discover` deliberately does not read it.

Three things have to agree, and the second is checked by EXTRACTING the discover
job's own python and running it rather than reimplementing its logic -- a
reimplementation is wrong in a direction you cannot see:

1. the file is well formed, and every floor really is at least twelve weeks, so
   an entry cannot promise less support than the policy states;
2. `discover` honours an entry whatever its dates -- including one whose floor
   has already passed, which is the case a date-driven version would get wrong;
3. the human-readable policy names the file, so the mechanism cannot exist in a
   workflow with nothing telling an operator a line is held.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "extended-support.json"
WORKFLOW = ROOT / ".github/workflows/release-rescan.yml"

#: Surfaces that must name the file, so the policy and the mechanism cannot
#: drift apart. A reader who finds one and not the other concludes the wrong
#: thing in whichever direction they happened to look.
PROSE = (ROOT / "SECURITY.md", ROOT / "docs/versioning-and-support.md")

#: The stated commitment: a held line keeps its support for at least this long.
#: An entry whose floor is sooner would promise less than the policy, which is
#: checkable arithmetic rather than a matter of opinion.
MIN_HOLD_DAYS = 84  # twelve weeks

#: A floor on the scenarios driven below. Without it a refactor that stops the
#: extraction matching makes this file pass on nothing.
MIN_SCENARIOS = 8

#: The fixture release set every scenario runs against. Two 2.x minors, three
#: 1.x ones, plus the rolling non-semver release that must be skipped rather
#: than sorted.
TAGS = ["v2.1.0", "v2.0.3", "v1.11.4", "v1.10.9", "v1.9.2", "pricesheet"]

LONG_PAST = "2020-01-01"
FUTURE = "2099-01-01"

problems: list[str] = []
notes: list[str] = []


def extract_discover_python() -> str:
    """The `discover` job's own python, read out of the workflow.

    Taken from the PARSED yaml rather than with a regex over the raw file: a
    `run: |` block scalar strips its common indentation, so the literal text in
    the file is indented and the shell sees it dedented. Slicing the raw file
    would hand python a block that cannot compile.
    """
    wf = yaml.safe_load(WORKFLOW.read_text())
    steps = wf["jobs"]["discover"]["steps"]
    run = next((str(s.get("run", "")) for s in steps if s.get("id") == "pick"), "")
    if not run:
        raise SystemExit("FAIL: release-rescan.yml has no `discover` step with id `pick`")
    start = run.find("<<'PY'")
    if start < 0:
        raise SystemExit("FAIL: the `pick` step no longer embeds a `PY` heredoc")
    body = run[run.index("\n", start) + 1 :]
    end = re.search(r"^PY$", body, re.M)
    if not end:
        raise SystemExit("FAIL: the `PY` heredoc is not terminated")
    return body[: end.start()]


def run_discover(src: str, policy: dict, *, explicit: str = "", minors: str = "2") -> list[str]:
    """Drive the extracted block in a scratch directory and read its `tags=`."""
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "all-tags.txt").write_text("\n".join(TAGS) + "\n")
        (Path(d) / "extended-support.json").write_text(json.dumps(policy))
        (Path(d) / "discover.py").write_text(src)
        env = {**os.environ, "INPUT_TAGS": explicit, "INPUT_MINORS": minors}
        out = subprocess.run(
            [sys.executable, "-I", "discover.py"],
            cwd=d,
            env=env,
            capture_output=True,
            text=True,
            check=False,  # the returncode is inspected below, to report stderr
        )
        if out.returncode != 0:
            raise SystemExit(f"FAIL: the extracted discover block errored:\n{out.stderr}")
        line = next((ln for ln in out.stdout.splitlines() if ln.startswith("tags=")), "")
        if not line:
            raise SystemExit(f"FAIL: the extracted block printed no tags=:\n{out.stdout}")
        return json.loads(line[len("tags=") :])


def entry(line: str, held: str = LONG_PAST, floor: str = FUTURE) -> dict:
    return {
        "extended_support": [
            {
                "line": line,
                "held_since": held,
                "earliest_retirement": floor,
                "reason": "fixture",
                "announced_in": "v2.0.0",
            }
        ]
    }


# ── 1. the file is well formed, and every floor is at least the policy ────
if not POLICY.exists():
    raise SystemExit(f"FAIL: {POLICY.name} is missing — it is the one place the policy is stated")

doc = json.loads(POLICY.read_text())
if not isinstance(doc.get("extended_support"), list):
    problems.append(f"{POLICY.name}: `extended_support` must be a list")

for i, e in enumerate(doc.get("extended_support", [])):
    where = f"{POLICY.name}[{i}]"
    if not isinstance(e, dict):
        problems.append(f"{where}: each entry must be an object")
        continue
    if not re.fullmatch(r"\d+\.\d+", str(e.get("line", ""))):
        problems.append(f"{where}: `line` must be MAJOR.MINOR, got {e.get('line')!r}")
    dates: dict[str, datetime.date | None] = {}
    for field in ("held_since", "earliest_retirement"):
        raw = str(e.get(field, ""))
        try:
            dates[field] = datetime.date.fromisoformat(raw)
        except ValueError:
            dates[field] = None
            problems.append(f"{where}: `{field}` must be a UTC ISO-8601 date, got {raw!r}")
    held, floor = dates["held_since"], dates["earliest_retirement"]
    if held and floor:
        days = (floor - held).days
        if days < MIN_HOLD_DAYS:
            problems.append(
                f"{where}: the hold is {days} day(s), below the {MIN_HOLD_DAYS}-day "
                f"({MIN_HOLD_DAYS // 7}-week) commitment — an entry must not promise "
                "less support than the policy states"
            )
        if floor <= datetime.date.today():
            # Not a failure, and deliberately not one: the entry goes on being
            # honoured, which is the whole design. It is the prompt for the
            # human act that retires it.
            notes.append(
                f"{where}: line {e.get('line')} has been held since {held} and its "
                f"{MIN_HOLD_DAYS // 7}-week floor passed on {floor} — it is still supported, "
                "and retiring it means deleting this entry"
            )
    for field in ("reason", "announced_in"):
        if not str(e.get(field, "")).strip():
            problems.append(
                f"{where}: `{field}` must say something — an entry with no reason is one nobody can retire"
            )

# ── 2. `discover` honours it, driven rather than reimplemented ────────────
src = extract_discover_python()
if "extended_support" not in src:
    problems.append("release-rescan.yml's discover block no longer reads `extended_support`")
if re.search(r"\bdatetime\b|\bTODAY\b", src):
    problems.append(
        "release-rescan.yml's discover block reads a date again — an entry must be honoured "
        "for as long as it is present, because a line that left support when a date passed, "
        "with nobody deciding to drop it, is the failure this mechanism exists to prevent"
    )

scenarios = 0
if "extended_support" in src:
    baseline = run_discover(src, {"extended_support": []})
    scenarios += 1
    if baseline != ["v2.1.0", "v2.0.3"]:
        problems.append(f"with no held line, discover should pick the newest 2 minors; got {baseline}")

    held = run_discover(src, entry("1.11"))
    scenarios += 1
    if held != ["v2.1.0", "v2.0.3", "v1.11.4"]:
        problems.append(f"a held line must be scanned alongside the newest minors; got {held}")

    # THE case a date-driven version gets wrong: the floor has long passed, the
    # entry is still there, and the line is therefore still supported.
    past_floor = run_discover(src, entry("1.11", held="2020-01-01", floor="2020-04-01"))
    scenarios += 1
    if past_floor != ["v2.1.0", "v2.0.3", "v1.11.4"]:
        problems.append(
            "a held line whose earliest_retirement has PASSED must still be scanned — "
            f"retirement is a human act, not a date; got {past_floor}"
        )

    far_future = run_discover(src, entry("1.11", held="2099-01-01", floor="2099-12-31"))
    scenarios += 1
    if far_future != ["v2.1.0", "v2.0.3", "v1.11.4"]:
        problems.append(f"the dates must not gate the scan in either direction; got {far_future}")

    unreleased = run_discover(src, entry("1.5"))
    scenarios += 1
    if unreleased != baseline:
        problems.append(f"a line with no published release must contribute nothing; got {unreleased}")

    already = run_discover(src, entry("2.0"))
    scenarios += 1
    if already != baseline:
        problems.append(f"an entry already inside the newest minors must not be duplicated; got {already}")

    malformed = run_discover(src, entry("1"))
    scenarios += 1
    if malformed != baseline:
        problems.append(f"a malformed `line` must be ignored rather than crash; got {malformed}")

    explicit = run_discover(src, entry("1.11"), explicit="v1.9.2")
    scenarios += 1
    if explicit != ["v1.9.2"]:
        problems.append(f"an explicit `tags` input must still win outright; got {explicit}")

    # The shape at 2.1.0: the count gives 2.1 + 2.0 and the entry gives 1.11,
    # which is the arrangement a count alone cannot reach.
    two_held = run_discover(
        src,
        {
            "extended_support": [
                {"line": "1.11", "held_since": LONG_PAST, "earliest_retirement": FUTURE,
                 "reason": "f", "announced_in": "v2.0.0"},
                {"line": "1.10", "held_since": LONG_PAST, "earliest_retirement": FUTURE,
                 "reason": "f", "announced_in": "v2.0.0"},
            ]
        },
    )
    scenarios += 1
    if two_held != ["v2.1.0", "v2.0.3", "v1.11.4", "v1.10.9"]:
        problems.append(f"several held lines must all be scanned; got {two_held}")

if scenarios < MIN_SCENARIOS:
    problems.append(f"only {scenarios} scenarios ran, below the floor of {MIN_SCENARIOS}")

# ── 3. the human-readable policy names the file ───────────────────────────
for page in PROSE:
    if POLICY.name not in page.read_text():
        problems.append(
            f"{page.relative_to(ROOT)} does not name {POLICY.name} — the mechanism would exist "
            "in a workflow with nothing telling an operator a line is held"
        )

for n in notes:
    print(f"  note: {n}")
if problems:
    print("FAIL — extended-support policy:")
    for p in problems:
        print(f"  - {p}")
    raise SystemExit(1)
print(
    f"PASS — {POLICY.name} is well formed ({len(doc['extended_support'])} line(s) held, each with a "
    f"floor of at least {MIN_HOLD_DAYS // 7} weeks), discover honours an entry regardless of its dates "
    f"across {scenarios} driven scenario(s), and {len(PROSE)} prose surface(s) name it."
)
