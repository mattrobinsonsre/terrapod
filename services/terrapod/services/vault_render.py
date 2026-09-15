"""Render a Vault-delivered file from one secret response (#1648).

A Vault reference's ``file`` object (#1619) can build its content from several
fields of the single per-run read, instead of writing one field:

- ``template``: a logic-less ``{{ name | filter }}`` substitution;
- ``format``: the whole data map (or the ``fields`` subset) as ``json`` or
  ``env`` (dotenv) lines;
- ``encoding: base64`` (handled by the caller for a single ``field``, through
  :func:`decode_base64_text`).

**This module is pure.** It receives the secret's data map and the lease
metadata as plain values and returns a string. It imports nothing that reaches
the OS, the filesystem or the network, and never evaluates anything: there are
no loops, conditionals, functions or environment access, and a source-
introspection test pins that. The template language is deliberately small so a
template stored in a variable can do nothing but place fields of the secret.

**Values are never re-scanned.** The template is tokenised once, before any
substitution, and each tag's result is appended to the output as text. A secret
that happens to contain ``{{`` therefore lands in the file verbatim; it cannot
expand into another field or inject anything.

**Errors name keys, never values.** :class:`RenderError` messages quote the
template's own text (a tag, a filter name, a field name) and the field *names*
present in the secret, which the reference already exposes. They never contain
any part of a value, because they become the run's error message.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass

#: The largest template accepted, in UTF-8 bytes.
MAX_TEMPLATE_BYTES = 16 * 1024
#: Filters a tag may apply, in the order the docs list them.
FILTERS: tuple[str, ...] = ("json", "base64decode", "trim", "lines", "indent")
#: What ``_lease.*`` may name. The lease id is deliberately absent.
LEASE_KEYS: tuple[str, ...] = ("ttl", "renewable", "expires_at")
#: The root name that selects lease metadata rather than a field of the secret.
LEASE_ROOT = "_lease"
#: ``format`` values.
FORMATS: tuple[str, ...] = ("json", "env")
#: ``encoding`` values.
ENCODINGS: tuple[str, ...] = ("base64",)
#: The widest ``indent N`` accepted.
MAX_INDENT = 64

_TAG = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_SEGMENT = r"[A-Za-z0-9_-]+"
_NAME = re.compile(rf"{_SEGMENT}(?:\.{_SEGMENT})*")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MESSAGE_TAG_LIMIT = 80


class RenderError(ValueError):
    """A file could not be rendered. The message carries names only, never a value."""


@dataclass(frozen=True)
class _Filter:
    name: str
    arg: int | None = None


@dataclass(frozen=True)
class _Tag:
    """One ``{{ … }}``: a dotted name and the filters applied to it, in order."""

    path: tuple[str, ...]
    filters: tuple[_Filter, ...]
    text: str  # the tag as written, for messages — template text, not a value

    @property
    def dotted(self) -> str:
        return ".".join(self.path)


def _shown(tag_text: str) -> str:
    """A tag as it appears in a message: collapsed whitespace, bounded length."""
    t = " ".join(tag_text.split())
    if len(t) > _MESSAGE_TAG_LIMIT:
        t = t[: _MESSAGE_TAG_LIMIT - 1] + "…"
    return "{{ " + t + " }}"


def _parse_filter(raw: str, tag_text: str) -> _Filter:
    parts = raw.split()
    if not parts:
        raise RenderError(f"tag {_shown(tag_text)} has an empty filter after '|'")
    name, args = parts[0], parts[1:]
    if name not in FILTERS:
        raise RenderError(
            f"tag {_shown(tag_text)} uses unknown filter {name!r} (available: {', '.join(FILTERS)})"
        )
    if name == "indent":
        if len(args) != 1 or not args[0].isdigit():
            raise RenderError(
                f"tag {_shown(tag_text)}: filter 'indent' takes one whole number, e.g. 'indent 4'"
            )
        n = int(args[0])
        if n > MAX_INDENT:
            raise RenderError(
                f"tag {_shown(tag_text)}: filter 'indent' is limited to {MAX_INDENT} spaces"
            )
        return _Filter(name, n)
    if args:
        raise RenderError(f"tag {_shown(tag_text)}: filter {name!r} takes no argument")
    return _Filter(name)


def _parse_tag(inner: str) -> _Tag:
    pieces = inner.split("|")
    name = pieces[0].strip()
    if not name:
        raise RenderError(f"tag {_shown(inner)} names nothing")
    if not _NAME.fullmatch(name):
        raise RenderError(
            f"tag {_shown(inner)} has an invalid name {name!r}: use field names of "
            "letters, digits, '_' and '-', joined by '.' to reach into a map"
        )
    path = tuple(name.split("."))
    if path[0] == LEASE_ROOT and (len(path) != 2 or path[1] not in LEASE_KEYS):
        raise RenderError(
            f"tag {_shown(inner)}: {LEASE_ROOT} offers "
            + ", ".join(f"{LEASE_ROOT}.{k}" for k in LEASE_KEYS)
        )
    filters = tuple(_parse_filter(p, inner) for p in pieces[1:])
    return _Tag(path=path, filters=filters, text=inner)


def parse_template(template: object) -> list[str | _Tag]:
    """Tokenise a template into literal text and tags, or raise :class:`RenderError`.

    Used both when a variable is written (so a syntax error is a 422 then, not a
    failed run later) and at render time. Every ``{{`` opens a tag: one with no
    closing ``}}`` is an error, so a typo cannot silently land in the file as
    literal text. A lone ``}}`` is ordinary text.
    """
    if not isinstance(template, str):
        raise RenderError("must be a string")
    size = len(template.encode("utf-8"))
    if size > MAX_TEMPLATE_BYTES:
        raise RenderError(
            f"is {size} bytes, over the {MAX_TEMPLATE_BYTES // 1024} KiB limit for a template"
        )
    parts: list[str | _Tag] = []
    pos = 0
    for m in _TAG.finditer(template):
        literal = template[pos : m.start()]
        if "{{" in literal:
            raise RenderError("has a '{{' with no closing '}}'")
        if literal:
            parts.append(literal)
        parts.append(_parse_tag(m.group(1)))
        pos = m.end()
    tail = template[pos:]
    if "{{" in tail:
        raise RenderError("has a '{{' with no closing '}}'")
    if tail:
        parts.append(tail)
    return parts


def to_text(value: object) -> str:
    """How a value is written into a file: a string as it is, anything else as JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def decode_base64_text(value: object, *, what: str) -> str:
    """Decode base64 to UTF-8 text, or raise :class:`RenderError` naming ``what``.

    Whitespace (a wrapped encoding) is ignored; anything else outside the
    base64 alphabet is refused. The decoded bytes must be UTF-8: binary files
    need a wire change and are not supported in this release.
    """
    if not isinstance(value, str):
        raise RenderError(f"{what} is not a string, so it cannot be base64-decoded")
    compact = "".join(value.split())
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as e:
        raise RenderError(f"{what} is not valid base64") from e
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RenderError(
            f"{what} decodes to bytes that are not UTF-8 text; binary files are not supported yet"
        ) from e


