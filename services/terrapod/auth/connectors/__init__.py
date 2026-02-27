"""SSO connector registry.

Manages initialized SSO connectors, keyed by provider name.
"""

import os

from terrapod.auth.sso import SSOConnector
from terrapod.config import settings
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

# Registry of initialized connectors
_connectors: dict[str, SSOConnector] = {}

# Environment variable overrides for OIDC client secrets.
# Keyed by provider name (uppercase), e.g. TERRAPOD_AUTH0_CLIENT_SECRET.
_SECRET_ENV_PREFIX = "TERRAPOD_"
_SECRET_ENV_SUFFIX = "_CLIENT_SECRET"


def init_connectors() -> None:
    """Initialize all configured auth connectors.

    Called during application startup (lifespan handler).
    Registers local, OIDC, and SAML connectors based on config.
    """
    from terrapod.auth.connectors.local import LocalConnector
    from terrapod.auth.connectors.oidc import OIDCConnector
    from terrapod.auth.connectors.saml import SAMLConnector

    _connectors.clear()

    # Register local connector when local auth is enabled
    if settings.auth.local_enabled:
        connector = LocalConnector()
        _connectors[connector.name] = connector
        logger.info("Registered local provider")

    for oidc_config in settings.auth.sso.oidc:
        # Inject client_secret from env var if not set in config.
        # Convention: TERRAPOD_{NAME}_CLIENT_SECRET
        env_key = f"{_SECRET_ENV_PREFIX}{oidc_config.name.upper()}{_SECRET_ENV_SUFFIX}"
        env_secret = os.environ.get(env_key, "")
        if env_secret and not oidc_config.client_secret:
            oidc_config.client_secret = env_secret
            logger.debug(
                "Loaded client_secret from env", provider=oidc_config.name, env_var=env_key
            )

        connector = OIDCConnector(oidc_config)
        _connectors[connector.name] = connector
        logger.info("Registered OIDC provider", provider=connector.name)

    for saml_config in settings.auth.sso.saml:
        connector = SAMLConnector(saml_config)
        _connectors[connector.name] = connector
        logger.info("Registered SAML provider", provider=connector.name)

    logger.info("Auth connectors initialized", count=len(_connectors))


def get_connector(name: str) -> SSOConnector | None:
    """Get a connector by provider name."""
    return _connectors.get(name)


def list_connectors() -> list[dict[str, str]]:
    """List all configured providers (name, type, display_name)."""
    return [
        {"name": c.name, "type": c.provider_type, "display_name": c.display_name}
        for c in _connectors.values()
    ]


def get_default_connector() -> SSOConnector | None:
    """Get the default SSO connector, if configured."""
    default_name = settings.auth.sso.default_provider
    if default_name:
        return _connectors.get(default_name)
    # Fall back to first configured connector
    if _connectors:
        return next(iter(_connectors.values()))
    return None
