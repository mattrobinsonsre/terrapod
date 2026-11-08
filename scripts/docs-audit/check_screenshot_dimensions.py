#!/usr/bin/env python3
"""Every committed Terrapod screenshot is the same shape.

A README or a docs page stacks these one above another, so a set captured at
one width and a dozen different heights reads as a pile of offcuts -- the tall
ones dominate and the short ones look like strips. That is not a hypothetical:
a single capture run shipped 31 images at 2560 wide with heights from 640 to
1800, because the framing had quietly moved from the viewport to the content
height. Nothing noticed until a human looked at the rendered page.

So the rule is: a desktop capture is EXACTLY the capture viewport, and a phone
capture is portrait and narrower than it.

THERE IS NO EXEMPTION LEDGER, deliberately. The set this applies to is derived
from `scripts/demo/capture.mjs` -- the thing that produces them -- by reading
its own shot registrations. An image in `docs/images/` that the capture script
does not produce is somebody else's UI (the GitHub App setup walkthrough, the
Slack app icon) and is none of this check's business; a ledger would have to
list those by hand and would go stale the first time one was added.

SELF_TEST IS WHAT MAKES THE DERIVATION CHECKABLE (#2425 §2.7). Deriving the set
from the script is right, and it means the gate has two extractors and a
comparison, none of which the floors can speak for: a floor proves the derived
sets are non-empty, and cannot tell a capture script that stopped registering
shots from an extractor that stopped reading them. Only one of those is this
gate's problem, and it is the one that fails silently -- a narrowed shot matcher
leaves an image unchecked while the gate reports OK on the rest. So each
spelling is pinned against a literal before the scan runs, including spellings
that must NOT be read, and the comparison is fixtured at a viewport that is
deliberately not the real one: a size written into the comparison would pass a
fixture at the real size and fail here, which is the whole claim this gate makes
about tracking `capture.mjs` rather than a number in a file.

One documented gap, backstopped by the floor rather than gated: the shot matcher
reads a SINGLE-quoted name, which is what `capture.mjs` uses throughout. A file
converted wholesale to double quotes drops every name and fails the floor; a
single line converted drops one image from checking and nothing says so. Widen
the pattern if that day comes -- the derived set on this tree is identical
either way, so there is nothing to preserve.

STDLIB ONLY: this runs in the `docs-audit` job alongside the other gates, and
PNG dimensions are 8 bytes at a fixed offset, so there is nothing to install.
"""

from __future__ import annotations

import pathlib
import re
import struct
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
CAPTURE = ROOT / "scripts" / "demo" / "capture.mjs"
IMAGES = ROOT / "docs" / "images"

#: A floor on each derived set, so a matcher that stops matching fails here
#: rather than passing on nothing -- the failure mode every derived gate in
#: this directory is built to refuse. The floors bound TOTAL breakage only;
#: SELF_TEST_* below is what pins each individual spelling, because a count
#: over the script cannot distinguish a shot that was retired from a spelling
#: the extractor stopped reading.
MIN_DESKTOP = 20
MIN_PHONE = 2

VIEWPORT_RE = re.compile(r"const VIEWPORT\s*=\s*\{\s*width:\s*(\d+),\s*height:\s*(\d+)\s*\}")
#: The helpers that push onto `shots`/`anonShots`, and the one that pushes onto
#: `phoneShots`. A registration names its shot with a string literal; a call
#: inside a helper's own definition passes the name through as a variable and
#: registers nothing readable, which is why the literal is part of the pattern.
DESKTOP_HELPERS = ("shot", "anonShot", "page_", "wsTab")
PHONE_HELPER = "phoneShot"

#: Literal spellings the viewport reader must read, and must not. `None` means
#: it has to REFUSE rather than guess: the keys are read positionally, so a
#: reordered or computed literal is one this gate cannot vouch for, and it says
#: to teach the extractor instead of hardcoding the numbers.
SELF_TEST_VIEWPORT: list[tuple[str, tuple[int, int] | None]] = [
    ("const VIEWPORT = { width: 1440, height: 900 }", (1440, 900)),
    ("const VIEWPORT={width:800,height:600}", (800, 600)),
    ("const VIEWPORT =\n  { width: 1280,\n    height: 720 }", (1280, 720)),
    # Must NOT be read as the desktop viewport: a differently-named const that
    # merely ends in the word, a computed value, and a reordered literal.
    ("const PHONE_VIEWPORT = { width: 412, height: 915 }", None),
    ("const VIEWPORT = devices['Pixel 7'].viewport", None),
    ("const VIEWPORT = { height: 900, width: 1440 }", None),
]

