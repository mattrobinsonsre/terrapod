"""Extracting a credential from an Authorization header (#1417).

Terrapod accepts the same set of credentials everywhere — runner tokens, API
tokens, sessions — but not every client can send them the same way, and the
places that absorb that difference kept multiplying.

* Most of the API takes `Authorization: Bearer <token>`.
* Container clients send **Basic**, because a Kubernetes
  `imagePullSecret` is a username and a password and nothing else (#1408).
* **pip** sends Basic too — credentials go in the index URL or `.netrc`, and
  there is no bearer option — while **npm** sends Bearer via `_authToken`.
* **ansible-galaxy** sends `Token <value>` for a `galaxy_server` with a `token`
  (#1482) — neither Bearer nor Basic. Captured from the client rather than read
  from documentation, after every request 401'd against a Bearer-only server.
  That one is opt-in per caller (`allow_token_scheme`) rather than universal;
  see :func:`extract_credential`.

So the *encoding* varies and the credential does not. This module owns the
encoding, and nothing else: each surface still maps a failure to its own error
shape, because a container client needs the distribution spec's error envelope
with a Basic challenge and a JSON:API caller needs neither. Sharing the parsing
without sharing the error handling is what keeps a change to the accepted
credential *types* from having to land in three files again.
"""

from __future__ import annotations

import base64
import binascii

from starlette.requests import Request


def extract_credential(request: Request, *, allow_token_scheme: bool = False) -> str | None:
    """The bearer-equivalent credential in a request, or None.

    Returns None rather than raising when the header is absent or unusable, so
    the caller decides whether anonymous access is permitted for that route
    instead of having the decision made here.

    `allow_token_scheme` additionally accepts `Authorization: Token <value>`,
    which is what `ansible-galaxy` sends for a `galaxy_server` carrying a
    `token` (#1482). It is opt-in rather than universal on purpose: the OCI
    surface has a spec-defined auth flow, and there is an existing test pinning
    that `Token` is *not* honoured there "just because Terrapod will speak it
    elsewhere". Widening every surface to suit one client would quietly overrule
    that decision, so the caller that needs it asks.
    """
    header = request.headers.get("authorization", "")
    if not header:
        return None

    scheme, _, value = header.partition(" ")
    scheme = scheme.lower()

    if scheme == "bearer" or (allow_token_scheme and scheme == "token"):
        return value.strip() or None

    if scheme == "basic":
        try:
            decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
        except binascii.Error, UnicodeDecodeError, ValueError:
            # Malformed base64 is indistinguishable from a wrong password as far
            # as the client is concerned, and saying which would be a small
            # oracle. Treated as "no credential".
            return None
        # Split on the FIRST colon. RFC 7617 forbids a colon in the userid and
        # permits one in the password, so everything after the first separator
        # is the password — which matters immediately: a runner token is
        # `runtok:{run}:{ttl}:{ts}:{sig}` and splitting on the last colon would
        # hand back only the signature.
        _username, separator, password = decoded.partition(":")
        if not separator:
            return None
        return password.strip() or None

    return None
