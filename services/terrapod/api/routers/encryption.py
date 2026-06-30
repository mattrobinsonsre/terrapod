"""Encryption-at-rest status endpoint (admin only).

UX/consumer contract: consumed by go-terrapod (GetEncryptionStatus) and surfaced
read-only. Reports whether app-layer encryption is enabled and — the headline
durability signal — whether everything is currently **decryptable**. Read-only;
no secrets or key material are ever returned.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.crypto.service import get_encryption, rotate_dek
from terrapod.db.session import get_db

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


@router.post("/encryption/rotate-dek")
async def rotate_data_encryption_key(
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Mint a new active DEK (admin only). Prior versions are retained so all
    existing ciphertext stays decryptable; run the encrypt migration to re-encrypt
    old rows under the new key. 409 when encryption is disabled.
    """
    try:
        version = await rotate_dek(db)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "data": {
            "type": "encryption-status",
            "attributes": {**get_encryption().status(), "rotated_to_version": version},
        }
    }
