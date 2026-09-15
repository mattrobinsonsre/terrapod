"""Vault variable value-source resolution (#1439).

At ``next_run`` a variable whose ``value_source`` is ``vault`` carries a
*reference* rather than a literal. This module turns each reference into the
concrete value that gets delivered, so the runner stays source-agnostic — it
receives ordinary env/terraform variables and never learns where they came from.

The reference::

    {"source": "vault", "vault": "default", "mount": "kvv2",
     "path": "apps/netbox", "field": "apitoken", "engine": "kv2"}

``vault`` is optional and resolves to the instance marked ``default: true``, or
the sole configured instance. ``engine`` is ``kv2`` (default) or ``dynamic``.

An optional ``"file": {"name": "gcp/adc.json"}`` (#1619) delivers the value as a
file instead: the variable's value becomes the file's absolute path, and the
content travels only in the ``vault-files`` runs/next attribute into the
per-run Secret. ``name`` defaults to the variable key; ``~/x`` names a path in
the runner's home. See ``terrapod.runner.vault_files``.

**Failure is fatal, unlike git-auth.** ``git_auth_service`` drops an
unresolvable credential with a warning so one bad entry cannot fail a run. That
is the wrong trade here: a silently absent credential leaves Terraform to fail
somewhere confusing, or to fall back to another identity and act with
credentials nobody chose. A reference that cannot be resolved raises, and the
run fails at the point of resolution with a message naming the cause.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import structlog

from terrapod.config import Settings
from terrapod.runner.vault_files import (
    MAX_FILE_BYTES,
    FilePathError,
    check_collisions,
    target_path,
    validate_name,
)
from terrapod.services.vault_client import (
    VaultError,
    VaultUnavailable,
    extract_field,
    read_secret_data,
    secret_path,
)

logger = structlog.get_logger("vault_source")

VALUE_SOURCE_STATIC = "static"
VALUE_SOURCE_VAULT = "vault"
VALUE_SOURCES = {VALUE_SOURCE_STATIC, VALUE_SOURCE_VAULT}


class VaultSourceError(RuntimeError):
    """A vault-sourced variable could not be resolved. Fails the run."""


class VaultTransient(VaultSourceError):
    """Vault was unreachable. The run is left queued rather than failed.

    A subclass so a caller that only knows about VaultSourceError still behaves
    safely — it fails the run, which is the conservative outcome — while a
    caller that knows the difference can wait instead.
    """


def parse_reference(raw: str, *, key: str) -> dict:
    """Parse and validate a stored reference, or raise."""
    try:
        ref = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise VaultSourceError(
            f"variable {key!r} has a vault source but its value is not JSON"
        ) from e
    if not isinstance(ref, dict):
        raise VaultSourceError(f"variable {key!r} has a vault reference that is not an object")

    missing = [f for f in ("mount", "path", "field") if not ref.get(f)]
    if missing:
        raise VaultSourceError(
            f"variable {key!r} has a vault reference missing: {', '.join(missing)}"
        )
    engine = ref.get("engine", "kv2")
    if engine not in ("kv2", "dynamic"):
        raise VaultSourceError(
            f"variable {key!r} has an unknown vault engine {engine!r} (expected kv2 or dynamic)"
        )
    method = str(ref.get("method", "GET")).upper()
    if method not in ("GET", "POST"):
        raise VaultSourceError(
            f"variable {key!r} has an unsupported vault method {method!r} (expected GET or POST)"
        )
    if ref.get("data") is not None and not isinstance(ref["data"], dict):
        raise VaultSourceError(f"variable {key!r} has a vault `data` that is not an object")
    if ref.get("file") is not None:
        _validate_file(ref["file"], key=key)
    return ref


#: Keys a reference's ``file`` object accepts today.
SUPPORTED_FILE_KEYS: tuple[str, ...] = ("name",)
#: Keys reserved for a later release — multi-field templated files, whole-secret
#: JSON, base64 decoding and a file mode. Refused for now with a message that
#: says so, rather than silently ignored: an older server must never drop a
#: newer client's instruction and write a different file than was asked for.
#: Adding one means moving it to SUPPORTED_FILE_KEYS and teaching
#: :func:`render_file_content` what it does.
RESERVED_FILE_KEYS: tuple[str, ...] = ("template", "format", "encoding", "mode")


def _validate_file(spec: object, *, key: str) -> None:
    """Validate the optional ``file`` object of a reference (#1619)."""
    if not isinstance(spec, dict):
        raise VaultSourceError(f"variable {key!r} has a vault `file` that is not an object")
    extra = set(spec) - set(SUPPORTED_FILE_KEYS)
    unknown = sorted(extra - set(RESERVED_FILE_KEYS))
    reserved = sorted(extra & set(RESERVED_FILE_KEYS))
    if unknown:
        raise VaultSourceError(
            f"variable {key!r} has a vault `file` with unknown keys: "
            f"{', '.join(unknown)} (only `name` is supported)"
        )
    if reserved:
        raise VaultSourceError(
            f"variable {key!r} has a vault `file` using {', '.join(reserved)}, which is "
            "reserved for a later release and not supported yet (only `name` is supported)"
        )
    try:
        validate_name(file_name(spec, key=key))
    except FilePathError as e:
        raise VaultSourceError(
            f"variable {key!r}: vault file name {file_name(spec, key=key)!r} is invalid: {e}"
        ) from e


def file_name(spec: dict, *, key: str) -> str:
    """The configured file name, defaulting to the variable key."""
    name = spec.get("name")
    return key if name is None else name


def looks_like_file_reference(raw: str | None) -> bool:
    """Whether a *static* value is really a Vault reference asking for a file.

    Used to refuse ``file`` on a non-vault source, which would otherwise deliver
    the reference JSON itself as the literal value. Narrow on purpose: it needs
    a ``file`` key *and* either ``"source": "vault"`` or all three coordinates,
    so an ordinary JSON value that happens to have a ``file`` key is untouched.
    """
    try:
        obj = json.loads(raw or "")
    except (ValueError, TypeError):
        return False
    if not isinstance(obj, dict) or "file" not in obj:
        return False
    return obj.get("source") == VALUE_SOURCE_VAULT or all(
        k in obj for k in ("mount", "path", "field")
    )


@dataclass
class VaultDelivery:
    """What resolution produces for a run.

    ``values`` maps each vault-sourced variable key to what is delivered as its
    value: the secret itself, or — for a file-mode variable — the absolute path
    of the file. ``files`` carries each file's content, and is the only place a
    file-mode secret appears; it rides the ``vault-files`` runs/next attribute
    into the per-run Secret and nowhere else.
    """

    values: dict[str, str] = field(default_factory=dict)
    files: list[dict] = field(default_factory=list)


async def resolve_vault_variables(resolved: list, settings: Settings) -> dict[str, str]:
    """Resolve every vault-sourced variable to the value it is delivered as.

    A file-mode variable maps to its file's path, never to the secret, so a
    caller that knows nothing of ``vault-files`` still cannot put a file's
    content into env or tfvars.
    """
    return (await resolve_vault_delivery(resolved, settings)).values


async def resolve_vault_delivery(resolved: list, settings: Settings) -> VaultDelivery:
    """Resolve every vault-sourced variable for delivery.

    ``resolved`` is the full ``ResolvedVariable`` list from ``resolve_variables``
    — already after variable-set precedence, so two variables with one key have
    collapsed to the winner and only distinct keys can collide on a file path.
    """
    wanted = [
        v for v in resolved if getattr(v, "value_source", VALUE_SOURCE_STATIC) == VALUE_SOURCE_VAULT
    ]
    if not wanted:
        return VaultDelivery()

    cfg = settings.vault
    if not cfg.enabled:
        raise VaultSourceError(
            f"{len(wanted)} variable(s) reference Vault but the Vault value source is "
            "disabled (api.config.vault.enabled)"
        )

    # Validate every reference and every file target before reading anything:
    # a run that is going to fail on a clash must not mint dynamic credentials
    # on the way there.
    refs = {v.key: parse_reference(v.value, key=v.key) for v in wanted}
    file_names: dict[str, str] = {}
    for v in wanted:
        spec = refs[v.key].get("file")
        if spec is None:
            continue
        # `structured` is the flag's name on this line (#1435); `hcl` is read too
        # so a caller still handing over the old attribute cannot slip past.
        if getattr(v, "structured", False) or getattr(v, "hcl", False):
            raise VaultSourceError(
                f"variable {v.key!r} uses vault file delivery with structured "
                "(formerly hcl) enabled; its value is the file's path, which is not "
                "a typed expression"
            )
        file_names[v.key] = file_name(spec, key=v.key)
    try:
        check_collisions(sorted(file_names.items()))
    except FilePathError as e:
        raise VaultSourceError(str(e)) from e

    insts = {}
    for v in wanted:
        iname = refs[v.key].get("vault")
        inst = cfg.resolve_instance(iname)
        if inst is None:
            if iname:
                raise VaultSourceError(
                    f"variable {v.key!r} references unknown vault instance {iname!r}"
                )
            raise VaultSourceError(
                f"variable {v.key!r} omits `vault` but several instances are configured "
                "and none is marked default — name the instance explicitly"
            )
        insts[v.key] = inst

    # One read per distinct secret. A dynamic engine mints a new credential on
    # every request, so reading per variable gave a certificate and a key from
    # two different issues, or an access key and a secret from two leases.
    # Variables naming the same secret now take their fields from one response.
    groups: dict[tuple, list] = {}
    for v in wanted:
        groups.setdefault(_read_identity(insts[v.key].name, refs[v.key]), []).append(v)

    out = VaultDelivery()
    for members in groups.values():
        keys = [m.key for m in members]
        ref = refs[keys[0]]
        inst = insts[keys[0]]
        where = secret_path(ref["mount"], ref["path"])
        try:
            secret = await read_secret_data(
                inst,
                mount=ref["mount"],
                path=ref["path"],
                engine=ref.get("engine", "kv2"),
                method=str(ref.get("method", "GET")).upper(),
                data=ref.get("data"),
                timeout=cfg.timeout_seconds,
                static_token=_secret_for(inst.name),
            )
        except VaultUnavailable as e:
            # Transient: the reference is fine, Vault is not answering. Signal
            # it distinctly so the caller can leave the run queued rather than
            # destroy it over a restart.
            logger.warning(
                "vault is unavailable; leaving the run for a later claim",
                keys=keys,
                instance=inst.name,
            )
            raise VaultTransient(f"{_variables(keys)}: {e}") from e
        except Exception as e:
            # Deliberately broad. VaultError is the expected shape, but anything
            # escaping here reaches the run dispatcher as a 500 and leaves the
            # run claimed in `planning` until the hour-long stale sweep — so one
            # brief Vault outage strands every queued run in the estate. Every
            # failure must become a failed run carrying a cause instead.
            # Deliberately not logging any part of a response body — only the
            # variable names, the coordinates and the cause.
            logger.error(
                "vault variable could not be resolved",
                keys=keys,
                instance=inst.name,
                mount=ref["mount"],
                path=ref["path"],
            )
            raise VaultSourceError(f"{_variables(keys)}: {e}") from e

        for v in members:
            field_name = refs[v.key]["field"]
            try:
                if v.key in file_names:
                    value = render_file_content(secret, refs[v.key]["file"], field_name, where)
                else:
                    value = extract_field(secret, field_name, where=where)
            except VaultError as e:
                raise VaultSourceError(f"variable {v.key!r}: {e}") from e

            if v.key not in file_names:
                out.values[v.key] = value
                continue
            size = len(value.encode("utf-8"))
            if size > MAX_FILE_BYTES:
                # Names and sizes only — never any part of the value.
                raise VaultSourceError(
                    f"variable {v.key!r}: the Vault value is {size} bytes, over the "
                    f"{MAX_FILE_BYTES // 1024} KiB limit for a file"
                )
            fname = file_names[v.key]
            out.files.append({"key": v.key, "name": fname, "value": value})
            # The variable itself carries the path. Whatever the listener does
            # with `vault-files`, the secret is never in env or tfvars.
            out.values[v.key] = target_path(fname)
    return out


def render_file_content(secret: dict, file_spec: dict, field: str, where: str) -> str:
    """The content of a file-mode variable, from the whole shared response.

    Today that is the reference's one ``field``. This is the seam for the
    reserved ``file`` keys (RESERVED_FILE_KEYS): a template over several fields,
    the whole secret as JSON, or base64 decoding all produce the content from
    ``secret`` — the complete response of the single read — rather than one
    field, without the resolver changing shape.
    """
    return extract_field(secret, field, where=where)


def _read_identity(instance: str, ref: dict) -> tuple:
    """What makes two references the same Vault request.

    Mirrors what the client sends: kv-v2 is always a GET with no body whatever
    the reference says, and a GET never sends ``data``, so neither can split a
    read. A POST body is canonicalised (sorted keys) so key order cannot either.
    """
    engine = ref.get("engine", "kv2")
    method = "GET" if engine == "kv2" else str(ref.get("method", "GET")).upper()
    body = (
        json.dumps(ref.get("data") or {}, sort_keys=True, separators=(",", ":"))
        if method == "POST"
        else None
    )
    return (
        instance,
        engine,
        str(ref["mount"]).strip("/"),
        str(ref["path"]).strip("/"),
        method,
        body,
    )


def _variables(keys: list[str]) -> str:
    """`variable 'A'` or `variables 'A', 'B'` — for a message about one read."""
    if len(keys) == 1:
        return f"variable {keys[0]!r}"
    return "variables " + ", ".join(repr(k) for k in keys)


def _secret_for(instance_name: str) -> str | None:
    """The approle secret_id / static token for an instance, from the env.

    Injected by the chart as ``TERRAPOD_VAULT_{NAME}_SECRET`` via secretKeyRef,
    the same shape the SSO connectors use for their client secrets. Kubernetes
    auth needs none of this.
    """
    import os

    env_key = f"TERRAPOD_VAULT_{instance_name.upper().replace('-', '_')}_SECRET"
    return os.environ.get(env_key) or None
