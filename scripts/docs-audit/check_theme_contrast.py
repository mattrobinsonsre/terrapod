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

It also holds the checkbox outline to 3:1 (WCAG 1.4.11), reading the classes out
of `checkbox.tsx`; the dark theme's half is `web/tests/theme-contrast-dark.test.ts`.
"""

from __future__ import annotations

import collections
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
CSS = ROOT / "web" / "src" / "app" / "globals.css"
SRC = ROOT / "web" / "src"
CHECKBOX = SRC / "components" / "checkbox.tsx"

AA = 4.5  # WCAG AA for normal-size text
NON_TEXT = 3.0  # WCAG 1.4.11: a control's outline against what it sits on
MIN_BARE_INKS = 4  # neutral ink stops used with no fill; a floor so a
#: broken matcher fails here instead of passing on nothing
FILL_FLOOR = 1.10  # a badge fill must be distinguishable from its card.
# This used to be justified as "the dark theme's own measured
# floor, so it is the bar the app already meets". Measured
# against the stock palette the dark theme renders, that is
# false: `bg-red-900/40` sits at 1.060:1 against its card on
# dark, and purple/violet/indigo at /40 are all 1.06-1.09, so
# dark's own floor is nearer 1.06 and 1.10 holds light to a
# bar dark does not meet. Kept at 1.10 because it is the right
# bar and lowering it would only make the gate agree with a
# defect; closing the dark side is ~60 sites and a separate
# change. The reason is recorded rather than deleted because a
# justification that reads as measured is the kind that
# survives review.
MIN_PAIRS = 40  # a matcher that stops matching must fail, not pass on nothing

#: Literal spellings `class_strings()` must read, each with the strings it must
#: yield. This is the gate on the gate, and it exists because the previous
#: matcher's blind spot was invisible to every count: it paired quotes with
#: `["'`]` on BOTH ends, so a closing quote could act as an opening one and
#: whether a given `className="..."` was read at all depended on the parity of
#: every quote above it in the file. 1,989 of 3,733 plain `className` attributes
#: were unread, ~415 of them carrying a bg/text pair, while the floors below sat
#: comfortably satisfied -- because a count over the tree cannot distinguish a
#: className that was deleted from a matcher that stopped reading it. Only
#: fixtures can, so narrowing the scanner fails HERE, naming the spelling.
SELF_TEST: list[tuple[str, list[str]]] = [
    # The attribute form, and the two neighbours whose quotes used to swallow it.
    ('<div className="bg-red-600 text-white" />', ["bg-red-600 text-white"]),
    (
        '<a title="x" className="bg-red-600 text-white">go</a>',
        ["bg-red-600 text-white"],
    ),
    (
        '<p aria-label="Close dialog" className="bg-red-600 text-white" />',
        ["Close dialog", "bg-red-600 text-white"],
    ),
    # The parity case: prose above the attribute must not shift what is read.
    (
        '// a comment that isn\'t balanced\n<div className="bg-red-600 text-white" />',
        ["bg-red-600 text-white"],
    ),
    (
        '/* it\'s a block comment */\n<div className="bg-red-600 text-white" />',
        ["bg-red-600 text-white"],
    ),
    (
        "<p>Don't, it's prose</p>\n<div className=\"bg-red-600 text-white\" />",
        ["bg-red-600 text-white"],
    ),
    # A class string in a `.ts` badge table, which is why there is no
    # `className=` prefix: the second axis was written for exactly these.
    (
        "const badge = { ok: 'bg-green-900/40 text-green-300' }",
        ["bg-green-900/40 text-green-300"],
    ),
    (
        'const cls = cond ? "bg-red-600 text-white" : "bg-slate-700 text-slate-200"',
        ["bg-red-600 text-white", "bg-slate-700 text-slate-200"],
    ),
    # A ternary inside a template literal yields each arm SEPARATELY. Pooling
    # them would invent a pairing that never renders, which is the mistake the
    # fourth axis below is written around.
    (
        "className={`px-2 text-white ${bad ? 'bg-red-600' : 'bg-green-700'} rounded`}",
        ["px-2 text-white ${bad ? ", "bg-red-600", "bg-green-700", "} rounded"],
    ),
    # Must NOT yield: a quote inside a literal is content, not a delimiter, and
    # a class string a comment merely mentions is not shipped.
    ('<img alt="It\'s fine" className="bg-red-600 text-white" />', ["bg-red-600 text-white"]),
    ('// never ship className="bg-red-600 text-white"', []),
]


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


def _sources() -> list[pathlib.Path]:
    """Every file in `web/src` that can hold a Tailwind class string.

    `.ts` AS WELL AS `.tsx`, and the omission was real rather than theoretical:
    this read `*.tsx` only while 23 bg/text pairings lived in `web/src/lib/`
    --- `host-status.ts`, `workspace-status.ts`, `workspace-fields.ts` --- and
    two of those three were edited by the very release this gate was written
    for. A badge table is exactly the shape the second axis exists to measure,
    and it was the one place the gate could not see. `globals.css` claimed this
    read "every className in `web/src`", which was false until now.
    """
    return sorted([*SRC.rglob("*.tsx"), *SRC.rglob("*.ts")])


#: A quote opens a literal only where one legitimately can. No JavaScript lets a
#: string literal follow an identifier, so a quote preceded by a word character
#: is JSX prose -- `<p>Don't, it's here</p>` -- and treating it as a delimiter is
#: how prose comes to decide what the scan can see.
_WORD = re.compile(r"\w")
#: A literal body is split on quotes, so each quote-free run is judged on its
#: own. That is what keeps a ternary inside a template literal from pooling: the
#: two arms are mutually exclusive, and pairing one arm's fill with the other's
#: ink invents a combination that never renders.
_SEGMENT = re.compile(r"""[^"'`]+""")


