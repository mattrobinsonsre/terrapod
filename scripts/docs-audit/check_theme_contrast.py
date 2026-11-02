#!/usr/bin/env python3
"""Every bg/text pairing the web app ships must clear WCAG AA on the light theme.

WHY THIS IS A GATE AND NOT A ONE-OFF MEASUREMENT. The light theme (#2337) is a
per-stop remap of Tailwind's palette, so a class written for a dark ground can
land on a light surface and lose its contrast silently -- nothing in `tsc`,
ESLint, the unit tests or the e2e suite looks at a colour. Three real failures
found while building it were invisible to every other check in the repo:

  - every status badge's FILL was indistinguishable from its card (1.006:1 on
    the first attempt), while its text contrast read as a perfectly respectable
    5.57:1. Text contrast is silent about whether a badge is visible at all, so
    this checks both axes.
  - `bg-red-600 text-white` sat at 3.20:1, which an earlier version of this
    script could not see because it only matched `text-<family>-<stop>` and
    `white` has neither.
  - `hover:bg-amber-500 text-white` at 3.20:1, invisible while variant-prefixed
    utilities were excluded wholesale.

Each of those is a shape, not an instance, which is why this reads the source
rather than carrying a list of badges: a pairing nobody thought of is checked
too.

STDLIB ONLY, AND NO node_modules. The light values are read out of `globals.css`
itself, where they are literal hexes -- so this runs in the `python-checks` job
with nothing installed, and it cannot disagree with the stylesheet the app
serves because it IS the stylesheet it reads.
"""

from __future__ import annotations

import collections
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
CSS = ROOT / "web" / "src" / "app" / "globals.css"
SRC = ROOT / "web" / "src"

AA = 4.5  # WCAG AA for normal-size text
FILL_FLOOR = 1.10  # a badge fill must be distinguishable from its card; this is
# the dark theme's own measured floor, so it is the bar the
# app already meets rather than a number invented here.
MIN_PAIRS = 40  # a matcher that stops matching must fail, not pass on nothing


