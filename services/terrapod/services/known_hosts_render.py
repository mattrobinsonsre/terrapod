"""Rendering approved host keys as the `known_hosts` file OpenSSH enforces (#2592).

Terrapod owns the MODEL of what a host is trusted to present; **OpenSSH owns the
enforcement**. This module is the one arrow between them, and it is the whole of
Terrapod's contribution to the decision: it turns rows into lines, and from there
the crypto, the matching and the refusal are OpenSSH's. Ansible is merely the
process that invokes `ssh` and is not modelled on at all.

Its own module, pure, and **it imports nothing beyond `collections.abc` and
`typing`** -- the same bar as `inventory_render`, and for the same measured
reason: the runner image carries it as one COPYed file, and
`requirements-runner.txt` is a deliberately minimal 21 packages. A module-level
import of anything else makes `job_entrypoint` unimportable and every run of
every kind fails at startup. That is why the two frozensets below are duplicated
from `db.models` rather than imported from it -- importing them would pull in
SQLAlchemy. `tests/services/test_known_hosts_render.py` pins the duplicates equal
to the model's, so they cannot drift.

The format is OpenSSH's and the measurements behind each decision are recorded
where the decision is made. The two that shape the whole module:

* **A marker is a LINE prefix, not a property of an algorithm.** So a host with
  three approved keys gets three lines, and one of them can be `@revoked` while
  the others are not.
* **`@revoked` is absolute and order-independent.** Measured against OpenSSH
  10.0p2 against a real `sshd`: a host key present BOTH plainly and as
  `@revoked` is refused in either line order, with "plain only" accepted as the
  control. So the sort below is for byte-stability alone and carries no
  semantics -- which is what makes a rotation safe: the outgoing key stays
  declared and refused rather than becoming an unknown key somebody is asked
  about.
"""

from collections.abc import Mapping, Sequence
from typing import Any

#: The markers OpenSSH recognises, without the `@`.
#:
#: Duplicated from `db.models.SSH_HOST_KEY_MARKERS` because this module must
#: import nothing -- see the module docstring. Pinned equal by a test.
MARKERS: frozenset[str] = frozenset({"cert-authority", "revoked"})

#: The key types this renderer will emit, duplicated from
#: `db.models.SSH_HOST_KEY_TYPES` for the same reason and pinned the same way.
KEY_TYPES: frozenset[str] = frozenset(
    {
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "ssh-rsa",
        "rsa-sha2-256",
        "rsa-sha2-512",
    }
)

#: The port OpenSSH does not bracket. A line for any other port MUST carry the
#: `[host]:port` form or it matches nothing -- measured both ways against a real
#: `sshd`: a bare `10.0.0.9` line did not match a connection to port 2222, and
#: ssh itself wrote `[127.0.0.1]:2222` when asked to learn that host.
DEFAULT_SSH_PORT = 22


class KnownHostsRenderError(ValueError):
    """A row cannot be rendered as a `known_hosts` line.

    Raised rather than skipped, and that direction is the point. Every field of
    a line is already refused at the API's write path, so reaching this means
    either a row that predates those validators or a value that arrived by some
    other route -- and in both cases the two alternatives are worse. Skipping
    the row leaves a host whose key an operator can SEE declared and which
    OpenSSH will refuse, which reads as "the host changed its key". Emitting it
    anyway is worse still: a field carrying a newline writes a line of the
    caller's choosing into a file whose whole purpose is to say which keys are
    trusted, for hosts the caller does not own.
    """


def known_hosts_patterns(
    host_name: str,
    host_vars: Mapping[str, Any] | None = None,
) -> list[str]:
    """Every name OpenSSH could be asked to match for this inventory host.

    **Not the inventory hostname, which is the trap this function exists for.**
    OpenSSH looks up whatever it was handed on the command line, and ansible
    hands it `ansible_host` when that is set -- measured: a five-host probe
    whose hosts all set `ansible_host: 127.0.0.1` produced five `ssh ... -tt
    127.0.0.1` invocations and the inventory names appeared nowhere in any of
    them. A file keyed on `web1` while ssh asks about `10.0.0.5` matches
    nothing, so every host would be refused and the symptom would read as a key
    that does not match rather than as a name that does not.

    The inventory name is kept too when it differs, because it is what ansible
    hands ssh for a host with no `ansible_host`, and because naming this host by
    another of its own names trusts nothing further -- the key is the same key.

    `ansible_port` adds the bracketed form rather than replacing the bare one.
    A port set somewhere this cannot see -- `remote_port` in the operator's own
    `ansible.cfg`, or a `-o Port=` inside their ssh args -- would otherwise make
    the lookup `[host]:port` against a file holding only `host`, and keeping the
    bare form costs nothing while covering the default-port direction of that
    gap. The other direction fails closed, which is the right way for it to
    fail and is documented in `docs/ansible-configure.md`.
    """
    variables = host_vars or {}
    target = variables.get("ansible_host") or host_name
    out: list[str] = [str(target)]

    port = variables.get("ansible_port")
    if port not in (None, "") and str(port) != str(DEFAULT_SSH_PORT):
        out.append(f"[{target}]:{port}")

    if str(host_name) != str(target):
        out.append(str(host_name))

    # Deduplicated while keeping the order above, which puts the form ssh is
    # most likely to ask for first. Order carries no meaning to OpenSSH -- a
    # comma-separated pattern list matches on any element, measured -- so this
    # is for a human reading the file.
    seen: set[str] = set()
    patterns: list[str] = []
    for pattern in out:
        if pattern and pattern not in seen:
            seen.add(pattern)
            patterns.append(pattern)
    return patterns


