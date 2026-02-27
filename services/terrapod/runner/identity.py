"""Listener identity management — local vs remote registration."""

import os
import uuid

from terrapod.config import load_runner_config
from terrapod.logging_config import get_logger

logger = get_logger(__name__)


class ListenerIdentity:
    """Identity for a runner listener.

    Local listeners auto-register with the default pool (no join token).
    Remote listeners join via token exchange and use certificate auth.
    """

    def __init__(
        self,
        listener_id: uuid.UUID,
        name: str,
        pool_id: uuid.UUID,
        mode: str,  # "local" or "remote"
        api_url: str = "",
        certificate_pem: str = "",
        private_key_pem: str = "",
        ca_cert_pem: str = "",
    ):
        self.listener_id = listener_id
        self.name = name
        self.pool_id = pool_id
        self.mode = mode
        self.api_url = api_url
        self.certificate_pem = certificate_pem
        self.private_key_pem = private_key_pem
        self.ca_cert_pem = ca_cert_pem

    @property
    def is_local(self) -> bool:
        return self.mode == "local"


async def establish_identity() -> ListenerIdentity:
    """Establish listener identity based on environment.

    TERRAPOD_LISTENER_MODE=local → auto-register with default pool via DB.
    TERRAPOD_LISTENER_MODE=remote → join via token from TERRAPOD_JOIN_TOKEN env.
    """
    mode = os.environ.get("TERRAPOD_LISTENER_MODE", "local")
    name = os.environ.get("TERRAPOD_LISTENER_NAME", "local")

    if mode == "local":
        return await _establish_local_identity(name)
    else:
        return await _establish_remote_identity(name)


async def _establish_local_identity(name: str) -> ListenerIdentity:
    """Register as a local listener with direct DB access."""
    from terrapod.db.session import get_db_session
    from terrapod.services.agent_pool_service import register_local_listener

    runner_config = load_runner_config()
    runner_defs = [d.name for d in runner_config.definitions]

    async with get_db_session() as db:
        listener = await register_local_listener(
            db, listener_name=name, runner_definitions=runner_defs
        )

    logger.info(
        "Established local listener identity",
        listener_id=str(listener.id),
        name=listener.name,
        pool_id=str(listener.pool_id),
    )

    return ListenerIdentity(
        listener_id=listener.id,
        name=listener.name,
        pool_id=listener.pool_id,
        mode="local",
    )


async def _establish_remote_identity(name: str) -> ListenerIdentity:
    """Join via token exchange with the API server."""
    import httpx

    api_url = os.environ.get("TERRAPOD_API_URL", "http://localhost:8000")
    join_token = os.environ.get("TERRAPOD_JOIN_TOKEN", "")
    pool_id = os.environ.get("TERRAPOD_POOL_ID", "")

    if not join_token or not pool_id:
        raise RuntimeError("Remote mode requires TERRAPOD_JOIN_TOKEN and TERRAPOD_POOL_ID")

    runner_config = load_runner_config()
    runner_defs = [d.name for d in runner_config.definitions]

    async with httpx.AsyncClient(base_url=api_url, timeout=30) as client:
        response = await client.post(
            f"/api/v2/agent-pools/{pool_id}/listeners/join",
            json={
                "join_token": join_token,
                "name": name,
                "runner_definitions": runner_defs,
            },
        )
        response.raise_for_status()
        data = response.json()["data"]

    listener_id = uuid.UUID(data["listener_id"])

    # Save certificate to filesystem for restart persistence
    cert_dir = os.environ.get("TERRAPOD_CERT_DIR", "/var/lib/terrapod/certs")
    os.makedirs(cert_dir, exist_ok=True)

    cert_path = os.path.join(cert_dir, "listener.crt")
    key_path = os.path.join(cert_dir, "listener.key")
    ca_path = os.path.join(cert_dir, "ca.crt")

    with open(cert_path, "w") as f:
        f.write(data["certificate"])
    with open(key_path, "w") as f:
        f.write(data["private_key"])
    os.chmod(key_path, 0o600)
    with open(ca_path, "w") as f:
        f.write(data["ca_certificate"])

    logger.info(
        "Joined via token exchange",
        listener_id=str(listener_id),
        name=name,
    )

    return ListenerIdentity(
        listener_id=listener_id,
        name=name,
        pool_id=uuid.UUID(pool_id.removeprefix("apool-")),
        mode="remote",
        api_url=api_url,
        certificate_pem=data["certificate"],
        private_key_pem=data["private_key"],
        ca_cert_pem=data["ca_certificate"],
    )