def srgb_to_linear(c: float) -> float:
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rel_lum(rgb: tuple[int, int, int]) -> float:
    r, g, b = (srgb_to_linear(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    a, b = rel_lum(fg), rel_lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def composite(
    fg: tuple[int, int, int], alpha: float, bg: tuple[int, int, int]
) -> tuple[int, int, int]:
    """What a `/NN` utility actually paints: fg at alpha over an opaque bg."""
    return tuple(round(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg, strict=True))


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def light_palette() -> dict[str, dict[int, tuple[int, int, int]]]:
    """The light theme, read from the `:root:not(.dark)` block in globals.css."""
    css = CSS.read_text()
    try:
        start = css.index(":root:not(.dark) {")
        end = css.index("\n}\n", start)
    except ValueError:
        sys.exit(
            "FAIL: no `:root:not(.dark)` block in globals.css -- the light "
            "theme has moved and this check can no longer read it."
        )
    block = css[start:end]
    pal: dict[str, dict[int, tuple[int, int, int]]] = collections.defaultdict(dict)
    for fam, stop, value in re.findall(r"--color-([a-z]+)-(\d+):\s*(#[0-9a-fA-F]{3,6})\s*;", block):
        pal[fam][int(stop)] = hex_to_rgb(value)
    if not pal:
        sys.exit(
            "FAIL: the light block parsed to nothing. It is expected to "
            "hold literal hex values; if it has moved to oklch() this "
            "check needs a converter, not a lower floor."
        )
    return pal


def main() -> int:
    pal = light_palette()
    fams = "|".join(sorted(pal))

    # A utility paints the BASE state only when it carries no variant prefix --
    # `\b` matches after a colon, so a naive pattern pairs `hover:bg-amber-600`
    # with a base `text-amber-600` and reports 1.00:1, a defect that does not
    # exist. Hover fills are matched separately and paired with the base text,
    # which is what a user sees while hovering.
    bg_re = re.compile(rf"(?:^|\s)bg-({fams})-(\d+)(?:/(\d+))?(?=\s|$)")
    hov_re = re.compile(rf"(?:^|\s)hover:bg-({fams})-(\d+)(?:/(\d+))?(?=\s|$)")
    txt_re = re.compile(rf"(?:^|\s)text-({fams})-(\d+)(?=\s|$)")
    plain_re = re.compile(r"(?:^|\s)text-(white|black)(?=\s|$)")
    cls_re = re.compile(r"""(?:className=\{?["'`]|["'`])([^"'`]{8,400})["'`]""")
    PLAIN = {"white": (255, 255, 255), "black": (0, 0, 0)}

    # An alpha fill composites over whatever is behind it, which is not knowable
    # from one className; evaluate against each surface the app actually uses as
    # a parent and report the worst.
    parents = {
        name: pal["slate"][stop]
        for name, stop in (("ground", 900), ("card", 800), ("input", 700))
        if stop in pal.get("slate", {})
    }
    if len(parents) != 3:
        sys.exit(
            "FAIL: globals.css no longer declares all of slate-700/800/900 "
            "on light; those are the app's three surfaces and this check "
            "composites alpha fills over them."
        )

    worst: dict[tuple[str, str], tuple[float, str, str]] = {}
    for path in sorted(SRC.rglob("*.tsx")):
        text = path.read_text()
        for m in cls_re.finditer(text):
            s = m.group(1)
            fills = [(f, st, a, False) for f, st, a in bg_re.findall(s)] + [
                (f, st, a, True) for f, st, a in hov_re.findall(s)
            ]
            if not fills:
                continue
            inks = [(f, int(st), None) for f, st in txt_re.findall(s)] + [
                (None, None, w) for w in plain_re.findall(s)
            ]
            if not inks:
                continue
            for bfam, bstop, balpha, hov in fills:
                bg = pal.get(bfam, {}).get(int(bstop))
                if bg is None:
                    continue
                alpha = int(balpha) / 100 if balpha else 1.0
                for tfam, tstop, plain in inks:
                    fg = PLAIN[plain] if plain else pal.get(tfam, {}).get(tstop)
                    if fg is None:
                        continue
                    key = (
                        ("hover:" if hov else "")
                        + f"bg-{bfam}-{bstop}"
                        + (f"/{balpha}" if balpha else ""),
                        f"text-{plain}" if plain else f"text-{tfam}-{tstop}",
                    )
                    low, where = None, ""
                    for pname, parent in parents.items():
                        painted = composite(bg, alpha, parent) if alpha < 1 else bg
                        c = contrast(fg, painted)
                        if low is None or c < low:
                            low, where = c, pname
                    prev = worst.get(key)
                    if prev is None or low < prev[0]:
                        worst[key] = (low, where, str(path.relative_to(ROOT)))

    problems = []
    if len(worst) < MIN_PAIRS:
        problems.append(
            f"only {len(worst)} bg/text pairs matched, below the floor of "
            f"{MIN_PAIRS}. The className matcher has stopped matching; fix it "
            f"rather than lowering the floor."
        )
    for (bg_cls, fg_cls), (ratio, where, path) in sorted(worst.items(), key=lambda kv: kv[1][0]):
        if ratio < AA:
            problems.append(
                f"{ratio:5.2f}:1  {bg_cls} + {fg_cls}  (worst on the {where})  first seen {path}"
            )

    # The second axis: a badge fill has to be visible against its card. Derived
    # from the fills the source pairs with tinted ink, which is what a badge is.
    card = parents["card"]
    for (bg_cls, fg_cls), (_r, _w, path) in worst.items():
        m = re.fullmatch(r"bg-([a-z]+)-(\d+)/(\d+)", bg_cls)
        if not m or m.group(1) == "slate" or not fg_cls.startswith("text-"):
            continue
        # Only at BADGE weight. Below /40 a tint is a deliberate whisper -- the
        # dark theme renders `bg-emerald-900/15` just as faintly -- so holding
        # it to a floor re-litigates an existing design choice instead of
        # catching a regression, and a gate whose findings are mostly correct
        # prose is one people learn to silence.
        if int(m.group(3)) < 40:
            continue
        fill = composite(pal[m.group(1)][int(m.group(2))], int(m.group(3)) / 100, card)
        c = contrast(fill, card)
        if c < FILL_FLOOR:
            problems.append(
                f"{c:5.3f}:1  {bg_cls} is indistinguishable from the card -- "
                f"its text may read fine while the badge itself does not "
                f"({path})"
            )

    if problems:
        print(f"check_theme_contrast: {len(problems)} problem(s) over {len(worst)} pairs\n")
        for p in problems:
            print(f"  {p}")
        print(
            "\n  AA is 4.5:1 for normal text. A stop doing two jobs -- a solid "
            "fill on one surface and a tint on another -- cannot be fixed by "
            "moving the token; change the class at the site."
        )
        return 1

    print(
        f"check_theme_contrast: OK -- {len(worst)} bg/text pairs, "
        f"all >= {AA}:1, all badge fills >= {FILL_FLOOR}:1 against the card"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
