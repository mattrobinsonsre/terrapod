"""Deterministic application of AI naming decisions to discovery output (#824 Phase A).

The AI onboarding "polish" makes the generated config read like something a human
wrote — resources renamed from their tags, grouped, and commented. The hard
invariant is that the AI **never touches an attribute value or an import id**.

This module is how that invariant is *enforced by construction*, not merely
checked: the model returns only structured naming decisions (a per-resource
``ResourcePolish`` of new-name / group / comment), and the functions here apply
them to the RAW text. The model's output never contributes a single character of
HCL value or import id — those are copied verbatim from the deterministic
discovery output. A rename is a label-only edit because ``-generate-config-out``
emits flat configs with literal values and no inter-resource references, so
renaming ``resource "aws_eip" "old"`` → ``"new"`` is self-contained; the only
coupled edit is the matching ``import {}`` block's ``to = aws_eip.<name>``, which
is rewritten from the same rename map (the ``id`` line is left byte-identical).

Everything here is pure stdlib (no HCL parser, no I/O) so it is exhaustively
unit-testable and cannot itself introduce a blocking dependency. Any structural
inconsistency (an address the raw config doesn't contain, an invalid or colliding
new name, an unsplittable block) raises :class:`PolishError`; the caller treats
that as "reject the polish, keep the raw config" — the raw output is always the
safe fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Terraform/OpenTofu resource local-name grammar: a letter or underscore, then
# letters / digits / underscores / hyphens. We validate the AI's proposed names
# against this so a bad suggestion can never produce unparseable HCL.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

# A resource block opener: `resource "TYPE" "NAME" {` at column 0. `-generate-
# config-out` always emits this shape (one space-separated line, brace on the
# same line); we key on it and treat the next column-0 `}` as the block end.
_RESOURCE_OPEN_RE = re.compile(r'^resource\s+"(?P<type>[^"]+)"\s+"(?P<name>[^"]+)"\s*\{\s*$')

# Cap a single comment line so a pathological model response can't bloat the
# stored config. Comments are cosmetic; truncation is harmless.
_MAX_COMMENT_LEN = 200


class PolishError(Exception):
    """A structural inconsistency that makes the AI's naming un-appliable.

    Raised (never swallowed here) so the caller keeps the raw, guaranteed-clean
    config instead of a half-applied polish. Message is safe to log.
    """


@dataclass(frozen=True)
class ResourcePolish:
    """One resource's naming decision, as returned by the model.

    ``address`` is the CURRENT address (``<type>.<name>``) — it must match a
    resource in the raw config exactly. ``new_name`` is the proposed local name
    (the type never changes). ``group``/``comment`` are cosmetic.
    """

    address: str
    new_name: str
    group: str = ""
    comment: str = ""


@dataclass(frozen=True)
class ResourceBlock:
    """A parsed resource block from the raw generated config."""

    rtype: str
    name: str
    body: str  # the exact block text, `resource "..." "..." {` … `}` (no trailing newline)

    @property
    def address(self) -> str:
        return f"{self.rtype}.{self.name}"


@dataclass(frozen=True)
class PolishResult:
    config: str
    import_blocks: str
    renamed: int = 0
    grouped: int = 0
    commented: int = 0
    _groups: tuple[str, ...] = field(default=())


def split_resource_blocks(config: str) -> list[ResourceBlock]:
    """Split ``-generate-config-out`` output into resource blocks, in order.

    Keys on a column-0 ``resource "T" "N" {`` opener and the next column-0 ``}``
    as the closer. Lines outside blocks (the generator's own header comment,
    blank lines) are ignored — we re-lay-out the file ourselves. Raises
    :class:`PolishError` if an opener has no matching column-0 closer (so a
    malformed config fails closed rather than producing a truncated block).
    """
    lines = config.splitlines()
    blocks: list[ResourceBlock] = []
    i = 0
    n = len(lines)
    while i < n:
        m = _RESOURCE_OPEN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        start = i
        j = i + 1
        while j < n and lines[j] != "}":
            # A nested resource opener before the close means our column-0
            # assumption broke — refuse rather than mis-split.
            if _RESOURCE_OPEN_RE.match(lines[j]):
                raise PolishError(f"nested resource opener before close near line {j + 1}")
            j += 1
        if j >= n:
            raise PolishError(f"unterminated resource block at line {start + 1}")
        body = "\n".join(lines[start : j + 1])
        blocks.append(ResourceBlock(rtype=m.group("type"), name=m.group("name"), body=body))
        i = j + 1
    return blocks


def _sanitize_comment(text: str) -> str:
    """One-line, length-capped, no embedded newlines. '' when nothing usable."""
    if not text:
        return ""
    first = text.replace("\r", "").split("\n", 1)[0].strip()
    if len(first) > _MAX_COMMENT_LEN:
        first = first[:_MAX_COMMENT_LEN].rstrip() + "…"
    return first


def _rename_block(block: ResourceBlock, new_name: str, comment: str) -> str:
    """Return the block with its label swapped and an optional comment prepended.

    Only the FIRST line (the `resource "T" "N" {` opener) is rewritten; every
    other byte of the body is preserved verbatim — this is what guarantees no
    attribute value can change.
    """
    lines = block.body.split("\n")
    lines[0] = f'resource "{block.rtype}" "{new_name}" {{'
    out = "\n".join(lines)
    c = _sanitize_comment(comment)
    if c:
        out = f"# {c}\n{out}"
    return out


def _rewrite_import_targets(import_blocks: str, renames: dict[str, str]) -> str:
    """Rewrite each ``to = <old-address>`` to its new address; ids untouched.

    Matches the exact importblock.go shape (`  to = <addr>` on its own line).
    Only the address token is replaced; the ``id = "…"`` line is never seen.
    """
    if not import_blocks or not renames:
        return import_blocks
    result = import_blocks
    for old_addr, new_addr in renames.items():
        if old_addr == new_addr:
            continue
        pattern = re.compile(
            r"(?m)^(?P<pre>[ \t]*to[ \t]*=[ \t]*)" + re.escape(old_addr) + r"(?P<post>[ \t]*)$"
        )
        result = pattern.sub(
            lambda mo, na=new_addr: f"{mo.group('pre')}{na}{mo.group('post')}", result
        )
    return result


def apply_polish(
    generated_config: str,
    import_blocks: str,
    resources: list[ResourcePolish],
    *,
    file_header: str = "",
) -> PolishResult:
    """Apply the AI's naming decisions deterministically.

    Renames resource labels, prepends per-resource comments, reorders blocks by
    ``group`` (stable, groups in first-appearance order, a section-header comment
    per named group), and rewrites the coupled ``import {}`` ``to =`` targets. The
    ``id`` of every import and every attribute value of every resource is copied
    verbatim from the raw input.

    Raises :class:`PolishError` on any inconsistency — an address not present in
    the raw config, an invalid new name, or a post-rename name collision within a
    type — so the caller can fall back to the raw config.
    """
    blocks = split_resource_blocks(generated_config)
    if not blocks:
        raise PolishError("no resource blocks in generated config")

    by_address = {b.address: b for b in blocks}
    decisions: dict[str, ResourcePolish] = {}
    for r in resources:
        if r.address not in by_address:
            raise PolishError(f"unknown resource address from model: {r.address!r}")
        if r.address in decisions:
            raise PolishError(f"duplicate decision for address: {r.address!r}")
        name = (r.new_name or "").strip()
        if name and not _IDENT_RE.match(name):
            raise PolishError(f"invalid resource name from model: {name!r}")
        decisions[r.address] = r

    # Resolve final names (default to the existing name where the model was
    # silent) and check per-type uniqueness — two resources of the same type
    # can't share a local name or the config won't parse.
    final_name: dict[str, str] = {}
    seen_per_type: dict[str, set[str]] = {}
    renamed = 0
    for b in blocks:
        d = decisions.get(b.address)
        chosen = d.new_name.strip() if d and d.new_name.strip() else b.name
        type_names = seen_per_type.setdefault(b.rtype, set())
        if chosen in type_names:
            raise PolishError(f"name collision for {b.rtype}: {chosen!r}")
        type_names.add(chosen)
        final_name[b.address] = chosen
        if chosen != b.name:
            renamed += 1

    # Group + order: stable, groups by first appearance; ungrouped ('') last.
    group_order: list[str] = []
    grouped_blocks: dict[str, list[ResourceBlock]] = {}
    commented = 0
    for b in blocks:
        d = decisions.get(b.address)
        grp = _sanitize_comment(d.group) if d and d.group else ""
        if grp not in grouped_blocks:
            grouped_blocks[grp] = []
            group_order.append(grp)
        grouped_blocks[grp].append(b)
    # Ungrouped block ('') goes to the end; named groups keep first-appearance
    # order. Capture the appearance index up front — sorting in place would make
    # a mid-sort .index() lookup unreliable.
    first_seen = {g: i for i, g in enumerate(group_order)}
    group_order.sort(key=lambda g: (g == "", first_seen[g]))

    rendered_groups: list[str] = []
    for grp in group_order:
        parts: list[str] = []
        if grp:
            parts.append(f"# {grp}")
        for b in grouped_blocks[grp]:
            d = decisions.get(b.address)
            comment = d.comment if d else ""
            if _sanitize_comment(comment):
                commented += 1
            parts.append(_rename_block(b, final_name[b.address], comment))
        rendered_groups.append("\n\n".join(parts))

    header = ""
    if file_header:
        header_lines = [
            f"# {ln.strip()}" if ln.strip() else "#"
            for ln in file_header.replace("\r", "").split("\n")
        ]
        header = "\n".join(header_lines) + "\n\n"

    config_out = header + "\n\n".join(rendered_groups) + "\n"

    renames = {b.address: f"{b.rtype}.{final_name[b.address]}" for b in blocks}
    imports_out = _rewrite_import_targets(import_blocks, renames)

    return PolishResult(
        config=config_out,
        import_blocks=imports_out,
        renamed=renamed,
        grouped=len([g for g in group_order if g]),
        commented=commented,
        _groups=tuple(g for g in group_order if g),
    )


def assert_values_preserved(generated_config: str, polished_config: str) -> None:
    """Belt-and-braces: every raw block body must survive verbatim in the polish.

    ``apply_polish`` preserves bodies by construction, but this independent check
    (run by the caller before persisting) turns the guarantee into an assertion:
    for every raw resource, the polished config must contain a block of the same
    type whose body — everything after the opener line — is byte-identical to the
    raw body. Raises :class:`PolishError` on any drift, so a future refactor that
    accidentally lets a value change fails loudly instead of silently shipping a
    corrupted import.
    """
    raw = split_resource_blocks(generated_config)
    pol = split_resource_blocks(polished_config)

    def _body_after_opener(b: ResourceBlock) -> str:
        return b.body.split("\n", 1)[1] if "\n" in b.body else ""

    # Group polished bodies by type; a raw body must appear among them.
    pol_bodies: dict[str, list[str]] = {}
    for b in pol:
        pol_bodies.setdefault(b.rtype, []).append(_body_after_opener(b))
    for b in raw:
        body = _body_after_opener(b)
        if body not in pol_bodies.get(b.rtype, []):
            raise PolishError(
                f"value drift: {b.rtype} body from {b.address!r} not found unchanged in polish"
            )


# An import block from terrapod-query: `import {\n  to = <addr>\n  id = "…"\n}`.
# No nested braces, so a non-greedy `{…}` match is exact.
_IMPORT_BLOCK_RE = re.compile(r"import\s*\{[^{}]*\}", re.DOTALL)
_IMPORT_TO_RE = re.compile(r"(?m)^\s*to\s*=\s*(\S+)\s*$")


def pair_config_and_imports(config: str, import_blocks: str) -> str:
    """Interleave each ``import {}`` directly above the resource it targets.

    Produces a single, review-friendly HCL string where every ``import { to =
    <addr> … }`` sits immediately above its ``resource "<type>" "<name>"`` block,
    preserving the config's block order, group headers, and per-resource comments.
    Imports are matched to resources by address (``<type>.<name>``). This is a
    **presentation-only, derived** transform — it never mutates the canonical
    ``config`` / ``import_blocks`` (which stay separable for the operator, since an
    applied import block is a harmless no-op left in place). Returns ``config``
    unchanged when there are no imports.

    Defensive: an import with no matching resource is appended at the end rather
    than dropped, and a resource with no import is emitted alone — so no line is
    ever lost even if the two halves disagree.
    """
    if not config:
        return ""
    if not import_blocks:
        return config

    imp_by_addr: dict[str, str] = {}
    for m in _IMPORT_BLOCK_RE.finditer(import_blocks):
        block = m.group(0)
        to = _IMPORT_TO_RE.search(block)
        if to:
            imp_by_addr[to.group(1)] = block.strip()

    out: list[str] = []
    used: set[str] = set()
    for line in config.splitlines():
        rm = _RESOURCE_OPEN_RE.match(line)
        if rm:
            addr = f"{rm.group('type')}.{rm.group('name')}"
            imp = imp_by_addr.get(addr)
            if imp is not None:
                out.append(imp)  # import block (multi-line) directly above resource
                used.add(addr)
        out.append(line)

    result = "\n".join(out).rstrip("\n")
    leftover = [imp_by_addr[a] for a in imp_by_addr if a not in used]
    if leftover:
        result += "\n\n" + "\n\n".join(leftover)
    return result + "\n"