def render_known_hosts(
    keys: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    patterns: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """The approved keys as `known_hosts` content.

    `keys` maps an inventory host NAME to the key attributes the API serves for
    it -- `key-type`, `public-key` and `marker`, in the API's own kebab-case
    spelling, so a caller hands the JSON:API attributes straight through with no
    translation step to get wrong. Extra attributes (`created-at` and friends)
    are ignored.

    `patterns` maps a host name to the names OpenSSH may be asked to match for
    it, from `known_hosts_patterns`. A host absent from it falls back to its
    inventory name alone, which is right for a host with no `ansible_host` and
    is the only answer available to a caller that has no inventory to read.

    **Every approved key gets its own line.** Never one per algorithm family: a
    host legitimately holds two keys of one type while it is being rotated, and
    the unique constraint on the table is `(host, type, key)` precisely so it
    can.

    Returns the empty string for empty input, with no trailing newline, so that
    "this workspace has no approved keys" is distinguishable from "it has some"
    by truthiness alone. A caller must not write an empty file and point ssh at
    it: under `StrictHostKeyChecking=yes` an empty `known_hosts` refuses every
    host -- measured -- which is correct as an allow-list and wrong as a default
    for a workspace that declared nothing.

    Raises:
        KnownHostsRenderError: a field cannot be rendered. See the exception.
    """
    lines: list[str] = []
    for host_name in sorted(keys):
        host_patterns = list((patterns or {}).get(host_name) or [str(host_name)])
        for pattern in host_patterns:
            _refuse_unrenderable("host pattern", pattern, host_name)
        joined = ",".join(host_patterns)

        # Sorted so the same rows render the same bytes whatever order they
        # arrived in. `@revoked` is order-independent (module docstring), so
        # this decides nothing but the diff.
        for row in sorted(
            keys[host_name],
            key=lambda r: (
                str(r.get("key-type") or ""),
                str(r.get("public-key") or ""),
                str(r.get("marker") or ""),
            ),
        ):
            lines.append(_line(joined, row, host_name))
    return "".join(f"{line}\n" for line in lines)


def _line(patterns: str, row: Mapping[str, Any], host_name: str) -> str:
    key_type = row.get("key-type")
    public_key = row.get("public-key")
    marker = row.get("marker")

    _refuse_unrenderable("key type", key_type, host_name)
    _refuse_unrenderable("public key", public_key, host_name)

    if key_type not in KEY_TYPES:
        raise KnownHostsRenderError(
            f"the approved key for {host_name!r} declares key type {key_type!r}, which "
            f"OpenSSH does not recognise. It would ignore the line, so the host would be "
            f"refused as though it had presented an unknown key -- refusing here instead, "
            f"where the cause is nameable. The types are: {', '.join(sorted(KEY_TYPES))}."
        )

    prefix = ""
    if marker not in (None, ""):
        _refuse_unrenderable("marker", marker, host_name)
        if marker not in MARKERS:
            raise KnownHostsRenderError(
                f"the approved key for {host_name!r} carries marker {marker!r}. OpenSSH "
                f"recognises only {', '.join(sorted(MARKERS))} and silently ignores a line "
                f"whose marker it does not know, which would make a declared trust anchor "
                f"match nothing at all."
            )
        prefix = f"@{marker} "

    return f"{prefix}{patterns} {key_type} {public_key}"


def _refuse_unrenderable(what: str, value: Any, host_name: str) -> None:
    """Refuse a field that is not one whitespace-free token.

    A `known_hosts` line is whitespace-delimited and newline-terminated, so a
    field containing either does not render as the field -- it renders as
    further fields, or as further LINES naming hosts the caller does not own.
    The API's own validators already refuse all of this at the write path; this
    is the same refusal at the point the bytes are produced, because that is the
    boundary where getting it wrong stops being a bad row and becomes a forged
    trust anchor.
    """
    if not isinstance(value, str) or not value:
        raise KnownHostsRenderError(
            f"the approved key for {host_name!r} has no usable {what}: "
            f"expected a non-empty string, got {value!r}"
        )
    if value.strip() != value or any(character.isspace() for character in value):
        raise KnownHostsRenderError(
            f"the {what} of an approved key for {host_name!r} contains whitespace "
            f"({value!r}). A known_hosts line is whitespace-delimited, so this would "
            f"render as further fields or further lines rather than as the value."
        )