def _lookup(tag: _Tag, data: dict, lease: dict | None) -> object:
    if tag.path[0] == LEASE_ROOT:
        if lease is None:
            raise RenderError(
                f"tag {_shown(tag.text)} reads the lease, but this secret's response "
                "carries no lease (kv-v2 secrets never do)"
            )
        return lease[tag.path[1]]
    cur: object = data
    for i, seg in enumerate(tag.path):
        if not isinstance(cur, dict):
            parent = ".".join(tag.path[:i])
            raise RenderError(
                f"tag {_shown(tag.text)}: {parent!r} is not a map, so it has no {seg!r}"
            )
        if seg not in cur:
            where = repr(".".join(tag.path[:i])) if i else "the secret"
            available = ", ".join(sorted(str(k) for k in cur)) or "none"
            raise RenderError(
                f"tag {_shown(tag.text)} names {tag.dotted!r}, which is not in "
                f"{where} (available: {available})"
            )
        cur = cur[seg]
    return cur


def _apply(f: _Filter, value: object, tag: _Tag) -> object:
    if f.name == "json":
        return json.dumps(value, ensure_ascii=False)
    if f.name == "base64decode":
        return decode_base64_text(value, what=f"{tag.dotted!r} in tag {_shown(tag.text)}")
    if f.name == "trim":
        return to_text(value).strip()
    if f.name == "lines":
        if not isinstance(value, list):
            raise RenderError(
                f"tag {_shown(tag.text)}: filter 'lines' needs a list, and "
                f"{tag.dotted!r} is not one"
            )
        return "\n".join(to_text(item) for item in value)
    if f.name == "indent":
        pad = " " * (f.arg or 0)
        first, *rest = to_text(value).split("\n")
        return "\n".join([first, *(pad + line if line else line for line in rest)])
    raise RenderError(f"unknown filter {f.name!r}")  # pragma: no cover - parse refuses it


