#!/usr/bin/env python3
"""Write a set of new keys into one locale catalogue, values only.

ONE writer, shared by everybody filling these keys, for the reason #1915
recorded: six people each inventing their own JSON serialisation reformat the
catalogues against each other, and the real change disappears into the noise.
This writes values only -- same key order, same 2-space indent, same
`ensure_ascii=False`, same trailing newline -- so every diff is changed values
and nothing else.

It refuses:

* a key outside the twenty this change adds, so a fill cannot touch anything
  else in the catalogue;
* a value that drops or renames an ICU placeholder the English string carries
  (`{limit}`, `{status}`). A placeholder lost in translation is invisible to
  the completeness gate -- the key is present and the sentence reads fine --
  and renders as the literal text at runtime;
* a value identical to the English one, unless `--allow-identical` names that
  key. Key parity is not translation (#1915): a catalogue holding the English
  string passes every gate while being untranslated, and thirty-one locales
  doing it are indistinguishable from thirty-one that are correct. The
  exemption exists because a few of these genuinely do not change -- a short
  technical label some languages keep verbatim -- and naming them is the point.

**The key set is DERIVED**, not listed here: it is whatever `en.json` has and
the target locale does not, under the namespace being filled. A hardcoded list
would make this a record of one past change rather than a tool, and it would
drift from the catalogues the moment anything else was added.

Usage:
    fill_configure_keys.py <locale> <values.json>
                           [--namespace workspaceDetail]
                           [--allow-identical k1,k2]

`values.json` is `{"group.leaf": "translated value"}` for every key the locale
is missing. A partial fill is refused, because a catalogue that is half filled
passes nothing and leaves the next person guessing which half.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2] / "messages"

_IDENTIFIER = re.compile(r"[a-zA-Z0-9_]+")


def placeholders(message: str) -> set[str]:
    """The ICU ARGUMENT names in a message.

    **ICU alternates by brace depth, and a regex cannot read it.** A placeholder
    opens at ODD depth and its first identifier is the argument name; a plural's
    sub-message opens at EVEN depth and its first word is prose. This used to be
    a bare `\\{([a-zA-Z0-9_]+)` over the whole string, which cannot tell the two
    apart -- so for

        {files, plural, one {already cached (# file)} other {…}}

    it reported the argument set as `{"files", "already"}` and then demanded the
    translation open its sub-messages with the English word "already". That is
    not a thing any translation can do, which made such a key UNFILLABLE: the
    refusal named a placeholder that does not exist, and the only way past it
    was to reword the English so every sub-message began with `#`.

    Counting the depth is both the fix and strictly stronger than what it
    replaces: it still catches an argument dropped or renamed in translation --
    the invisible defect this check exists for, since the key is present and the
    sentence reads fine while the placeholder renders as literal text -- and it
    no longer invents one out of prose. `_self_check` drives the shapes below as
    literal fixtures before any file is read, so narrowing it again fails by
    name rather than quietly shrinking what is checked. The fixtures live in
    this file because nothing in CI runs this script -- it is a tool, not a gate
    -- so a test beside it would be a test nothing executes.
    """
    found: set[str] = set()
    depth = 0
    i = 0
    while i < len(message):
        char = message[i]
        if char == "'" and i + 1 < len(message) and message[i + 1] in "{}#|":
            # An ICU quote: the next special character is literal text. Skipped
            # so `'{'` is not read as opening a placeholder.
            i += 2
            continue
        if char == "{":
            depth += 1
            if depth % 2 == 1:
                match = _IDENTIFIER.match(message, i + 1)
                if match:
                    found.add(match.group())
        elif char == "}":
            depth = max(0, depth - 1)
        i += 1
    return found


def missing_keys(locale: str, namespace: str) -> tuple[str, ...]:
    """The dotted paths `en` has under `namespace` and `locale` has not.

    **Any depth.** This walked exactly TWO levels, which made a nested group
    unfillable rather than merely unsupported: it surfaced the GROUP as the
    missing key, whose English value is an object, then refused it as "not a
    non-empty string" -- and because a partial fill is also refused, there was
    no subset of such a change the script would accept. Three separate fills of
    `runDetail.log.hosts.*` hit that and each wrote its own driver, which is the
    #1915 failure the one-writer rule exists to prevent.

    The old docstring said a nested group "is skipped rather than guessed at".
    The intent was right and the skip was implemented at the group level rather
    than the leaf, so it hard-failed instead of skipping. Recursing to the leaf
    is what the intent actually required -- `runDetail.log.index` had been a
    nested group all along, so the shape was never exotic.
    """
    src = _load("en")[namespace]
    dst = _load(locale).get(namespace, {})
    out: list[str] = []

    def walk(want, have, prefix: str) -> None:
        for name, value in want.items():
            here = have.get(name) if isinstance(have, dict) else None
            path = f"{prefix}{name}"
            if isinstance(value, dict):
                walk(value, here if isinstance(here, dict) else {}, f"{path}.")
            elif here is None:
                out.append(path)

    walk(src, dst, "")
    return tuple(out)


def _reach(doc, path: str):
    """The English value at a dotted path, for the parity and identity checks."""
    for part in path.split("."):
        doc = doc[part]
    return doc


def _plant(doc, path: str, value: str) -> None:
    """Write `value` at a dotted path, creating the groups it passes through."""
    *groups, leaf = path.split(".")
    for group in groups:
        nxt = doc.get(group)
        if not isinstance(nxt, collections.OrderedDict):
            nxt = collections.OrderedDict(nxt) if isinstance(nxt, dict) else collections.OrderedDict()
            doc[group] = nxt
        doc = nxt
    doc[leaf] = value


def _load(locale: str) -> collections.OrderedDict:
    path = ROOT / f"{locale}.json"
    if not path.exists():
        sys.exit(f"no catalogue for locale {locale!r} at {path}")
    return json.loads(path.read_text(), object_pairs_hook=collections.OrderedDict)


#: `(message, the argument names it carries)`. The last three are the ones a
#: depth-blind matcher gets wrong, and the first four are what stop a fix for
#: them from being a matcher that finds nothing.
_PLACEHOLDER_FIXTURES: tuple[tuple[str, set[str]], ...] = (
    ("no placeholders here", set()),
    ("one {limit} of them", {"limit"}),
    ("{a} and {b}", {"a", "b"}),
    ("{count, plural, one {# thing} other {# things}}", {"count"}),
    # A sub-message opening with PROSE. Depth-blind, "already" reads as a second
    # argument and the key becomes unfillable.
    ("{files, plural, one {already cached (# file)} other {already cached}}", {"files"}),
    # A nested argument inside a sub-message is still an argument.
    ('{count, plural, one {# of "{name}"} other {# of "{name}"}}', {"count", "name"}),
    # A quoted brace is literal text, not an argument.
    ("a literal '{' brace and a real {arg}", {"arg"}),
)


def _self_check() -> None:
    """Prove the matcher still matches, before it is trusted about anything.

    A matcher that has quietly stopped recognising a form reports no problems,
    which reads exactly like a clean fill -- so this asserts the forms rather
    than counting hits, and runs before any catalogue is read.
    """
    for message, want in _PLACEHOLDER_FIXTURES:
        got = placeholders(message)
        if got != want:
            sys.exit(f"placeholder matcher is broken: {message!r} gave {sorted(got)}")


def main() -> int:
    _self_check()
    ap = argparse.ArgumentParser()
    ap.add_argument("locale")
    ap.add_argument(
        "values",
        help=(
            "PATH TO A FILE holding a JSON object of {dotted.key: translated value}. "
            "A path, not the JSON itself -- this said 'JSON object' and three "
            "separate fills read it as an inline string before the brief started "
            "warning about it."
        ),
    )
    ap.add_argument("--namespace", default="workspaceDetail")
    ap.add_argument("--allow-identical", default="")
    ap.add_argument(
        "--replace",
        default="",
        help=(
            "Dotted keys the locale ALREADY has, to be overwritten. Every value "
            "refusal below still applies, so a replacement cannot be the English "
            "string or drop a placeholder. Naming a key the locale does not have "
            "is refused: that is a fill, and mixing the two silently would let a "
            "typo in a key name read as a successful replace."
        ),
    )
    args = ap.parse_args()

    if args.locale == "en":
        sys.exit("en is the SOURCE catalogue; it is not filled by this script")

    keys = missing_keys(args.locale, args.namespace)
    replace = {k.strip() for k in args.replace.split(",") if k.strip()}
    if replace:
        have = _load(args.locale).get(args.namespace, {})
        absent = []
        for key in sorted(replace):
            try:
                found = _reach(have, key)
            except (KeyError, TypeError):
                found = None
            if not isinstance(found, str):
                absent.append(key)
        if absent:
            sys.exit(
                f"--replace names keys {args.locale} does not already carry "
                f"(that is a fill, not a replace): {absent}"
            )
        keys = tuple(dict.fromkeys((*keys, *sorted(replace))))
    if not keys:
        sys.exit(
            f"{args.locale} is already complete under {args.namespace} and "
            "--replace named nothing; there is nothing to write"
        )

    allow_identical = {k.strip() for k in args.allow_identical.split(",") if k.strip()}
    unknown_exemptions = allow_identical - set(keys)
    if unknown_exemptions:
        sys.exit(f"--allow-identical names keys this change does not add: {sorted(unknown_exemptions)}")

    values = json.loads(pathlib.Path(args.values).read_text())
    stray = sorted(set(values) - set(keys))
    if stray:
        sys.exit(f"refusing: not keys this locale is missing: {stray}")
    missing = sorted(set(keys) - set(values))
    if missing:
        sys.exit(f"refusing a partial fill — missing: {missing}")

    english = _load("en")[args.namespace]
    problems: list[str] = []
    for key, value in values.items():
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{key}: not a non-empty string")
            continue
        try:
            src = _reach(english, key)
        except (KeyError, TypeError):
            problems.append(f"{key}: not a key `en` has under this namespace")
            continue
        if not isinstance(src, str):
            problems.append(f"{key}: names a GROUP in `en`, not a value — fill its leaves")
            continue
        want = sorted(placeholders(src))
        got = sorted(placeholders(value))
        if want != got:
            problems.append(f"{key}: placeholders {got} should be {want}")
        if value == src and key not in allow_identical:
            problems.append(f"{key}: identical to English — translate it or pass --allow-identical")
    if problems:
        sys.exit("refusing:\n  " + "\n  ".join(problems))

    path = ROOT / f"{args.locale}.json"
    doc = _load(args.locale)
    wd = doc.setdefault(args.namespace, collections.OrderedDict())
    for key, value in values.items():
        _plant(wd, key, value)
    # Atomically, so a concurrent reader -- several locales are often filled in
    # parallel -- can only ever see the whole old file or the whole new one.
    rendered = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(rendered)
    os.replace(tmp, path)
    print(f"{args.locale}: wrote {len(values)} keys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
