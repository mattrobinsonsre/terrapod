"""Which entity classes replicate to the peer (#960 phase 3, #1110).

**Registration order is dependency order.** Backfill walks this registry in
order, so a class must appear after anything it holds a foreign key to — a join
token cannot be inserted before its pool exists.

Adding a class here is the whole opt-in: the flush hook starts emitting events
for it automatically, and the CI gate starts requiring its backfill path and its
full test matrix. That is deliberate — a class you can add without also adding
its tests is a class that will quietly fail to converge.
"""

from terrapod.db.models import AgentPool, AgentPoolToken
from terrapod.services.replication import ReplicatedClass, register

# The pool itself: ordinary settings, no counters, no encrypted columns. It is
# the proving case for the generic path.
AGENT_POOLS = register(
    ReplicatedClass(
        name="agent_pools",
        model=AgentPool,
    )
)

# Join tokens, and the reason `monotonic_fields` exists.
#
# `use_count` is the budget `max_uses` is spent against, and BOTH nodes spend
# it: a shared listener fleet follows the DNS name, so joins land on A before a
# failover and on B after one. Under blanket last-write-wins a stale copy would
# regress the count and silently hand the token its spent budget back — the one
# field where losing an update issues extra credentials rather than merely
# losing information. `is_revoked` is the same hazard in boolean form: a
# revoked token must never be replicated back to usable.
#
# This is also why a shared fleet wants a long-lived, generously reusable token
# in the first place: every failover costs one use per listener.
AGENT_POOL_TOKENS = register(
    ReplicatedClass(
        name="agent_pool_tokens",
        model=AgentPoolToken,
        monotonic_fields=frozenset({"use_count"}),
        one_way_true_fields=frozenset({"is_revoked"}),
    )
)
