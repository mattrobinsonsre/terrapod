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
    PlatformRoleAssignment,
    Role,
    RoleAssignment,
    User,
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