#: One fixture per registration spelling, plus the lines that must register
#: nothing. The helper DEFINITIONS and the generic calls inside them are the
#: must-not-match half: `capture.mjs` contains both, so a matcher that read
#: them would invent shot names that no image can ever satisfy.
SELF_TEST_SHOTS: list[tuple[str, set[str], set[str]]] = [
    ("shot('run-detail', async (page, ws, run) => {", {"run-detail"}, set()),
    ("anonShot('login', '/login', 'input[type=\"password\"]')", {"login"}, set()),
    ("page_('labels-browser', '/labels')", {"labels-browser"}, set()),
    ("wsTab('workspace-state', 'state')", {"workspace-state"}, set()),
    ("phoneShot('mobile-workspaces', async (page) => {", set(), {"mobile-workspaces"}),
    ("  shot('indented-is-fine', async () => {", {"indented-is-fine"}, set()),
    ("const shot = (name, fn) => shots.push({ name, fn })", set(), set()),
    ("  shot(name, async (page) => {", set(), set()),
    ("// shot('retired-shot', async () => {", set(), set()),
]

#: Deliberately NOT the real viewport. A comparison with the real size written
#: into it would pass a fixture at the real size and fail here, so this is what
#: pins that the numbers come out of `capture.mjs`.
FIXTURE_VIEWPORT = (1234, 567)

#: (size, substring the problem must name) -- `None` for "no problem".
SELF_TEST_DESKTOP: list[tuple[tuple[int, int], str | None]] = [
    ((1234, 567), None),
    # Exactly the #2334 shape: right width, framed to content height.
    ((1234, 568), "1234x568"),
    ((1234, 1800), "1234x1800"),
    # The right aspect ratio at the wrong size is still the wrong size.
    ((2468, 1134), "2468x1134"),
]

#: (size, substrings every problem list must name).
SELF_TEST_PHONE: list[tuple[tuple[int, int], list[str]]] = [
    ((412, 915), []),
    ((1234, 2000), ["not narrower"]),
    ((2000, 3000), ["not narrower"]),
    ((500, 400), ["not portrait"]),
    ((1300, 600), ["not narrower", "not portrait"]),
]


def png_size(path: pathlib.Path) -> tuple[int, int] | None:
    """(width, height) from the IHDR, or None if it is not a PNG."""
    data = path.read_bytes()[:24]
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", data[16:24])


def viewport(src: str) -> tuple[int, int] | None:
    """The capture viewport, or None when the literal cannot be read."""
    m = VIEWPORT_RE.search(src)
    return (int(m.group(1)), int(m.group(2))) if m else None


def shot_names(src: str) -> tuple[set[str], set[str]]:
    """(desktop, phone) shot names, read off their own registrations."""
    desktop: set[str] = set()
    for helper in DESKTOP_HELPERS:
        desktop |= set(re.findall(rf"^\s*{re.escape(helper)}\(\s*'([^']+)'", src, re.M))
    phone = set(re.findall(rf"^\s*{re.escape(PHONE_HELPER)}\(\s*'([^']+)'", src, re.M))
    return desktop, phone


def desktop_problem(name: str, size: tuple[int, int], vw: int, vh: int) -> str | None:
    """A desktop capture is EXACTLY the viewport, or it is a problem."""
    if size == (vw, vh):
        return None
    return (
        f"{name}.png is {size[0]}x{size[1]}, not the capture viewport "
        f"{vw}x{vh}. Re-run: node scripts/demo/capture.mjs "
        f"--host <host> --only {name}"
    )


def phone_problems(name: str, size: tuple[int, int], vw: int) -> list[str]:
    """A phone capture is portrait and narrower than the desktop capture."""
    w, h = size
    out: list[str] = []
    if w >= vw:
        out.append(
            f"{name}.png is {w}px wide, which is not narrower than the "
            f"{vw}px desktop capture -- it is meant to show the phone"
        )
    if h <= w:
        out.append(f"{name}.png is {w}x{h}, which is not portrait")
    return out


