"""Runtime app ↔ schema skew guard (#544).

A new API pod can start before/without its migration applied — a failed Helm
pre-upgrade hook that let the rollout proceed, a bare `kubectl set image`, or
ArgoCD sync ordering. The new code then queries a not-yet-existing column and
500s **every** request against that table. This module lets `/ready` report a
schema-behind pod as *not ready* (so it's pulled from the load balancer) instead
of silently erroring, and lets startup log a loud warning.

It compares the revision recorded in the DB's ``alembic_version`` table against
the head revision the shipped ``alembic/`` scripts declare. Alembic's revision
chain is linear, so a single head is expected; a DB revision that is not the
code head means the schema and the code are out of step.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from sqlalchemy import text

import terrapod
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger

logger = get_logger(__name__)


def _find_alembic_base() -> Path | None:
    """Locate the directory holding ``alembic.ini`` + ``alembic/``.

    Robust across the API image (``/app``) and the repo/tests layout (repo root)
    by walking up from the installed ``terrapod`` package.
    """
    # `terrapod` is a PEP 420 namespace package (no __init__.py), so __file__
    # is None — use __path__ for the package directory instead.
    pkg_paths = list(getattr(terrapod, "__path__", []))
    if not pkg_paths:
        return None
    start = Path(pkg_paths[0]).resolve().parent
    for base in (start, *start.parents):
        if (base / "alembic.ini").is_file() and (base / "alembic").is_dir():
            return base
    return None


@lru_cache(maxsize=1)
def code_head_revisions() -> frozenset[str]:
    """The head revision(s) the shipped alembic scripts declare (cached).

    Empty frozenset if the scripts can't be located — callers treat that as
    "can't tell, don't block". Reads the filesystem; warm it once at startup
    (see ``check_schema_at_startup``) so the async ``/ready`` path never does
    this sync I/O in the event loop.
    """
    base = _find_alembic_base()
    if base is None:
        return frozenset()
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(base / "alembic.ini"))
        cfg.set_main_option("script_location", str(base / "alembic"))
        return frozenset(ScriptDirectory.from_config(cfg).get_heads())
    except Exception:
        logger.warning("Could not read alembic head revision", exc_info=True)
        return frozenset()


async def db_current_revision() -> str | None:
    """The revision recorded in the DB's ``alembic_version`` table, or None if
    the table is absent/empty (fresh DB, migrations never run)."""
    try:
        async with get_db_session() as db:
            row = (await db.execute(text("SELECT version_num FROM alembic_version"))).first()
            return row[0] if row else None
    except Exception:
        return None


async def schema_is_current() -> tuple[bool, str]:
    """Return (ok, detail): does the applied DB revision match the code head?

    ok=True when they match — or when the code head can't be determined (we
    don't block a pod on our own inability to read the scripts; the DB/Redis
    checks still gate readiness). ok=False when the DB revision is missing or
    differs from the code head (the schema-skew signal)."""
    heads = code_head_revisions()
    if not heads:
        return True, "unknown (alembic scripts not found; not gating readiness)"
    current = await db_current_revision()
    if current is None:
        return False, "no alembic_version row — migrations not applied"
    if current in heads:
        return True, current
    return False, f"schema at {current}, code head {'/'.join(sorted(heads))}"


async def check_schema_at_startup() -> None:
    """Warm the code-head cache and log a loud warning if the schema is behind.

    Called once from the app lifespan. Does not raise — a schema-behind pod
    boots (so it can serve `/ready` = not-ready) rather than crash-looping."""
    import asyncio

    await asyncio.to_thread(code_head_revisions)  # warm cache off the event loop
    ok, detail = await schema_is_current()
    if ok:
        logger.info("Schema version check passed", detail=detail)
    else:
        logger.warning(
            "SCHEMA SKEW: the database schema does not match this app's expected "
            "migration head — this pod will report NOT READY until the migration "
            "is applied. Run the Alembic migration Job / helm upgrade hook.",
            detail=detail,
        )
