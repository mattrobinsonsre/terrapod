"""A Pulumi stack's deployment, as Terrapod stores it and opens it (#1522, #1576).

Terrapod keeps a Pulumi stack's state as a state version holding the bare
deployment — the `deployment` object of `pulumi stack export`, with every secret
sealed by Terrapod's own encryption (the `service` secrets provider). The helpers
here are the pure half of reading and writing that, and they serve two callers:

- **the Pulumi service surface** (`routers/pulumi_service.py`), which seals and
  opens secrets **one value at a time** as the CLI asks — `seal_bytes` and
  `open_sealed` — and normalises the stored provider's URL on the way out
  (`with_canonical_service_url`). Every CLI reaches Terrapod this way: an
  operator's laptop after `pulumi login`, and, since #1881, the CLI inside a
  runner Job. Neither is ever handed the whole deployment with its secrets open;
- **the deployment hand-over routes** (`routers/run_artifacts.py`), which move a
  **whole** deployment in and out of a run — `reveal_secrets` opening every
  secret on the way out, `seal_secrets` sealing them again on the way back. That
  pair is what an agent run was built on before #1881, and is kept because
  retiring an API surface is its own decision; nothing in a run calls it now.

Whole-deployment transfer is what needs the provider block swapped: it is removed
on the way out, because the importer seals the stack under its own provider, and
set on the way in, because what is stored must name the provider that can
actually open the stored ciphertext.

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
    only the operator's CLI holds. Terrapod cannot open them on a caller's
    behalf, and handing over ciphertext it cannot open would only move the
    failure somewhere less clear.
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(provider)


class SealedSecretInUploadError(ValueError):
    """An uploaded deployment still carries ciphertext.

    An uploaded deployment is sealed again under Terrapod's own provider, so
    ciphertext arriving from somewhere else is sealed under a key Terrapod has
    no way to reach — unrecoverable once stored. An export made without
    `--show-secrets` is refused rather than silently destroying every secret in
    the stack.
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


def has_plaintext_secrets(deployment: Any) -> bool:
    """Whether any secret in the deployment is in plaintext — `--show-secrets` output."""
    found = False

    def _check(secret: dict[str, Any]) -> dict[str, Any]:
        nonlocal found
        found = found or "plaintext" in secret
        return secret

    _map_secrets(deployment, _check)
    return found


def provider_of(deployment: dict[str, Any] | None) -> dict[str, Any] | None:
    """The deployment's `secrets_providers` block, if it has one."""
    if not deployment:
        return None
    provider = deployment.get("secrets_providers")
    return provider if isinstance(provider, dict) else None


def with_canonical_service_url(
    deployment: dict[str, Any] | None, canonical_url: str | None
) -> dict[str, Any] | None:
    """Serve a `service` provider block naming an address a client can reach.

    **The stored URL is the one the CLI uses, and it does not check it against
    the backend it is logged in to.** Verified against Pulumi's own source:
    `NewServiceSecretsManagerFromState` unmarshals the stored state and passes
    `s.URL` straight to `getServiceSecretsAccount`, which looks up the saved
    credential *for that exact URL*. There is no comparison with the current
    backend and no error on mismatch -- it simply fails later with
    ``could not find access token for <url>, have you logged in?``.

    That makes a stale URL unrecoverable rather than merely wrong, and an agent
    run is how a stack acquires one. A runner reaches the API at its in-cluster
    address, so a stack it writes names `http://terrapod-api:8000/...` in this
    block -- an address no laptop can resolve, let alone hold a token for, and
    one the operator cannot log in to to satisfy the lookup because it does not
    exist outside the cluster. That was true of the agent runs before #1576,
    which is what this was written for (#1580), and it is true again of the
    service-backed ones #1881 restored; nothing on the write path rewrites it,
    because `service_provider`
    keeps any prior block as it is and a checkpoint is stored as the CLI sent it.

    So the URL is normalised **on the way out**, not in storage:

      * every stack is fixed at once, including one that never takes another
        write -- a write-path fix alone would strand exactly the stacks that
        are finished and therefore most likely to be read;
      * nothing stored is altered, so this is reversible by configuration and
        cannot lose an operator's data.

    **Only when `external_url` is configured.** Without it the deployment has
    not declared the address it is reached at, and the fallback is whichever
    host the caller happened to use -- normalising to a guess could rewrite a
    working block to a worse one, so an unset `external_url` leaves the block
    exactly as stored.

    The one case this changes for an operator: a stack whose block names some
    *other* reachable address is normalised to `external_url`. They are not
    stranded, because `external_url` is by definition where this deployment
    answers, so `pulumi login` against it works. That is a far better failure
    than the one it replaces.
    """
    if not canonical_url or not deployment:
        return deployment
    provider = provider_of(deployment)
    if not provider or provider.get("type") != SERVICE_PROVIDER:
        return deployment
    state = provider.get("state")
    if not isinstance(state, dict) or state.get("url") == canonical_url:
        return deployment
    return {
        **deployment,
        "secrets_providers": {
            **provider,
            "state": {**state, "url": canonical_url},
        },
    }


