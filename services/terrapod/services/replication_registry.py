"""Which entity classes replicate to the peer (#960 phase 3, #1110).

**Registration order is dependency order.** Backfill walks this registry in
order, so a class must appear after anything it holds a foreign key to — a join
token cannot be inserted before its pool exists.

Adding a class here is the whole opt-in: the flush hook starts emitting events
for it automatically, and the CI gate starts requiring its backfill path and its
full test matrix. That is deliberate — a class you can add without also adding
its tests is a class that will quietly fail to converge.
"""

from terrapod.db.models import (
    AgentPool,
    AgentPoolToken,
    APIToken,
    AutodiscoveryRule,
    CatalogItem,
    PlatformRoleAssignment,
    ProviderTemplate,
    RegistryModule,
    Role,
    RoleAssignment,
    User,
    VCSConnection,
)
from terrapod.services.replication import ReplicatedClass, register

# The pool itself: ordinary settings, no counters, no encrypted columns. It is
# the proving case for the generic path.
AGENT_POOLS = register(
    ReplicatedClass(
        name="agent_pools",
        model=AgentPool,
    )
)

# Join tokens.
#
# A shared listener fleet wants a long-lived, generously reusable token: every
# failover re-points the fleet at the promoted node, each listener re-joins, and
# each re-join spends one use. A `max_uses` sized for the initial rollout is
# therefore exhausted by the first real failover.
AGENT_POOL_TOKENS = register(ReplicatedClass(name="agent_pool_tokens", model=AgentPoolToken))


# ---------------------------------------------------------------------------
# Identity and access (#1119)
#
# Withholding these leaves a promoted node with no admins — it holds the estate
# and nobody can touch it. They come before everything that references them.
# ---------------------------------------------------------------------------

# Keyed by email, not a UUID.
USERS = register(ReplicatedClass(name="users", model=User, pk_attrs=("email",)))

ROLES = register(ReplicatedClass(name="roles", model=Role, pk_attrs=("name",)))

# Composite keys, and the reason #1119 existed. A node with users and roles but
# not the mapping between them has nobody with any permissions.
ROLE_ASSIGNMENTS = register(
    ReplicatedClass(
        name="role_assignments",
        model=RoleAssignment,
        pk_attrs=("provider_name", "email"),
    )
)

PLATFORM_ROLE_ASSIGNMENTS = register(
    ReplicatedClass(
        name="platform_role_assignments",
        model=PlatformRoleAssignment,
        pk_attrs=("provider_name", "email", "role_name"),
    )
)

# API tokens, and the class #1115 was really about.
#
# Revocation is a hard DELETE (the urgent-offboarding path), so it converges
# only because the delta path records deletes AND backfill reconciles. Without
# both, an offboarded person's token comes back to life at a failover, weeks
# after the revocation was performed and confirmed.
API_TOKENS = register(ReplicatedClass(name="api_tokens", model=APIToken))


# ---------------------------------------------------------------------------
# VCS connections (#1132)
#
# The first class holding real credentials, and the first to exercise the
# per-node encryption path end to end: an `EncryptedText` column is read through
# the ORM already decrypted, crosses the authenticated peer link as plaintext,
# and is re-encrypted under the RECEIVING node's own key on write. Neither node
# needs the other's key, which is what lets a pair span two clouds or two KMS
# tenancies.
#
# Withholding it would make a promotion look successful and then do nothing: the
# poller on the promoted node has no credentials, so every VCS-connected
# workspace silently stops seeing pushes and pull requests. It is also the
# prerequisite for replicating workspaces, autodiscovery rules and the registry,
# all of which hold a foreign key to it.
# ---------------------------------------------------------------------------

VCS_CONNECTIONS = register(ReplicatedClass(name="vcs_connections", model=VCSConnection))


# ---------------------------------------------------------------------------
# What a workspace points at (#1134)
#
# Every one of a workspace's `vcs_connection_id`, `autodiscovery_rule_id` and
# `catalog_item_id` is a real foreign key, and a *nullable* foreign key is still
# an enforced one — so none of it can be deferred by nulling the column on the
# follower. Nor should it be: `workspace_rbac_service` clamps permissions on a
# `catalog_item_id`-bearing workspace, so dropping that id would WIDEN access at
# exactly the moment of a failover.
# ---------------------------------------------------------------------------

# The module row, and deliberately not its versions.
#
# `registry_module_versions` rows point at tarballs in object storage, and object
# storage is #1114. A version row without its tarball is a promise this node
# cannot keep: `terraform init` would resolve the version, fetch it, and get a
# 404 mid-run. Withholding the versions fails more honestly — the module exists
# (so catalog items and module-workspace links have their foreign-key target) and
# the registry reports no matching version, which is at least true.
REGISTRY_MODULES = register(ReplicatedClass(name="registry_modules", model=RegistryModule))

# The parameterised provider configurations a catalog item renders into its
# generated root module. No foreign keys of its own, and referenced from a
# catalog item only by a JSONB id list — so nothing *enforces* this order. It is
# still the right one: a catalog item whose provider templates are missing gets
# through registration and then fails at provision, which is a worse way to find
# out than not having the item at all.
PROVIDER_TEMPLATES = register(ReplicatedClass(name="provider_templates", model=ProviderTemplate))

# A blessed designation over a module. `provider_template_ids` and
# `allowed_agent_pool_ids` are JSONB id lists rather than real foreign keys, so
# they impose no ordering the database will check — but the ids still have to
# resolve on the promoted node, which is what puts both of those classes above.
CATALOG_ITEMS = register(ReplicatedClass(name="catalog_items", model=CatalogItem))

# Monorepo discovery. Points at both a connection and (optionally) a pool, so it
# comes after both.
#
# Withholding it costs more than new directories going undiscovered: the
# lifecycle reconciler reads `on_directory_delete` off the rule to decide what a
# removed directory means. A promoted node without the rule has workspaces it
# cannot reconcile, and the safe-by-default answer it would fall back to is not
# the one the operator configured.
AUTODISCOVERY_RULES = register(ReplicatedClass(name="autodiscovery_rules", model=AutodiscoveryRule))
