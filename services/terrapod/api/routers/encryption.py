"""Encryption-at-rest status endpoint (admin only).

UX/consumer contract: consumed by go-terrapod (GetEncryptionStatus) and surfaced
read-only. Reports whether app-layer encryption is enabled and — the headline
durability signal — whether everything is currently **decryptable**. Read-only;
no secrets or key material are ever returned.
"""

from fastapi import APIRouter, Depends

from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.crypto.service import get_encryption

router = APIRouter(prefix="/admin", tags=["encryption"])


@router.get("/encryption")
async def get_encryption_status(
    user: AuthenticatedUser = Depends(require_admin),
) -> dict:
    """Return encryption-at-rest health (admin only).

    ``decryptable`` is the key field: false means the platform booted but the
    active key can't read the canary back — investigate immediately
    (docs/encryption-at-rest.md#recovery).
    """
    return {"data": {"type": "encryption-status", "attributes": get_encryption().status()}}
