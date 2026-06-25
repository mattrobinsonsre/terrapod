"""Shared PKCE (RFC 7636) S256 helper.

Used on both sides of the auth surface: where Terrapod is the OAuth2 *client*
(the upstream OIDC connector sending PKCE to the IdP) and where it is the
*server* (verifying the CLI login's ``code_verifier`` against the stored
``code_challenge``). Keeping the derivation in one place avoids the two
implementations drifting.
"""

import base64
import hashlib


def s256_challenge(verifier: str) -> str:
    """Return the S256 PKCE ``code_challenge`` for a ``code_verifier``.

    ``base64url(sha256(verifier))`` with trailing ``=`` padding stripped, per
    RFC 7636 section 4.2.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
