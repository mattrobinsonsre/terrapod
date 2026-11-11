"""Runtime app ↔ schema skew guard (#544).

A new API pod can start before/without its migration applied — a failed Helm
pre-upgrade hook that let the rollout proceed, a bare `kubectl set image`, or
ArgoCD sync ordering. The new code then queries a not-yet-existing column and
500s **every** request against that table. This module lets `/ready` report a
schema-behind pod as *not ready* (so it's pulled from the load balancer) instead
of silently erroring, and lets startup log a loud warning.

It compares the revision recorded in the DB's ``alembic_version`` table against
the revisions the shipped ``alembic/`` scripts declare. Alembic's revision chain
is linear, so a single head is expected.

Being out of step has **two** directions and they are not the same (#2500):

* **behind** — the DB revision is in this code's graph but is not its head. The
  code expects a column the DB does not have yet. Not ready; this is the case
  #544 was written for.
* **ahead** — the DB revision is one these scripts have never heard of, so the
  schema is newer than this code. That is the *normal* state of every rolling
  upgrade: the migration Job runs first, then pods are replaced one at a time,
  and the old ones keep serving against the new schema. Reporting those pods as
  not ready empties the load balancer and takes the API out of service for the
  length of the rollout -- which is what made a zero-downtime mechanism into an
  outage. Ahead is therefore **ready**, with a warning saying so.

Ahead is only safe while migrations are additive (expand/contract), so a
migration that genuinely contracts the schema records itself in
``schema_contractions``. A pod refuses readiness when that table holds a
revision **its own graph does not contain**: a contraction it predates is by
definition one it cannot know about, and one already in its graph is one it is
at or after, so no ordering between revisions is needed. That gives an operator
a documented gate rather than a silent one, and it needs no cooperation from the
pod being replaced.
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


@lru_cache(maxsize=1)
def code_all_revisions() -> frozenset[str]:
    """Every revision the shipped alembic scripts know about (cached).

    This is what distinguishes *behind* from *ahead*: a DB revision inside this
    set is one of our own that has not been reached, while a revision outside it
    is newer than anything we ship. Empty frozenset if the scripts cannot be
    located, which callers treat the same way as an unreadable head -- can't
    tell, don't block.
    """
    base = _find_alembic_base()
    if base is None:
        return frozenset()
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(base / "alembic.ini"))
        cfg.set_main_option("script_location", str(base / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        return frozenset(rev.revision for rev in script.walk_revisions())
    except Exception:
        logger.warning("Could not read the alembic revision graph", exc_info=True)
        return frozenset()


async def applied_contractions() -> frozenset[str]:
    """Revisions recorded in ``schema_contractions``, or empty.

    Empty covers two cases that need no distinguishing here: a schema migrated
    by a line predating the table, and a database we cannot reach at all. The
    readiness probe gates on the database check separately, so an unreachable
    database does not need this function to fail closed as well -- the same
    stance ``db_current_revision`` already takes.
    """
    try:
        async with get_db_session() as db:
            rows = await db.execute(text("SELECT revision FROM schema_contractions"))
            return frozenset(row[0] for row in rows)
    except Exception:
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
    """Return (ok, detail): may this pod serve traffic against this schema?

    ok=True when the applied revision is this code's head, when the schema is
    *ahead* of this code (the normal rolling-upgrade state -- see the module
    docstring), or when the scripts can't be read at all (we don't block a pod
    on our own inability to read them; the DB/Redis checks still gate
    readiness).

    ok=False when the schema is *behind*, when no revision is recorded at all,
    or when a contraction has been applied that this code predates.
    """
    heads = code_head_revisions()
    if not heads:
        return True, "unknown (alembic scripts not found; not gating readiness)"
    current = await db_current_revision()
    if current is None:
        return False, "no alembic_version row — migrations not applied"
    head_names = "/".join(sorted(heads))
    if current in heads:
        return True, current

    known = code_all_revisions()
    if current in known:
        return False, f"schema at {current}, BEHIND this code's head {head_names}"

    # Unknown to our scripts, so the schema is newer than this code. Ready --
    # but only because additive migrations leave old code working. A contraction
    # we predate does not, so that is the one thing checked here, and it is
    # checked only on this path: when the applied revision is our own head every
    # recorded contraction is necessarily already in `known`, and `/ready` is
    # probed often enough that a second query per probe is worth avoiding.
    unknown_contractions = await applied_contractions() - known
    if unknown_contractions:
        return False, (
            f"schema at {current}, ahead of this code's head {head_names}, and a "
            f"CONTRACTING migration ({'/'.join(sorted(unknown_contractions))}) has been "
            "applied that this code predates — it may no longer have the columns this "
            "code reads"
        )
    return True, (
        f"schema at {current}, ahead of this code's head {head_names} "
        "(expected during a rolling upgrade)"
    )


async def check_schema_at_startup() -> None:
    """Warm the code-head cache and log a loud warning if the schema is behind.

    Called once from the app lifespan. Does not raise — a schema-behind pod
    boots (so it can serve `/ready` = not-ready) rather than crash-looping."""
    import asyncio

    # Warm both caches off the event loop so the async `/ready` path never does
    # this sync filesystem I/O inside the loop.
    await asyncio.to_thread(code_head_revisions)
    await asyncio.to_thread(code_all_revisions)
    ok, detail = await schema_is_current()
    if ok and "ahead" in detail:
        logger.warning(
            "SCHEMA AHEAD: the database schema is newer than this app's migrations. "
            "This is expected while a rolling upgrade is in progress — this pod stays "
            "READY and keeps serving, because an additive migration leaves it working.",
            detail=detail,
        )
    elif ok:
        logger.info("Schema version check passed", detail=detail)
    else:
        logger.warning(
            "SCHEMA SKEW: this pod cannot safely serve against the database schema — "
            "it will report NOT READY. If the schema is behind, run the Alembic "
            "migration Job / helm upgrade hook. If a contracting migration has been "
            "applied, this pod's image predates it and must be replaced.",
            detail=detail,
        )