#: Marks a value sealed byte-safely (#1573). The CLI encrypts binary values as
#: well as text, and the envelope layer seals text, so the bytes are base64'd
#: first and the envelope only ever sees ASCII. The marker sits OUTSIDE the
#: envelope, so a value can be told apart without decrypting it. Anything
#: without it was sealed before #1573 and is read the old way: as text, either
#: envelope-sealed or, with encryption at rest off, stored as it was.
BYTES_PREFIX = "terrapod-bytes:v1:"


def seal_bytes(encrypt: Any, raw: bytes) -> str:
    """Seal any value, binary included. `encrypt` is the encryption service's."""
    return BYTES_PREFIX + encrypt(base64.b64encode(raw).decode("ascii"))


def open_sealed(decrypt: Any, sealed: str) -> bytes:
    """The bytes behind a sealed value, whichever way it was sealed.

    Before #1573 only text could be sealed — a binary value failed on the way
    in — so a value without the marker comes back as its UTF-8 bytes, which is
    exactly what the CLI sent.
    """
    if sealed.startswith(BYTES_PREFIX):
        return base64.b64decode(decrypt(sealed[len(BYTES_PREFIX) :]))
    return decrypt(sealed).encode("utf-8", errors="surrogateescape")


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
        # A deployment secret is JSON text, so its bytes decode cleanly.
        return {SECRET_SIG_KEY: SECRET_SIG, "plaintext": open_sealed(decrypt, sealed).decode()}

    body = {k: v for k, v in deployment.items() if k != "secrets_providers"}
    result: dict[str, Any] = _map_secrets(body, _open)
    return result


def seal_secrets(
    deployment: dict[str, Any], encrypt: Any, provider: dict[str, Any]
) -> dict[str, Any]:
    """The deployment with every plaintext secret sealed and `provider` set.

    The inverse of `reveal_secrets`, and the only form Terrapod stores: whatever
    provider block arrived is discarded, because it names the uploader's own
    provider and what is stored is sealed by Terrapod's.
    """

    def _seal(secret: dict[str, Any]) -> dict[str, Any]:
        if "plaintext" not in secret:
            raise SealedSecretInUploadError(
                "the deployment carries a sealed secret; export it with --show-secrets"
            )
        sealed = seal_bytes(encrypt, secret["plaintext"].encode())
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

    The prior block is kept when it already names the service provider: a CLI
    reads it to find the backend that can open the secrets, so changing it under
    an operator who is logged in to that URL would break their next read. Only a
    stack with no service block yet — a first deployment, arriving through the
    hand-over upload route — gets a fresh one, pointing at the deployment's
    canonical surface.
    """
    if prior and prior.get("type") == SERVICE_PROVIDER:
        return prior
    return {
        "type": SERVICE_PROVIDER,
        "state": {"url": url, "owner": "default", "project": project, "stack": stack},
    }
