"""Masking `!vault` scalars so the levels view resolves without a secret (#2506).

The API holds no vault secret and is not going to: a workspace's vault
passphrase is an `ansible_vault_auth` credential delivered to a configure run,
and keeping it out of the API pod is a trust-model boundary rather than an
implementation detail. That property has a cost here, and this module is what
pays it instead of the operator.

## Why there is anything to pay

`ansible-inventory --list` never decrypts: a vaulted value comes back as
`{"__ansible_vault": "<ciphertext>"}`, which is why resolution and configure
runs have always worked without a secret. `--graph --vars` prints every
variable's VALUE as text, so ansible must decrypt, and with no secret it exits
1 -- taking the `--list` result that already succeeded down with it. Measured on
core 2.21.6 and reported on 2.15.12:

    ansible-inventory -i inv --list          rc 0, `{"__ansible_vault": ...}`
    ansible-inventory -i inv --graph --vars  rc 1, "Attempting to decrypt but
                                                   no vault secrets found."

## What this does instead

The fetched tree is a throwaway extraction under a scratch directory, so the
copy ansible reads for the attribution pass is ours to edit. Every `!vault`
tagged scalar in it is replaced with the plain string `<vaulted>` BEFORE the
graph pass and AFTER `--list` has already run, so:

* `--list` -- the resolution itself -- is untouched and still carries the
  ciphertext exactly as it always did;
* `--graph --vars` has nothing left to decrypt, exits 0, and attributes every
  variable including the vaulted ones, by level;
* no ciphertext is decrypted and no secret is needed, which is the whole point.

The cost, stated rather than hidden: the levels view can say WHERE a vaulted
variable is set but not WHAT it is, and a level ansible would only reach by
decrypting -- a vaulted value that itself templates another variable -- is
attributed to the placeholder rather than to the value a configure would see.
Measured: `--graph --vars` prints the raw `{{ ... }}` for a templated value
without expanding it, so this costs nothing a reader could otherwise have had.

## The edit is spliced by byte offset, not re-serialised

A load-and-dump round trip would rewrite every file it touched: comments gone,
anchors expanded, key order and quoting style at PyYAML's discretion, and any
other custom tag (`!unsafe`) needing a constructor it does not have. All of that
is a change the operator did not make to a file ansible is about to parse.

So PyYAML's **composer** supplies the node marks -- YAML's own tokenizer rather
than a regular expression of ours -- and only the exact span of each tagged
scalar is replaced. Everything outside those spans stays byte for byte. The
replacement carries the span's own newline count, so every later line keeps its
original line number and an error ansible reports afterwards still points where
the operator would look.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
import yaml

from terrapod.services.inventory_provenance import VAULTED_PLACEHOLDER

logger = structlog.get_logger(__name__)

#: The two tags ansible's own constructor registers for an inline-encrypted
#: scalar (`ansible/_internal/_yaml/_constructor.py`, core 2.21.6). The second
#: is deprecated upstream and still parsed, so masking only the first would
#: leave a file using it failing exactly as before.
_VAULT_TAGS = frozenset({"!vault", "!vault-encrypted"})

#: Only the files a vars plugin or the yaml inventory plugin will read. `.json`
#: is excluded deliberately: JSON has no tags, so it cannot carry one.
_MASKABLE_SUFFIXES = frozenset({".yml", ".yaml", ""})

#: A vars file is kilobytes. The bound is here so that a tree holding something
#: large and extension-less -- an archive, a binary -- is skipped rather than
#: read into memory on the off-chance it is YAML.
_MAX_BYTES = 2 * 1024 * 1024


def _nodes(node: yaml.Node) -> Any:
    """Every node in a composed document, parents before children."""
    yield node
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            yield from _nodes(key)
            yield from _nodes(value)
    elif isinstance(node, yaml.SequenceNode):
        for value in node.value:
            yield from _nodes(value)


def mask_text(text: str) -> str | None:
    """`text` with every `!vault` scalar replaced, or None if there is none.

    None rather than the unchanged text, so the caller can tell "nothing to do"
    from "rewritten identically" and leave the file's mtime alone.

    Returns None on anything the composer refuses as well. A file that is not
    YAML cannot be carrying a YAML tag, and a file that IS YAML and is broken is
    ansible's to report -- rewriting it here would only change which error the
    operator sees.
    """
    if "!vault" not in text:
        return None
    try:
        documents = list(yaml.compose_all(text))
    except yaml.YAMLError:
        return None

    spans: list[tuple[int, int]] = []
    for document in documents:
        if document is None:
            continue
        for node in _nodes(document):
            if node.tag in _VAULT_TAGS:
                spans.append((node.start_mark.index, node.end_mark.index))
    if not spans:
        return None

    # Right to left, so an earlier span's offsets are still valid after a later
    # one has been spliced. A vault node is a scalar and so has no children,
    # which is what makes the spans disjoint.
    out = text
    for start, end in sorted(spans, reverse=True):
        original = out[start:end]
        # The placeholder is double-quoted so it is a string in every context --
        # block and flow alike -- whatever it is replacing, and the span's own
        # newlines are carried so the file keeps its line numbering.
        out = out[:start] + f'"{VAULTED_PLACEHOLDER}"' + "\n" * original.count("\n") + out[end:]
    return out


def mask_vaulted_scalars(root: Path) -> int:
    """Replace every `!vault` scalar under `root`, in place. Returns the count.

    `root` is a scratch extraction that is deleted when the resolution returns,
    so this edits nothing an operator stored. It must run AFTER `--list`, whose
    output is the resolution itself and must keep its ciphertext.
    """
    masked = 0
    for path in sorted(root.rglob("*")):
        # A symlink is skipped rather than followed, and that matters here in a
        # way it does not for a read-only walk: this pass WRITES. A fetched
        # repository is attacker-influenced content, and a symlink in it
        # pointing outside the scratch tree would otherwise have a file of ours
        # written through it.
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix.lower() not in _MASKABLE_SUFFIXES:
            continue
        try:
            if path.stat().st_size > _MAX_BYTES:
                continue
            text = path.read_text(errors="replace")
        except OSError:
            continue
        rewritten = mask_text(text)
        if rewritten is None:
            continue
        try:
            path.write_text(rewritten)
        except OSError:
            continue
        masked += 1
    if masked:
        logger.info("inventory vault scalars masked for attribution", files=masked)
    return masked
