#!/usr/bin/env python3
"""Every relative link and heading anchor in the docs corpus resolves (#1389).

Pure mechanism, no judgement: a link either points at something that exists or
it does not. That is why this is the first gate — it needs no human in the loop
and it caught real defects the moment it was pointed at the corpus.

Checks, for each tracked doc:
  * relative links to files            -> the file exists
  * relative links with an #anchor     -> the target file has that heading
  * same-page #anchors                 -> this file has that heading
  * image sources                      -> the image exists

Deliberately NOT checked: external http(s) links. Reaching the network makes
the gate flaky and slow, and a 404 on someone else's site is not something a
pull request can be blamed for.

Links inside fenced code blocks are skipped — they are samples, not references.
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# [text](target) — target stops at whitespace so an optional "title" is dropped.
LINK = re.compile(r'!?\[(?:[^\]]*)\]\(\s*([^)\s]+)(?:\s+"[^"]*")?\s*\)')
# CommonMark fence rules, not a naive toggle. A block opened with ```json can
# only be closed by a bare fence of the same character and at least the same
# length — so a ```json line appearing INSIDE a block is content, not a close.
# Toggling on every fence-looking line silently swallowed 90% of the API
# reference's headings and produced 20 confident false positives.
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


def strip_fenced(lines: list[str]):
    """Yield (lineno, text) for lines outside fenced code blocks."""
    marker: str | None = None
    for i, line in enumerate(lines, 1):
        m = FENCE_RE.match(line)
        if m:
            run, info = m.group(1), m.group(2).strip()
            if marker is None:
                # An opener's info string may not contain a backtick.
                if not ("`" in info and run[0] == "`"):
                    marker = run
                    continue
            elif run[0] == marker[0] and len(run) >= len(marker) and info == "":
                marker = None
                continue
        if marker is None:
            yield i, line
# A heading, or an HTML anchor some docs use for stable link targets.
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
HTML_ANCHOR = re.compile(r'<a\s+(?:id|name)="([^"]+)"', re.I)


def slug(text: str) -> str:
    """GitHub's heading-anchor algorithm.

    Strip inline markdown first (`code`, **bold**, links keep their text), then
    lowercase, drop everything that is not alphanumeric/space/hyphen/underscore,
    and turn spaces into hyphens. Getting this wrong in either direction makes
    the checker useless: too strict and it cries wolf, too loose and it passes
    broken anchors.
    """
    t = text
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)  # links -> their text
    # Backtick/asterisk/tilde only. NOT underscore: it is a word character and
    # GitHub keeps it, so stripping it mangles every heading naming an
    # identifier — which is most of the runbook headings.
    t = re.sub(r"[`*~]", "", t)
    t = re.sub(r"<[^>]+>", "", t)                      # inline html
    t = t.strip().lower()
    t = re.sub(r"[^\w\s-]", "", t, flags=re.UNICODE)
    # ONE hyphen per whitespace character, not per run. Removing an em-dash
    # from "Kinds — personal" leaves two spaces and GitHub emits "kinds--
    # personal"; collapsing them here would reject anchors that actually work.
    return re.sub(r"\s", "-", t)


def anchors_of(path: Path) -> set[str]:
    """Every anchor a reader could link to in this file."""
    found: set[str] = set()
    seen: dict[str, int] = {}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for _, line in strip_fenced(lines):
        for a in HTML_ANCHOR.findall(line):
            found.add(a.lower())
        m = HEADING.match(line)
        if not m:
            continue
        s = slug(m.group(2))
        if not s:
            continue
        # GitHub disambiguates repeats with -1, -2, ...
        n = seen.get(s, 0)
        seen[s] = n + 1
        found.add(s if n == 0 else f"{s}-{n}")
        if n == 0:
            found.add(s)
    return found


def links_of(path: Path) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, line in strip_fenced(lines):
        for target in LINK.findall(line):
            out.append((i, target))
    return out


def main() -> int:
    files = subprocess.run(
        ["git", "ls-files", "*.md", "llms.txt"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    docs = [ROOT / f for f in files]
    anchor_cache: dict[Path, set[str]] = {}

    def anchors(p: Path) -> set[str]:
        if p not in anchor_cache:
            anchor_cache[p] = anchors_of(p)
        return anchor_cache[p]

    problems: list[str] = []
    checked = 0

    for doc in docs:
        rel = doc.relative_to(ROOT)
        for lineno, target in links_of(doc):
            if target.startswith(("http://", "https://", "mailto:", "tel:")):
                continue
            checked += 1
            frag = ""
            if "#" in target:
                target, frag = target.split("#", 1)

            if target == "":
                # Same-page anchor.
                if frag and frag.lower() not in anchors(doc):
                    problems.append(f"{rel}:{lineno}: no heading for same-page anchor #{frag}")
                continue

            # Repo-absolute (/docs/x.md) or relative to this file's directory.
            dest = (ROOT / target.lstrip("/")) if target.startswith("/") else (doc.parent / target)
            try:
                dest = dest.resolve()
                dest.relative_to(ROOT)
            except (OSError, ValueError):
                problems.append(f"{rel}:{lineno}: link escapes the repo: {target}")
                continue

            if not dest.exists():
                problems.append(f"{rel}:{lineno}: broken link -> {target}")
                continue

            if frag and dest.suffix == ".md":
                if frag.lower() not in anchors(dest):
                    problems.append(
                        f"{rel}:{lineno}: {target} exists but has no anchor #{frag}"
                    )

    print(f"  {len(docs)} docs, {checked} internal links checked")
    if problems:
        print(f"\nFAIL — {len(problems)} broken reference(s):\n")
        for p in problems:
            print(f"  {p}")
        return 1
    print("PASS — every internal link and anchor resolves.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