def self_test() -> list[str]:
    """Pin every reader and the comparison against literals, before the scan.

    Asserted FIRST on purpose. A narrowed extractor otherwise reports OK over
    whatever it can still see, which is the exact shape of "a gate that stopped
    being able to fail"; here it says which spelling it stopped reading.
    """
    out: list[str] = []
    for src, expected in SELF_TEST_VIEWPORT:
        got = viewport(src)
        if got != expected:
            out.append(
                f"the viewport reader no longer reads {src!r} the way it must: "
                f"expected {expected}, read {got}. Widen the pattern rather than "
                "the fixture, and never hardcode the size here."
            )
    for src, want_desktop, want_phone in SELF_TEST_SHOTS:
        got_desktop, got_phone = shot_names(src)
        if (got_desktop, got_phone) != (want_desktop, want_phone):
            out.append(
                f"the shot matcher no longer reads {src!r} the way it must: expected "
                f"desktop={sorted(want_desktop)} phone={sorted(want_phone)}, read "
                f"desktop={sorted(got_desktop)} phone={sorted(got_phone)}. A spelling "
                "this gate cannot see is an image it does not check."
            )
    vw, vh = FIXTURE_VIEWPORT
    for size, want in SELF_TEST_DESKTOP:
        got = desktop_problem("fixture", size, vw, vh)
        ok = got is None if want is None else (got is not None and want in got)
        if not ok:
            out.append(
                f"the desktop comparison no longer judges {size} against a "
                f"{vw}x{vh} viewport correctly: expected "
                f"{'no problem' if want is None else want!r}, got {got!r}. If this "
                "passes at the real viewport and fails here, a size has been written "
                "into the comparison instead of read out of capture.mjs."
            )
    for size, wants in SELF_TEST_PHONE:
        got = phone_problems("fixture", size, vw)
        if len(got) != len(wants) or not all(any(w in g for g in got) for w in wants):
            out.append(
                f"the phone comparison no longer judges {size} against a {vw}px "
                f"desktop capture correctly: expected {wants}, got {got}"
            )
    return out


def main() -> int:
    blockers = self_test()
    if blockers:
        print("check_screenshot_dimensions cannot do its job:\n")
        for b in blockers:
            print(f"  - {b}")
        return 1

    if not CAPTURE.exists():
        sys.exit(f"FAIL: {CAPTURE.relative_to(ROOT)} is missing; this gate reads it")
    src = CAPTURE.read_text()

    size = viewport(src)
    if size is None:
        sys.exit(
            "FAIL: could not read VIEWPORT out of capture.mjs. Teach this "
            "extractor the new shape -- do NOT hardcode the numbers here, or "
            "the gate stops tracking the script it exists to enforce."
        )
    vw, vh = size

    desktop, phone = shot_names(src)

    problems: list[str] = []
    if len(desktop) < MIN_DESKTOP:
        problems.append(
            f"only {len(desktop)} desktop shot(s) matched, below the floor of "
            f"{MIN_DESKTOP}. The shot matcher has stopped matching; fix it "
            f"rather than lowering the floor."
        )
    if len(phone) < MIN_PHONE:
        problems.append(f"only {len(phone)} phone shot(s) matched, below the floor of {MIN_PHONE}.")

    for name in sorted(desktop):
        f = IMAGES / f"{name}.png"
        if not f.exists():
            problems.append(f"{name}.png is registered in capture.mjs but not committed")
            continue
        got = png_size(f)
        if got is None:
            problems.append(f"{name}.png is not a PNG")
            continue
        problem = desktop_problem(name, got, vw, vh)
        if problem:
            problems.append(problem)

    for name in sorted(phone):
        f = IMAGES / f"{name}.png"
        if not f.exists():
            problems.append(f"{name}.png is registered in capture.mjs but not committed")
            continue
        got = png_size(f)
        if got is None:
            problems.append(f"{name}.png is not a PNG")
            continue
        problems.extend(phone_problems(name, got, vw))

    if problems:
        print(f"check_screenshot_dimensions: {len(problems)} problem(s)\n")
        for p in problems:
            print(f"  {p}")
        print(
            f"\n  Every desktop screenshot is exactly the capture viewport "
            f"({vw}x{vh}) so the set is one shape; phone shots are portrait "
            f"and narrower. Images capture.mjs does not produce are not checked."
        )
        return 1

    print(
        f"check_screenshot_dimensions: OK -- {len(desktop)} desktop image(s) at "
        f"{vw}x{vh}, {len(phone)} phone image(s) portrait and narrower; "
        f"{len(SELF_TEST_VIEWPORT) + len(SELF_TEST_SHOTS) + len(SELF_TEST_DESKTOP) + len(SELF_TEST_PHONE)}"
        " fixtures hold"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
