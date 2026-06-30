"""Async helpers for state-file encryption at rest (#635).

State blobs can be multi-MB, so the AES-GCM encrypt/decrypt MUST run off the event
loop (CLAUDE.md #13). These thin wrappers offload the synchronous service calls to
a worker thread and are the single entry point every state read/write site uses,
so encryption coverage can't drift between the CLI, runner, and management paths:

  * write a state object  →  put encrypted bytes via ``encrypt_state_bytes``
  * read a state object   →  ``decrypt_state_bytes`` on the fetched bytes

When encryption is disabled (the default) both are cheap passthroughs and the byte
stream is untouched — a plaintext blob with no ``TPENC1`` magic reads straight
back, so enabling/disabling is a migration, not a hard cutover.
"""

import asyncio

from terrapod.crypto.service import get_encryption


def state_encryption_active() -> bool:
    """True when state writes should be encrypted (enabled + active DEK loaded).

    Call sites that stream state to/from storage branch on this: when off they
    keep the existing zero-copy streaming path (presigned download / streamed
    upload); when on they buffer-and-(de)crypt through the API.
    """
    return get_encryption().state_encryption_active


async def encrypt_state_bytes(data: bytes) -> bytes:
    """Encrypt a state blob under the active DEK (passthrough when disabled)."""
    svc = get_encryption()
    if not svc.state_encryption_active:
        return data
    return await asyncio.to_thread(svc.encrypt_state, data)


async def decrypt_state_bytes(blob: bytes) -> bytes:
    """Decrypt a state blob (passthrough for legacy/plaintext blobs).

    Raises if the blob is encrypted under a DEK version we cannot load — never
    returns ciphertext as if it were plaintext state.
    """
    from terrapod.crypto import envelope

    if not envelope.is_encrypted_blob(blob):
        return blob
    return await asyncio.to_thread(get_encryption().decrypt_state, blob)