def render_template(template: str, data: dict, lease: dict | None = None) -> str:
    """Render ``template`` against one secret's ``data`` and optional ``lease``.

    Single pass: the template is tokenised before anything is substituted, and
    each result is appended as text, so a value is never re-scanned.
    """
    out: list[str] = []
    for part in parse_template(template):
        if isinstance(part, str):
            out.append(part)
            continue
        value = _lookup(part, data, lease)
        for f in part.filters:
            value = _apply(f, value, part)
        out.append(to_text(value))
    return "".join(out)


def _selected(data: dict, fields: list[str] | None) -> dict:
    if not fields:
        return data
    missing = [f for f in fields if f not in data]
    if missing:
        available = ", ".join(sorted(str(k) for k in data)) or "none"
        raise RenderError(
            f"fields {', '.join(repr(f) for f in missing)} are not in the secret "
            f"(available: {available})"
        )
    return {f: data[f] for f in fields}


def env_escape(text: str) -> str:
    """Escape ``text`` for the inside of a POSIX-shell double-quoted string.

    Inside ``"…"`` a POSIX shell treats only ``\\``, ``"``, ``$`` and a backtick
    specially, so exactly those four are backslash-escaped. Newlines stay
    literal, inside the quotes: sourcing the file (``set -a; . ./file``)
    yields the original value byte for byte.
    """
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


def render_format(fmt: str, data: dict, fields: list[str] | None = None) -> str:
    """The whole data map, or the ``fields`` subset, as ``json`` or ``env``.

    ``json``: the object, indented two spaces, with a trailing newline. Keys
    keep the order Vault returned them in (or the order of ``fields``).

    ``env``: one ``KEY="value"`` line per key, each ending in a newline, with the
    value escaped by :func:`env_escape`. A key must be a valid environment name
    (``[A-Za-z_][A-Za-z0-9_]*``) and a value must not contain a NUL byte.
    Non-string values are written as JSON.
    """
    obj = _selected(data, fields)
    if fmt == "json":
        return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    if fmt == "env":
        lines = []
        for k, v in obj.items():
            if not isinstance(k, str) or not _ENV_KEY.fullmatch(k):
                raise RenderError(
                    f"format 'env' needs every key to be an environment variable name, "
                    f"and {k!r} is not"
                )
            text = to_text(v)
            if "\x00" in text:
                raise RenderError(f"format 'env': the value of {k!r} contains a NUL byte")
            lines.append(f'{k}="{env_escape(text)}"\n')
        return "".join(lines)
    raise RenderError(f"unknown format {fmt!r} (expected {' or '.join(FORMATS)})")
