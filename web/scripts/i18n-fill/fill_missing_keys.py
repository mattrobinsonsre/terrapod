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
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2] / "messages"

_PLACEHOLDER = re.compile(r"\{([a-zA-Z0-9_]+)")


def missing_keys(locale: str, namespace: str) -> tuple[str, ...]:
    """The dotted `group.leaf` keys `en` has under `namespace` and `locale` has not.

    Two levels deep, which is the shape of every namespace this fills. A key
    whose value is itself an object is skipped rather than guessed at: a nested
    group needs its own fill, and silently flattening one would write a string
    where the catalogue expects a map.
    """
    src = _load("en")[namespace]
    dst = _load(locale).get(namespace, {})
    out: list[str] = []
    for group, items in src.items():
        if not isinstance(items, dict):
            continue
        have = dst.get(group, {})
        have = have if isinstance(have, dict) else {}
        out.extend(f"{group}.{leaf}" for leaf in items if leaf not in have)
    return tuple(out)


def _load(locale: str) -> collections.OrderedDict:
    path = ROOT / f"{locale}.json"
    if not path.exists():
        sys.exit(f"no catalogue for locale {locale!r} at {path}")
    return json.loads(path.read_text(), object_pairs_hook=collections.OrderedDict)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("locale")
    ap.add_argument("values", help="JSON object of {dotted.key: translated value}")
    ap.add_argument("--namespace", default="workspaceDetail")
    ap.add_argument("--allow-identical", default="")
    args = ap.parse_args()

    if args.locale == "en":
        sys.exit("en is the SOURCE catalogue; it is not filled by this script")

    keys = missing_keys(args.locale, args.namespace)
    if not keys:
        sys.exit(f"{args.locale} is already complete under {args.namespace}; nothing to fill")

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
        grp, leaf = key.split(".")
        src = english[grp][leaf]
        want = sorted(set(_PLACEHOLDER.findall(src)))
        got = sorted(set(_PLACEHOLDER.findall(value)))
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
        grp, leaf = key.split(".")
        wd.setdefault(grp, collections.OrderedDict())[leaf] = value
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    print(f"{args.locale}: wrote {len(values)} keys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
