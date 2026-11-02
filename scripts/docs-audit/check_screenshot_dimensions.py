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
#: this directory is built to refuse.
MIN_DESKTOP = 20
MIN_PHONE = 2


def png_size(path: pathlib.Path) -> tuple[int, int] | None:
    """(width, height) from the IHDR, or None if it is not a PNG."""
    data = path.read_bytes()[:24]
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", data[16:24])


def main() -> int:
    if not CAPTURE.exists():
        sys.exit(f"FAIL: {CAPTURE.relative_to(ROOT)} is missing; this gate reads it")
    src = CAPTURE.read_text()

    m = re.search(
        r"const VIEWPORT\s*=\s*\{\s*width:\s*(\d+),\s*height:\s*(\d+)\s*\}", src
    )
    if not m:
        sys.exit(
            "FAIL: could not read VIEWPORT out of capture.mjs. Teach this "
            "extractor the new shape -- do NOT hardcode the numbers here, or "
            "the gate stops tracking the script it exists to enforce."
        )
    vw, vh = int(m.group(1)), int(m.group(2))

    # The desktop set: every helper that pushes onto `shots` or `anonShots`.
    desktop = set()
    for helper in ("shot", "anonShot", "page_", "wsTab"):
        desktop |= set(re.findall(rf"^\s*{re.escape(helper)}\(\s*'([^']+)'", src, re.M))
    phone = set(re.findall(r"^\s*phoneShot\(\s*'([^']+)'", src, re.M))

    problems: list[str] = []
    if len(desktop) < MIN_DESKTOP:
        problems.append(
            f"only {len(desktop)} desktop shot(s) matched, below the floor of "
            f"{MIN_DESKTOP}. The shot matcher has stopped matching; fix it "
            f"rather than lowering the floor."
        )
    if len(phone) < MIN_PHONE:
        problems.append(
            f"only {len(phone)} phone shot(s) matched, below the floor of {MIN_PHONE}."
        )

    for name in sorted(desktop):
        f = IMAGES / f"{name}.png"
        if not f.exists():
            problems.append(f"{name}.png is registered in capture.mjs but not committed")
            continue
        size = png_size(f)
        if size is None:
            problems.append(f"{name}.png is not a PNG")
        elif size != (vw, vh):
            problems.append(
                f"{name}.png is {size[0]}x{size[1]}, not the capture viewport "
                f"{vw}x{vh}. Re-run: node scripts/demo/capture.mjs "
                f"--host <host> --only {name}"
            )

    for name in sorted(phone):
        f = IMAGES / f"{name}.png"
        if not f.exists():
            problems.append(f"{name}.png is registered in capture.mjs but not committed")
            continue
        size = png_size(f)
        if size is None:
            problems.append(f"{name}.png is not a PNG")
            continue
        w, h = size
        if w >= vw:
            problems.append(
                f"{name}.png is {w}px wide, which is not narrower than the "
                f"{vw}px desktop capture -- it is meant to show the phone"
            )
        if h <= w:
            problems.append(f"{name}.png is {w}x{h}, which is not portrait")

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
        f"{vw}x{vh}, {len(phone)} phone image(s) portrait and narrower"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
