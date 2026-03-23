"""Listener identity management — join via token exchange."""

import os
import uuid

from terrapod.logging_config import get_logger

logger = get_logger(__name__)


class ListenerIdentity:
    """Identity for a runner listener.

    All listeners join a pool via the API using a join token.
    The join flow issues an X.509 certificate used for subsequent API calls.
    Certificates are saved to disk for restart persistence.
    """

    def __init__(
        self,
        listener_id: uuid.UUID,
        name: str,
        pool_id: uuid.UUID,
        api_url: str,
        certificate_pem: str,
        private_key_pem: str,
        ca_cert_pem: str,
    ):
        self.listener_id = listener_id
        self.name = name
        self.pool_id = pool_id
        self.api_url = api_url
        self.certificate_pem = certificate_pem
        self.private_key_pem = private_key_pem
        self.ca_cert_pem = ca_cert_pem


async def establish_identity() -> ListenerIdentity:
    """Establish listener identity.

    1. Check for saved certificates from a previous join → resume if valid
    2. Otherwise join via TERRAPOD_JOIN_TOKEN → save certs for next restart
    """
    base_name = os.environ.get("TERRAPOD_LISTENER_NAME", "listener")
    pod_name = os.environ.get("POD_NAME", "")
    name = f"{base_name}-{pod_name}" if pod_name else base_name
    api_url = os.environ.get("TERRAPOD_API_URL", "http://localhost:8000")

    # Try to resume from saved certificates
    identity = _try_resume(name, api_url)
    if identity:
        logger.info(
            "Resumed from saved certificate",
            listener_id=str(identity.listener_id),
            name=identity.name,
        )
        return identity

    # Join via token exchange
    return await _join_via_token(name, api_url)


def _try_resume(name: str, api_url: str) -> ListenerIdentity | None:
    """Try to resume from saved certificates on disk."""
    cert_dir = os.environ.get("TERRAPOD_CERT_DIR", "/var/lib/terrapod/certs")
    cert_path = os.path.join(cert_dir, "listener.crt")
    key_path = os.path.join(cert_dir, "listener.key")
    ca_path = os.path.join(cert_dir, "ca.crt")
    meta_path = os.path.join(cert_dir, "identity.txt")

    if not all(os.path.exists(p) for p in [cert_path, key_path, ca_path, meta_path]):
        return None

    try:
        with open(cert_path) as f:
            cert_pem = f.read()
        with open(key_path) as f:
            key_pem = f.read()
        with open(ca_path) as f:
            ca_pem = f.read()
        with open(meta_path) as f:
            lines = f.read().strip().splitlines()
            meta = dict(line.split("=", 1) for line in lines if "=" in line)

        listener_id = uuid.UUID(meta["listener_id"])
        pool_id = uuid.UUID(meta["pool_id"])

        return ListenerIdentity(
            listener_id=listener_id,
            name=name,
            pool_id=pool_id,
            api_url=api_url,
            certificate_pem=cert_pem,
            private_key_pem=key_pem,
            ca_cert_pem=ca_pem,
        )
    except Exception as e:
        logger.warning("Failed to resume from saved certs, will rejoin", error=str(e))
        return None


async def _join_via_token(name: str, api_url: str) -> ListenerIdentity:
    """Join a pool via token exchange with the API server.

    Retries with exponential backoff if the API is not yet available.
    """
    import asyncio

    import httpx

    join_token = os.environ.get("TERRAPOD_JOIN_TOKEN", "")
    if not join_token:
        raise RuntimeError(
            "TERRAPOD_JOIN_TOKEN is required. "
            "Create an agent pool and generate a join token via the API, "
            "then set the token as TERRAPOD_JOIN_TOKEN."
        )

    max_retries = 30
    backoff = 2

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(base_url=api_url, timeout=30) as client:
                response = await client.post(
                    "/api/v2/agent-pools/join",
                    json={
                        "join_token": join_token,
                        "name": name,
                    },
                )
                response.raise_for_status()
                data = response.json()["data"]

            listener_id = uuid.UUID(data["listener_id"])
            pool_id = uuid.UUID(data["pool_id"])

            # Save certificates and metadata to disk for restart persistence
            _save_certs(
                listener_id=listener_id,
                pool_id=pool_id,
                certificate=data["certificate"],
                private_key=data["private_key"],
                ca_certificate=data["ca_certificate"],
            )

            logger.info(
                "Joined pool via token exchange",
                listener_id=str(listener_id),
                name=name,
                pool_id=str(pool_id),
            )

            return ListenerIdentity(
                listener_id=listener_id,
                name=name,
                pool_id=pool_id,
                api_url=api_url,
                certificate_pem=data["certificate"],
                private_key_pem=data["private_key"],
                ca_cert_pem=data["ca_certificate"],
            )

        except Exception as e:
            if attempt < max_retries - 1:
                wait = min(backoff * (2**attempt), 60)
                logger.warning(
                    "Join failed, retrying",
                    attempt=attempt + 1,
                    wait_seconds=wait,
                    error=str(e),
                )
                await asyncio.sleep(wait)
            else:
                raise RuntimeError(f"Failed to join pool after {max_retries} attempts: {e}") from e

    raise RuntimeError("Unreachable")


def _save_certs(
    listener_id: uuid.UUID,
    pool_id: uuid.UUID,
    certificate: str,
    private_key: str,
    ca_certificate: str,
) -> None:
    """Save certificate material to disk for restart persistence."""
    cert_dir = os.environ.get("TERRAPOD_CERT_DIR", "/var/lib/terrapod/certs")
    os.makedirs(cert_dir, exist_ok=True)

    with open(os.path.join(cert_dir, "listener.crt"), "w") as f:
        f.write(certificate)

    key_path = os.path.join(cert_dir, "listener.key")
    with open(key_path, "w") as f:
        f.write(private_key)
    os.chmod(key_path, 0o600)

    with open(os.path.join(cert_dir, "ca.crt"), "w") as f:
        f.write(ca_certificate)

    with open(os.path.join(cert_dir, "identity.txt"), "w") as f:
        f.write(f"listener_id={listener_id}\n")
        f.write(f"pool_id={pool_id}\n")
