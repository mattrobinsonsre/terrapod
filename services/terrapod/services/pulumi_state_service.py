"""A Pulumi stack's deployment, as Terrapod stores it and hands it to a runner (#1576).

Terrapod keeps a Pulumi stack's state as a state version holding the bare
deployment — the `deployment` object of `pulumi stack export`, with every secret
sealed by Terrapod's own encryption (the `service` secrets provider). Two
consumers read and write it:

- **local mode** — the CLI on an operator's machine, through the service surface
  (`routers/pulumi_service.py`), which seals and opens secrets one call at a time;
- **agent runs** — a runner Job, which does NOT use that surface. It runs Pulumi
  against a file backend inside the Job, exactly as a Terraform run keeps
  `terraform.tfstate` in its working directory: the deployment is handed over at
  the start through the run's artifact API and handed back once at the end.

This module is the API half of that handover. The runner imports a deployment
into a stack it has just created with a throwaway passphrase, so the secrets it
receives must be **plaintext** — the passphrase stack cannot open service
ciphertext — and what it sends back is `stack export --show-secrets`, which this
module seals again before it is stored. The provider block is replaced on the way
out and restored on the way in: the runner substitutes its own, and what is
stored must name the provider that can actually open the stored ciphertext.

Deliberately free of I/O, so it can run in a worker thread (CLAUDE.md #13) over a
multi-MB deployment without touching the event loop.
"""

from __future__ import annotations

import base64
from typing import Any

#: How Pulumi marks a secret inside a deployment: a map carrying this key with
#: this value, plus either `ciphertext` or `plaintext`. Both are fixed constants
#: of Pulumi's deployment format (`apitype.SecretV1`), not something we choose.
SECRET_SIG_KEY = "4dabf18193072939515e22adb298388d"
SECRET_SIG = "1b47061264138c4ac30d75fd1eb44270"

#: The provider whose ciphertext Terrapod can open — its own.
SERVICE_PROVIDER = "service"


class UnreadableSecretsError(Exception):
    """The stored deployment holds ciphertext Terrapod has no key for.

    A stack moved to a passphrase or cloud-KMS provider with
    `pulumi stack change-secrets-provider` keeps its secrets sealed under a key
    only the operator's CLI holds. An agent run cannot open them, and handing it
    ciphertext it cannot open would only move the failure somewhere less clear.
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(provider)


class SealedSecretInUploadError(ValueError):
    """An uploaded deployment still carries ciphertext.

    The runner's passphrase dies with its Pod, so a secret sealed under it is
    unrecoverable once stored. An export without `--show-secrets` is refused
    rather than silently destroying every secret in the stack.
    """


def _is_secret(node: dict[str, Any]) -> bool:
    return node.get(SECRET_SIG_KEY) == SECRET_SIG


def _map_secrets(node: Any, fn: Any) -> Any:
    """A copy of `node` with every secret map replaced by `fn(secret)`.

    Secrets can appear anywhere a property value can — resource inputs and
    outputs, stack outputs, nested inside lists and objects — so the whole tree
    is walked rather than the handful of places they usually sit.
    """
    if isinstance(node, dict):
        if _is_secret(node):
            return fn(node)
        return {k: _map_secrets(v, fn) for k, v in node.items()}
    if isinstance(node, list):
        return [_map_secrets(v, fn) for v in node]
    return node


def provider_of(deployment: dict[str, Any] | None) -> dict[str, Any] | None:
    """The deployment's `secrets_providers` block, if it has one."""
    if not deployment:
        return None
    provider = deployment.get("secrets_providers")
    return provider if isinstance(provider, dict) else None


def reveal_secrets(deployment: dict[str, Any], decrypt: Any) -> dict[str, Any]:
    """The deployment with its secrets opened and its provider block removed.

    `decrypt` is the encryption service's `decrypt`. A secret's ciphertext is the
    base64 of what that service sealed — the same string the service surface's
    `encrypt` handed the CLI — and its plaintext is the JSON text Pulumi sealed.

    Raises `UnreadableSecretsError` when a secret is sealed by a provider other
    than Terrapod's own. A deployment with no secrets opens whatever its provider.
    """
    provider = (provider_of(deployment) or {}).get("type", "")

    def _open(secret: dict[str, Any]) -> dict[str, Any]:
        if "ciphertext" not in secret:
            return secret
        if provider != SERVICE_PROVIDER:
            raise UnreadableSecretsError(provider or "unknown")
        sealed = base64.b64decode(secret["ciphertext"]).decode()
        return {SECRET_SIG_KEY: SECRET_SIG, "plaintext": decrypt(sealed)}

    body = {k: v for k, v in deployment.items() if k != "secrets_providers"}
    result: dict[str, Any] = _map_secrets(body, _open)
    return result


def seal_secrets(
    deployment: dict[str, Any], encrypt: Any, provider: dict[str, Any]
) -> dict[str, Any]:
    """The deployment with every plaintext secret sealed and `provider` set.

    The inverse of `reveal_secrets`, and the only form Terrapod stores: whatever
    provider block arrived is discarded, because it names the runner's
    passphrase, which no longer exists.
    """

    def _seal(secret: dict[str, Any]) -> dict[str, Any]:
        if "plaintext" not in secret:
            raise SealedSecretInUploadError(
                "the deployment carries a sealed secret; export it with --show-secrets"
            )
        sealed = encrypt(secret["plaintext"])
        return {
            SECRET_SIG_KEY: SECRET_SIG,
            "ciphertext": base64.b64encode(sealed.encode()).decode(),
        }

    body = {k: v for k, v in deployment.items() if k != "secrets_providers"}
    sealed_body: dict[str, Any] = _map_secrets(body, _seal)
    sealed_body["secrets_providers"] = provider
    return sealed_body


def service_provider(
    prior: dict[str, Any] | None, *, url: str, project: str, stack: str
) -> dict[str, Any]:
    """The provider block to store alongside a deployment sealed by Terrapod.

    The prior block is kept when it already names the service provider: a local
    CLI reads it to find the backend that can open the secrets, so changing it
    under an operator who is logged in to that URL would break their next read.
    Only a stack with no service block yet — a first deployment, written by an
    agent run — gets a fresh one, pointing at the deployment's canonical surface.
    """
    if prior and prior.get("type") == SERVICE_PROVIDER:
        return prior
    return {
        "type": SERVICE_PROVIDER,
        "state": {"url": url, "owner": "default", "project": project, "stack": stack},
    }