def class_strings(text: str) -> list[str]:
    """Every string literal in a source file, as quote-free runs of 8-400 chars.

    THIS IS A SCANNER AND NOT A REGEX, and the reason is that the delimiters are
    ambiguous to a pattern. The previous matcher accepted `["'`]` at both ends,
    so it could not tell an OPENING quote from a CLOSING one: given
    `<a title="x" className="bg-red-600 text-white">`, it matched from the quote
    that closes `title` through ` className=` to the quote that OPENS the class
    string, consuming it -- and the class string was then never read. Whether a
    given `className` was visible therefore depended on the parity of every
    quote, and every apostrophe in every comment, above it in the file. 1,989 of
    3,733 plain `className` attributes went unread that way, ~415 of them
    carrying a bg/text pair the gate consequently never evaluated, while
    `globals.css` and this file's own docstring both claimed it read every
    className in `web/src`. A backreference fixes which quote CLOSES a literal
    and not which quote may OPEN one, so the fix has to consume literals as
    units; that also means comments can be skipped, which is the other half of
    it.

    Two documented limits. A backtick inside a `${...}` interpolation (a nested
    template literal) ends the outer literal early, and a regex literal
    containing an unbalanced quote reads as one -- neither occurs in `web/src`,
    and both fail in the direction of reading LESS, which `MIN_PAIRS` and
    `SELF_TEST` between them are what catch.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and text[i + 1 : i + 2] == "/":
            nl = text.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue
        if c == "/" and text[i + 1 : i + 2] == "*":
            close = text.find("*/", i + 2)
            i = n if close < 0 else close + 2
            continue
        if c in "\"'`" and not (i and _WORD.match(text[i - 1])):
            j = i + 1
            while j < n:
                d = text[j]
                if d == "\\":
                    j += 2
                    continue
                if d == c or (c != "`" and d == "\n"):
                    break  # a newline in a '' or "" means it was never a literal
                j += 1
            if j < n and text[j] == c:
                for seg in _SEGMENT.finditer(text[i + 1 : j]):
                    if 8 <= len(seg.group(0)) <= 400:
                        out.append(seg.group(0))
                i = j + 1
                continue
        i += 1
    return out


def self_test() -> list[str]:
    """Pin each spelling BEFORE the scan, so a narrowing fails by name."""
    out: list[str] = []
    for text, expected in SELF_TEST:
        got = class_strings(text)
        if sorted(got) != sorted(expected):
            out.append(
                f"class_strings() no longer reads {text!r} the way it must: "
                f"expected {sorted(expected)}, read {sorted(got)}. Widen the "
                "scanner rather than the fixture -- a spelling this gate cannot "
                "see is a spelling it does not check, and a count over the tree "
                "cannot tell a deleted className from a matcher that stopped "
                "matching."
            )
    return out


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


def checkbox_outline_problems(pal: dict[str, dict[int, tuple[int, int, int]]]) -> list[str]:
    """The outline in `checkbox.tsx` against its own fill and the surfaces behind it.
    Hovering the box also hovers its row, so that state is measured on a hovered row."""
    m = re.search(r"className=\{([^}]*appearance-none[^}]*)\}", CHECKBOX.read_text())
    if not m:
        return [
            f"{CHECKBOX.relative_to(ROOT)}: no className with `appearance-none`; the outline check cannot read it"
        ]
    toks = " ".join(re.findall(r"'([^']*)'", m.group(1))).split()

    def paint(variant: str, prop: str):
        """`<variant><prop>-<family>-<stop>[/<alpha>]`, the light override first."""
        for pre in (f"light:{variant}", variant):
            for t in toks:
                mm = re.fullmatch(rf"{re.escape(pre)}{prop}-([a-z]+)-(\d+)(?:/(\d+))?", t)
                if mm and int(mm[2]) in pal.get(mm[1], {}):
                    return pal[mm[1]][int(mm[2])], int(mm[3] or 100) / 100, t
        return None

    page = pal["slate"][900]
    panel = composite(pal["slate"][800], 0.5, page)
    row = composite(pal["slate"][700], 0.2, panel)
    base_fill, hov_fill = paint("", "bg"), paint("hover:", "bg")
    base_edge, hov_edge = paint("", "border"), paint("hover:", "border")
    chk_edge = paint("checked:", "border")
    if not (base_fill and hov_fill and base_edge and hov_edge and chk_edge):
        return [
            f"{CHECKBOX.relative_to(ROOT)}: a base, hover or checked border or fill class is missing"
        ]

    states = [
        (f"on the {n}", bd, base_fill, base_edge)
        for n, bd in (("page", page), ("panel", panel), ("hovered row", row))
    ]
    states.append(("hovered, on a hovered row", row, hov_fill, hov_edge))
    out: list[str] = []
    for name, backdrop, fill, edge in states:
        under = composite(fill[0], fill[1], backdrop)
        painted = composite(edge[0], edge[1], under)
        for what, other in (("its own fill", under), ("the surface behind it", backdrop)):
            c = contrast(painted, other)
            if c < NON_TEXT:
                out.append(
                    f"{c:5.2f}:1  checkbox outline `{edge[2]}` {name}, against {what} "
                    f"(needs {NON_TEXT}:1; {CHECKBOX.relative_to(ROOT)})"
                )
    for name, backdrop in (("page", page), ("panel", panel), ("hovered row", row)):
        c = contrast(chk_edge[0], backdrop)
        if c < NON_TEXT:
            out.append(
                f"{c:5.2f}:1  checkbox checked outline `{chk_edge[2]}` on the {name} "
                f"(needs {NON_TEXT}:1; {CHECKBOX.relative_to(ROOT)})"
            )
    return out


def main() -> int:
    broken = self_test()
    if broken:
        print("check_theme_contrast: the className scanner is broken\n")
        for b in broken:
            print(f"  {b}")
        return 1

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
    # A hover fill is paired with the hover INK when the element declares one,
    # and only otherwise with the base ink. Pairing it with the base ink
    # unconditionally invents a state: `bg-amber-700 hover:bg-amber-600
    # text-amber-200 hover:text-white` renders amber-200 over amber-700 or white
    # over amber-600, never amber-200 over amber-600 -- and that phantom was
    # reported at 1.80:1 across three pages, which is the shape of finding people
    # learn to silence a gate over.
    hov_txt_re = re.compile(rf"(?:^|\s)hover:text-({fams})-(\d+)(?=\s|$)")
    hov_plain_re = re.compile(r"(?:^|\s)hover:text-(white|black)(?=\s|$)")
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
    for path in sorted(_sources()):
        for s in class_strings(path.read_text()):
            fills = [(f, st, a, False) for f, st, a in bg_re.findall(s)] + [
                (f, st, a, True) for f, st, a in hov_re.findall(s)
            ]
            if not fills:
                continue
            inks = [(f, int(st), None) for f, st in txt_re.findall(s)] + [
                (None, None, w) for w in plain_re.findall(s)
            ]
            hov_inks = [(f, int(st), None) for f, st in hov_txt_re.findall(s)] + [
                (None, None, w) for w in hov_plain_re.findall(s)
            ]
            if not inks and not hov_inks:
                continue
            for bfam, bstop, balpha, hov in fills:
                bg = pal.get(bfam, {}).get(int(bstop))
                if bg is None:
                    continue
                alpha = int(balpha) / 100 if balpha else 1.0
                for tfam, tstop, plain in (hov_inks or inks) if hov else inks:
                    fg = PLAIN[plain] if plain else pal.get(tfam, {}).get(tstop)
                    if fg is None:
                        continue
                    hov_ink = hov and bool(hov_inks)
                    key = (
                        ("hover:" if hov else "")
                        + f"bg-{bfam}-{bstop}"
                        + (f"/{balpha}" if balpha else ""),
                        ("hover:" if hov_ink else "")
                        + (f"text-{plain}" if plain else f"text-{tfam}-{tstop}"),
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

    # The FOURTH axis: AN INK OUTSIDE AN INTERPOLATION, A FILL INSIDE IT.
    #
    # The pair axis above requires the fill and the ink in the SAME quoted run,
    # which a template literal splits apart:
    #
    #   className={`... text-white ... ${cond ? 'bg-red-600' : 'bg-amber-900/40'}`}
    #
    # `text-white` is in the literal part and each fill is in its own branch, so
    # three separate matches and no pair is ever formed. That is not theoretical:
    # the state-version rollback button shipped `text-white` over
    # `bg-amber-900/40`, which on light is 40% of `#ffb900` -- 1.33:1, an
    # invisible label -- and this gate reported OK over 94 pairs while it did.
    #
    # Pooling the whole expression would be wrong in the other direction: the two
    # arms of a ternary are mutually exclusive, so pairing one arm's fill with the
    # other arm's ink invents a combination that never renders. So the rule is
    # narrower and matches what the browser does -- an ink in the literal part
    # applies to EVERY branch, an ink inside a branch applies to that branch only.
    tmpl_re = re.compile(r"className=\{`([^`]{8,2000})`\}", re.S)
    interp_re = re.compile(r"\$\{(.*?)\}", re.S)
    quoted_re = re.compile(r"""['"]([^'"]{4,300})['"]""")
    for path in sorted(_sources()):
        for m in tmpl_re.finditer(path.read_text()):
            body = m.group(1)
            shared = interp_re.sub(" ", body)  # the literal parts only
            shared_inks = [(f, int(s), None) for f, s in txt_re.findall(shared)] + [
                (None, None, w) for w in plain_re.findall(shared)
            ]
            if not shared_inks:
                continue
            for im in interp_re.finditer(body):
                for branch in quoted_re.findall(im.group(1)):
                    fills = [(f, s, a, False) for f, s, a in bg_re.findall(branch)] + [
                        (f, s, a, True) for f, s, a in hov_re.findall(branch)
                    ]
                    # An ink in the branch itself wins, and the pair axis has
                    # already judged that case.
                    if not fills or txt_re.search(branch) or plain_re.search(branch):
                        continue
                    for bfam, bstop, balpha, hov in fills:
                        bg = pal.get(bfam, {}).get(int(bstop))
                        if bg is None:
                            continue
                        alpha = int(balpha) / 100 if balpha else 1.0
                        for tfam, tstop, plain in shared_inks:
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
    problems.extend(checkbox_outline_problems(pal))
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

    # The third axis: BARE INK -- a neutral text colour with no fill of its own.
    #
    # The pair check above only ever looks at a `bg-*` and a `text-*` declared
    # on the SAME element, so text that simply inherits the page or a card --
    # which is most text -- was never checked at all. `text-slate-600` shipped
    # at 1.42:1 through that hole, invisible, on the workspaces list and the
    # sortable headers, because slate-600 is a border colour on light and the
    # ramp made it do double duty as ink.
    #
    # A slate ink carries no fill with it, so it has to be legible wherever the
    # app happens to put it: on the ground, on a card, and on an input. Tinted
    # inks (red/green/amber) are badge text and travel with their own fill, so
    # they stay out of this; and an inverted pill like `bg-slate-100
    # text-slate-900` declares its fill on the element and is covered above.
    bare: dict[int, str] = {}
    for path in sorted(_sources()):
        for s in class_strings(path.read_text()):
            if bg_re.search(s):
                continue  # has its own fill; the pair axis owns it
            for fam, stop in txt_re.findall(s):
                if fam != "slate":
                    continue
                bare.setdefault(int(stop), str(path.relative_to(ROOT)))
    if len(bare) < MIN_BARE_INKS:
        problems.append(
            f"only {len(bare)} bare slate ink stops matched, below the floor "
            f"of {MIN_BARE_INKS}. The matcher has stopped matching; fix it "
            f"rather than lowering the floor."
        )
    for stop, path in sorted(bare.items()):
        fg = pal["slate"].get(stop)
        if fg is None:
            continue
        for pname, parent in parents.items():
            c = contrast(fg, parent)
            if c < AA:
                problems.append(
                    f"{c:5.2f}:1  text-slate-{stop} on the bare {pname} "
                    f"({parent})  first seen {path}"
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
        f"all >= {AA}:1, all badge fills >= {FILL_FLOOR}:1 against the card, "
        f"checkbox outline >= {NON_TEXT}:1"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
