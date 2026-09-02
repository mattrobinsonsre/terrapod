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

**Failure is fatal, unlike git-auth.** ``git_auth_service`` drops an
unresolvable credential with a warning so one bad entry cannot fail a run. That
is the wrong trade here: a silently absent credential leaves Terraform to fail
somewhere confusing, or to fall back to another identity and act with
credentials nobody chose. A reference that cannot be resolved raises, and the
run fails at the point of resolution with a message naming the cause.
"""

from __future__ import annotations

import json

import structlog

from terrapod.config import Settings
from terrapod.services.vault_client import VaultError, read_secret

logger = structlog.get_logger("vault_source")

VALUE_SOURCE_STATIC = "static"
VALUE_SOURCE_VAULT = "vault"
VALUE_SOURCES = {VALUE_SOURCE_STATIC, VALUE_SOURCE_VAULT}


class VaultSourceError(RuntimeError):
    """A vault-sourced variable could not be resolved. Fails the run."""


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
    return ref


async def resolve_vault_variables(resolved: list, settings: Settings) -> dict[str, str]:
    """Resolve every vault-sourced variable to its concrete value.

    ``resolved`` is the full ``ResolvedVariable`` list from
    ``resolve_variables``. Returns ``{key: concrete_value}`` for the vault-backed
    ones only; the caller substitutes them before delivery.
    """
    wanted = [
        v for v in resolved if getattr(v, "value_source", VALUE_SOURCE_STATIC) == VALUE_SOURCE_VAULT
    ]
    if not wanted:
        return {}

    cfg = settings.vault
    if not cfg.enabled:
        raise VaultSourceError(
            f"{len(wanted)} variable(s) reference Vault but the Vault value source is "
            "disabled (api.config.vault.enabled)"
        )

    out: dict[str, str] = {}
    for v in wanted:
        ref = parse_reference(v.value, key=v.key)
        name = ref.get("vault")
        inst = cfg.resolve_instance(name)
        if inst is None:
            if name:
                raise VaultSourceError(
                    f"variable {v.key!r} references unknown vault instance {name!r}"
                )
            raise VaultSourceError(
                f"variable {v.key!r} omits `vault` but several instances are configured "
                "and none is marked default — name the instance explicitly"
            )
        try:
            out[v.key] = await read_secret(
                inst,
                mount=ref["mount"],
                path=ref["path"],
                field=ref["field"],
                engine=ref.get("engine", "kv2"),
                method=str(ref.get("method", "GET")).upper(),
                data=ref.get("data"),
                timeout=cfg.timeout_seconds,
                static_token=_secret_for(inst.name),
            )
        except VaultError as e:
            # Deliberately not logging the reference's field value or any part of
            # a response body — only the coordinates and the cause.
            logger.error(
                "vault variable could not be resolved",
                key=v.key,
                instance=inst.name,
                mount=ref["mount"],
                path=ref["path"],
            )
            raise VaultSourceError(f"variable {v.key!r}: {e}") from e
    return out


def _secret_for(instance_name: str) -> str | None:
    """The approle secret_id / static token for an instance, from the env.

    Injected by the chart as ``TERRAPOD_VAULT_{NAME}_SECRET`` via secretKeyRef,
    the same shape the SSO connectors use for their client secrets. Kubernetes
    auth needs none of this.
    """
    import os

    env_key = f"TERRAPOD_VAULT_{instance_name.upper().replace('-', '_')}_SECRET"
    return os.environ.get(env_key) or None
