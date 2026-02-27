"""
Database session management for Terrapod API server.

Provides async SQLAlchemy session factory for database access.
Single engine (no read replica for MVP).
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from terrapod.config import settings
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

# Primary engine — created lazily in init_db()
_engine = None
_async_session_factory = None


async def init_db() -> None:
    """Initialize database connection pool."""
    global _engine, _async_session_factory  # noqa: PLW0603
    logger.info("Initializing database connection")

    _engine = create_async_engine(
        str(settings.database_url),
        echo=settings.debug,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )

    _async_session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

    async with _engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("Database connection established")


async def close_db() -> None:
    """Close database connection pool."""
    global _engine, _async_session_factory  # noqa: PLW0603
    if _engine is not None:
        logger.info("Closing database connection pool")
        await _engine.dispose()
        _engine = None
        _async_session_factory = None


async def get_db() -> AsyncGenerator[AsyncSession]:
    """
    Dependency that provides a read-write database session.

    Usage:
        @router.post("/users")
        async def create_user(db: AsyncSession = Depends(get_db)):
            ...
    """
    if _async_session_factory is None:
        raise RuntimeError("Database not initialized — call init_db() first")

    async with _async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession]:
    """Context manager for non-dependency database access (e.g. lifespan startup).

    Unlike get_db(), this is not a FastAPI dependency — it's used in places
    that don't have the request/dependency lifecycle (startup, background tasks).
    """
    if _async_session_factory is None:
        raise RuntimeError("Database not initialized — call init_db() first")

    async with _async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db_health() -> bool:
    """Check database health for readiness probe."""
    try:
        if _engine is None:
            return False
        async with _engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error("Database health check failed", error=str(e))
        return False
