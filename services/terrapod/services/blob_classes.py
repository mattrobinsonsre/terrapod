"""The object store, divided into classes an operator can make decisions about.

Second slice of #1114. The first (#1147) proved whether the objects a node's rows
name are actually present. This is the register those checks — and the copying that
follows — both read from, plus the per-class configuration that decides which of
the two Terrapod does.

**Why per class and not one switch.** #1114's argument is that the choice belongs
to the operator and differs by class within a single deployment. Copying state and
configuration versions across a region boundary while letting a re-warmable
provider cache stay cold is a coherent position, and a bucket-level replication
policy cannot express it. So each class carries its own mode:

``off``
    Terrapod neither checks nor copies it. Reported as skipped rather than
    quietly omitted, so an operator reading a clean readiness report can tell
    "nothing missing" from "nobody looked".
``verify``
    Check presence; do not copy. The case where the operator has already arranged
    replication — provider-native cross-region, a storage-level mirror — and what
    Terrapod owes them is evidence it happened, not a second copy of it.
``copy``
    Copy to the peer, and verify. The cross-CSP, on-prem and air-gapped case,
    where nothing else is doing it.

The default is ``verify`` everywhere. That is deliberate: it observes the whole
store, costs nothing until the readiness endpoint is called, and commits the
operator to nothing. #1114's own "not in scope" is *deciding for the operator*.

**Two things a class can be, and the difference matters.** A class is
*verifiable* only when a database row **guarantees** the object exists. That is
what makes the check sound: the row names the key, so an absent object is a real
finding. Where no row makes that promise — a run's logs exist only if the run got
far enough to write them, a pull-through cache holds whatever it happens to hold —
presence cannot be derived from the database, and asserting otherwise would
manufacture exactly the false signal #1147 exists to remove. Those classes are
copy-only: the store's own listing is the truth about what is there, so the copier
enumerates by prefix and readiness declines to claim.

**Sealing changes the tiering, and it is derived, not remembered.** A cold
provider cache normally re-warms itself on first use. On a node in ``cache_only``
mode it cannot: reaching upstream is precisely what sealing forbids, so a promoted
node with a cold cache has no terraform binary and no providers and can never run
anything again. That moves those classes into the same tier as state. The operator
should not have to notice this and mirror it into a second setting, so
:func:`effective_tier` reads it off ``registry.cache_only``.

**One correction to #1114's own table.** It lists ``policies/{ps}/{v}.tar.gz`` as
irreplaceable. There is no such object: policy Rego is stored inline in Postgres
(``policies.rego``) and ``keys.policy_set_key`` has no callers. Policy sets are
covered by settings replication, not by this phase — recorded here rather than
carried forward as a class that would always verify empty.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import BLOB_CLASS_NAMES, BLOB_MODES
from terrapod.storage import keys

#: What losing a class costs, from #1114.
IRREPLACEABLE = "irreplaceable"
HISTORY = "history"
REDERIVABLE = "rederivable"

#: What Terrapod does about a class.
OFF = "off"
VERIFY = "verify"
COPY = "copy"

MODES = BLOB_MODES

#: Resolvers return ``(total_rows, keys_to_check)``. The split is load-bearing: a
#: class can hold ten thousand rows and be sampled to twenty-five keys, and a
#: report that carries only one of those numbers cannot say how much of the class
#: the answer covers.
Resolver = Callable[[AsyncSession, int | None], Awaitable[tuple[int, list[str]]]]


@dataclass(frozen=True)
class BlobClass:
    """One prefix class of the object store."""

    name: str
    #: Tier when nothing about the deployment changes it. See
    #: :func:`effective_tier` for the sealed case.
    tier: str
    #: Key prefixes the class covers. What a copier enumerates, and what an
    #: operator greps for in a bucket listing when a report says something is
    #: missing.
    prefixes: tuple[str, ...]
    #: Present only when a row guarantees the object. ``None`` means the class is
    #: copy-only — see the module docstring; it is a statement about soundness,
    #: not an omission to fill in later.
    resolver: Resolver | None = None
    #: True when sealing (`registry.cache_only`) escalates this class to
    #: irreplaceable, because a sealed node cannot re-warm it.
    sealed_is_fatal: bool = False
    #: Why the class is not verifiable, surfaced to the operator so the gap reads
    #: as a considered boundary rather than a missing feature.
    unverifiable_reason: str = ""


async def _count(db: AsyncSession, model: type) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar() or 0)


# ---------------------------------------------------------------------------
# Verifiable classes — a row names the key, so an absent object is a finding
# ---------------------------------------------------------------------------


async def _resolve_state(db: AsyncSession, limit: int | None) -> tuple[int, list[str]]:
    """Every state version, not only the latest.

    Rollback is a shipped feature, so a node holding only HEAD has silently lost
    rollback depth — and would look perfectly healthy doing it.
    """
    from terrapod.db.models import StateVersion

    total = await _count(db, StateVersion)
    stmt = select(StateVersion.workspace_id, StateVersion.id).order_by(
        StateVersion.created_at.desc()
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).all()
    return total, [keys.state_key(str(ws), str(sv)) for ws, sv in rows]


async def _resolve_state_index(db: AsyncSession, limit: int | None) -> tuple[int, list[str]]:
    """The break-glass recovery index.

    Worse than absent if it is stale on the promoted node: it points at objects
    that are not there while looking authoritative. Presence is all this checks —
    freshness would need the index parsed, which is a separate job.
    """
    from terrapod.db.models import StateVersion

    total = 1 if await _count(db, StateVersion) else 0
    return total, [keys.state_index_key()] if total else []


async def _resolve_config_versions(db: AsyncSession, limit: int | None) -> tuple[int, list[str]]:
    """The sharpest omission in the whole store.

    A VCS-connected workspace can refetch its configuration. A CLI-uploaded,
    catalog-provisioned or migrated one cannot — this tarball is the only copy.
    Losing it means those workspaces can never run again, while the UI still
    lists them as healthy.
    """
    from terrapod.db.models import ConfigurationVersion

    total = await _count(db, ConfigurationVersion)
    stmt = (
        select(ConfigurationVersion.workspace_id, ConfigurationVersion.id)
        .where(ConfigurationVersion.status == "uploaded")
        .order_by(ConfigurationVersion.created_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).all()
    return total, [keys.config_version_key(str(ws), str(cv)) for ws, cv in rows]


async def _resolve_modules(db: AsyncSession, limit: int | None) -> tuple[int, list[str]]:
    """Module tarballs. The rows say the module exists, so the registry *looks*
    fine and every `terraform init` fails."""
    from terrapod.db.models import RegistryModule, RegistryModuleVersion

    total = await _count(db, RegistryModuleVersion)
    stmt = (
        select(
            RegistryModule.namespace,
            RegistryModule.name,
            RegistryModule.provider,
            RegistryModuleVersion.version,
        )
        .join(RegistryModuleVersion, RegistryModuleVersion.module_id == RegistryModule.id)
        .order_by(RegistryModuleVersion.created_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).all()
    return total, [keys.module_tarball_key(ns, n, p, v) for ns, n, p, v in rows]


async def _resolve_provider_binaries(db: AsyncSession, limit: int | None) -> tuple[int, list[str]]:
    """Provider platform zips, plus the signed manifest that makes them
    installable.

    Provider versions are client-signed and immutable: they cannot be
    regenerated server-side, only re-published by whoever holds the signing key.
    That makes a missing one closer to state than to a cache.
    """
    from terrapod.db.models import (
        RegistryProvider,
        RegistryProviderPlatform,
        RegistryProviderVersion,
    )

    total = await _count(db, RegistryProviderPlatform)
    stmt = (
        select(
            RegistryProvider.namespace,
            RegistryProvider.name,
            RegistryProviderVersion.version,
            RegistryProviderPlatform.os,
            RegistryProviderPlatform.arch,
        )
        .join(
            RegistryProviderVersion,
            RegistryProviderVersion.provider_id == RegistryProvider.id,
        )
        .join(
            RegistryProviderPlatform,
            RegistryProviderPlatform.version_id == RegistryProviderVersion.id,
        )
        .order_by(RegistryProviderPlatform.created_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).all()

    out: list[str] = []
    for ns, name, version, os_, arch in rows:
        out.append(keys.provider_binary_key(ns, name, version, os_, arch))
        # The manifest and its signature are what make the binary installable;
        # a present zip with an absent SHA256SUMS still fails `terraform init`.
        out.append(keys.provider_shasums_key(ns, name, version))
        out.append(keys.provider_shasums_sig_key(ns, name, version))
    return total, out


async def _resolve_provider_cache(db: AsyncSession, limit: int | None) -> tuple[int, list[str]]:
    """Pull-through upstream provider binaries.

    Ordinarily the least interesting class in the store — it re-warms itself. On a
    sealed node it is the difference between a working promotion and a node that
    cannot run `terraform init` at all, which is why the row carries the full
    coordinates and this is checkable rather than guessed at.
    """
    from terrapod.db.models import CachedProviderPackage

    total = await _count(db, CachedProviderPackage)
    stmt = select(
        CachedProviderPackage.hostname,
        CachedProviderPackage.namespace,
        CachedProviderPackage.type,
        CachedProviderPackage.version,
        CachedProviderPackage.filename,
    ).order_by(CachedProviderPackage.cached_at.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).all()
    return total, [keys.provider_cache_key(h, ns, t, v, f) for h, ns, t, v, f in rows]


async def _resolve_binary_cache(db: AsyncSession, limit: int | None) -> tuple[int, list[str]]:
    """Cached terraform/tofu/terragrunt executables.

    Only the executable itself is checked. The publisher manifest and signature
    cached alongside it (#607) are written opportunistically, so a node that
    cached a binary before that landed legitimately has none — treating their
    absence as missing would report a false gap on a healthy node.
    """
    from terrapod.db.models import CachedBinary

    total = await _count(db, CachedBinary)
    stmt = select(
        CachedBinary.tool, CachedBinary.version, CachedBinary.os, CachedBinary.arch
    ).order_by(CachedBinary.cached_at.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).all()
    return total, [keys.binary_cache_key(t, v, o, a) for t, v, o, a in rows]


# ---------------------------------------------------------------------------
# The register
#
# Ordered as #1114 sets it out: irreplaceable first. That is the order a human
# reads a report in, and the order a copier has to work in — the classes whose
# loss is permanent go over the link before the ones that re-derive.
# ---------------------------------------------------------------------------

CLASSES: tuple[BlobClass, ...] = (
    BlobClass(
        name="state",
        tier=IRREPLACEABLE,
        prefixes=("state/",),
        resolver=_resolve_state,
    ),
    BlobClass(
        name="state_index",
        tier=IRREPLACEABLE,
        prefixes=("state/index.yaml",),
        resolver=_resolve_state_index,
    ),
    BlobClass(
        name="configuration_versions",
        tier=IRREPLACEABLE,
        prefixes=("config/",),
        resolver=_resolve_config_versions,
    ),
    BlobClass(
        name="registry_modules",
        tier=IRREPLACEABLE,
        prefixes=("registry/modules/",),
        resolver=_resolve_modules,
    ),
    BlobClass(
        name="registry_providers",
        tier=IRREPLACEABLE,
        prefixes=("registry/providers/",),
        resolver=_resolve_provider_binaries,
    ),
    BlobClass(
        name="run_logs",
        tier=HISTORY,
        prefixes=("logs/",),
        unverifiable_reason=(
            "A run writes logs only once it reaches the phase that produces them, so "
            "no row promises the object exists and an absent one is not a finding."
        ),
    ),
    BlobClass(
        name="run_plans",
        tier=HISTORY,
        prefixes=("plans/",),
        unverifiable_reason=(
            "Which of the plan artifacts a run has — tfplan, JSON output, lock file, "
            "artifacts tarball, cost estimate — depends on how far it got and what it "
            "produced, so presence cannot be derived from the run row."
        ),
    ),
    BlobClass(
        name="run_vars",
        tier=HISTORY,
        prefixes=("runs/",),
        unverifiable_reason="Written only for runs that had variables to render.",
    ),
    BlobClass(
        name="provider_cache",
        tier=REDERIVABLE,
        prefixes=("cache/providers/",),
        resolver=_resolve_provider_cache,
        sealed_is_fatal=True,
    ),
    BlobClass(
        name="binary_cache",
        tier=REDERIVABLE,
        prefixes=("cache/binaries/",),
        resolver=_resolve_binary_cache,
        sealed_is_fatal=True,
    ),
    BlobClass(
        name="platform_provider_cache",
        tier=REDERIVABLE,
        prefixes=("cache/provider/terrapod/",),
        sealed_is_fatal=True,
        unverifiable_reason=(
            "The Terrapod provider is cached on demand from GitHub Releases with no "
            "row recording it, so the store's own listing is the only account of what "
            "is there."
        ),
    ),
    BlobClass(
        name="cost_pricesheet",
        tier=REDERIVABLE,
        prefixes=("cache/cost/",),
        sealed_is_fatal=True,
        unverifiable_reason=(
            "A single cache object with no row behind it; a node that has never "
            "estimated a cost legitimately has none."
        ),
    ),
    BlobClass(
        # Not escalated by sealing: these re-derive from the VCS provider, which a
        # sealed node still reaches — `cache_only` seals upstream *registries*, not
        # the operator's own git.
        name="vcs_archives",
        tier=REDERIVABLE,
        prefixes=("vcs_archives/",),
        unverifiable_reason=(
            "A content-addressed cache with no table behind it; entries come and go "
            "with the commits that produced them."
        ),
    ),
    BlobClass(
        name="module_overrides",
        tier=REDERIVABLE,
        prefixes=("module_overrides/",),
        unverifiable_reason=(
            "Built per pull-request commit for impact analysis and referenced from a "
            "run's JSON column rather than a row of its own."
        ),
    ),
)

CLASS_NAMES: tuple[str, ...] = tuple(c.name for c in CLASSES)

# The operator-facing vocabulary is declared in `config.py`, because `config.py`
# has to stay a leaf: it is imported before anything else at startup and,
# separately, by the config-channel contract check in a pydantic-only
# environment. A validator reaching in here would pull in SQLAlchemy and the
# storage package — which imports `settings` back, so the import would land
# mid-initialisation and the API would refuse to boot.
#
# That leaves the names written down twice, so they are bound together here and
# the check fails at import rather than at the first mismatched lookup. Ordering
# is part of it: the register's order is what a readiness report and a copier both
# follow, and the config's list documents it.
if CLASS_NAMES != BLOB_CLASS_NAMES:
    raise RuntimeError(
        "blob class register and config.BLOB_CLASS_NAMES disagree.\n"
        f"  register: {CLASS_NAMES}\n"
        f"  config:   {BLOB_CLASS_NAMES}\n"
        "Both must list the same classes in the same order — the config's copy is "
        "what validates an operator's `ha.blobs.classes`, so a class missing there "
        "is one they cannot configure."
    )

_BY_NAME = {c.name: c for c in CLASSES}


def get(name: str) -> BlobClass:
    """The class by name. Raises `KeyError` for an unknown one."""
    return _BY_NAME[name]


def _blobs_config():
    from terrapod.config import settings

    return settings.ha.blobs


def _sealed() -> bool:
    from terrapod.config import settings

    return bool(settings.registry.cache_only)


def effective_mode(cls: BlobClass, *, blobs=None) -> str:
    """What Terrapod does about this class: ``off``, ``verify`` or ``copy``.

    A per-class entry wins over the global default. Both are the operator's call —
    nothing here escalates a mode on their behalf, because the mode is a decision
    about their topology and #1114 is explicit that Terrapod does not make it.

    Takes the class rather than its name so it is total: there is no way to ask
    about a class that does not exist, which is where a name-keyed lookup would
    have needed a fallback and a fallback is where a typo goes quiet.
    """
    cfg = blobs if blobs is not None else _blobs_config()
    return cfg.classes.get(cls.name, cfg.mode)


def effective_tier(cls: BlobClass, *, sealed: bool | None = None) -> str:
    """The tier this class actually sits in on **this** deployment.

    Derived rather than configured: on a sealed node a cold cache is not
    inconvenient, it is terminal, and expecting the operator to remember that and
    restate it in a second setting is how a config grows a way to be wrong.
    """
    is_sealed = _sealed() if sealed is None else sealed
    if cls.sealed_is_fatal and is_sealed:
        return IRREPLACEABLE
    return cls.tier
