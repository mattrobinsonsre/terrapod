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
    ConfigurationVersion,
    ExecutionHook,
    ExecutionHookWorkspace,
    GPGKey,
    ModuleWorkspaceLink,
    NotificationConfiguration,
    PlatformRoleAssignment,
    Policy,
    PolicySet,
    ProviderTemplate,
    RegistryModule,
    RegistryModuleVersion,
    RegistryProvider,
    RegistryProviderPlatform,
    RegistryProviderVersion,
    Role,
    RoleAssignment,
    RunTask,
    RunTrigger,
    StateVersion,
    User,
    Variable,
    VariableSet,
    VariableSetVariable,
    VariableSetWorkspace,
    VCSConnection,
    Workspace,
    WorkspaceAgentPool,
    WorkspaceRemoteStateConsumer,
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
        # All THREE PK columns. A user may hold several roles under one
        # (provider, email), so a two-column key collapses them: every row
        # encodes to the same entity_id, read_entity returns an arbitrary one,
        # apply_upsert mutates role_name on the existing row instead of
        # inserting a second, and the backfill cursor pages straight past the
        # siblings. The follower ends up granting one arbitrary role.
        pk_attrs=("provider_name", "email", "role_name"),
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

# The module row and, since #1175, its versions.
#
# The versions were withheld while the object store was unreplicated, and the
# reasoning was sound at the time: a version row without its tarball is a promise
# this node cannot keep — `terraform init` would resolve the version, fetch it,
# and get a 404 mid-run. Withholding failed more honestly, because "no matching
# version" is at least true.
#
# #1114 copies the tarballs, which reverses it. The tarball now arrives and the
# row does not, so the registry reports no version for a module whose bytes are
# sitting in the store — the same lie in the other direction, and this one wastes
# a working artifact.
REGISTRY_MODULES = register(ReplicatedClass(name="registry_modules", model=RegistryModule))

REGISTRY_MODULE_VERSIONS = register(
    ReplicatedClass(name="registry_module_versions", model=RegistryModuleVersion)
)


# ---------------------------------------------------------------------------
# Provider registry (#1175)
#
# A published provider version is client-signed and immutable. Terrapod never
# re-signs — the publisher owns the signature — so a promoted node cannot
# reconstruct any of this. It either has the rows or the provider is gone.
# ---------------------------------------------------------------------------

# The public keys the download response advertises. Without them a follower can
# serve provider binaries that no client will install: `terraform init` verifies
# the signature against the key the registry names, and a registry that names no
# key fails closed.
#
# `private_key` is excluded. It is the one genuinely dangerous column here, and
# nothing on the receiving node needs it: `sign_data` exists in
# `gpg_key_service` but has no caller, because the server does not sign. Sending
# a signing key over the peer link would widen what a compromised peer costs, in
# exchange for a capability neither node uses.
GPG_KEYS = register(
    ReplicatedClass(
        name="gpg_keys",
        model=GPGKey,
        exclude=frozenset({"private_key"}),
    )
)

# The provider, then its versions, then the per-platform rows — foreign-key
# order, and also the order in which they stop being useful: a platform row
# without its version is unreachable, a version without its provider is orphaned.
REGISTRY_PROVIDERS = register(ReplicatedClass(name="registry_providers", model=RegistryProvider))

# `shasums_uploaded` / `shasums_sig_uploaded` are the publish protocol's state:
# binaries are refused until the signature has been verified against a registered
# key. Carrying them keeps that gate's answer, so a promoted node does not offer
# a half-published version as complete.
REGISTRY_PROVIDER_VERSIONS = register(
    ReplicatedClass(name="registry_provider_versions", model=RegistryProviderVersion)
)

# `shasum` and `h1_hash` are what the client checks the downloaded zip against.
# A platform row that arrives without them is worse than absent: the download
# proceeds and the verification has nothing to compare.
REGISTRY_PROVIDER_PLATFORMS = register(
    ReplicatedClass(name="registry_provider_platforms", model=RegistryProviderPlatform)
)

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


# ---------------------------------------------------------------------------
# Workspaces (#1136)
#
# Nothing is excluded, and that is the considered answer rather than the lazy
# one. Several columns look like node-local operational state and are not:
#
#   vcs_last_commit_sha  The poller cursor, and the most dangerous omission in
#                        the class. A promoted node that has not seen it treats
#                        every tracked branch as changed and queues a plan AND
#                        APPLY on every VCS-connected workspace at once — a
#                        fleet-wide event triggered by the failover itself.
#   locked / lock_id     The CLI state lock. Dropping a held lock at promotion
#                        lets two writers collide on state; carrying a stale one
#                        costs a manual unlock. Fail closed.
#   state_diverged       An apply succeeded but its state upload did not. A node
#                        that loses this believes state is good when it is not.
#   lifecycle_state      `pending_deletion` is a workspace awaiting a human
#                        decision; resetting it to `active` discards that.
#   drift_status         Continuity, and it stops the promoted node re-checking
#                        the entire fleet at once.
#
# `drift_latest_run_id` is carried and WILL dangle — runs are a later phase, and
# that column is deliberately not a foreign key so artifact retention cannot
# cascade into workspace deletion. The UI handles a 404 on click-through. Better
# a dead link than a special case here.
WORKSPACES = register(ReplicatedClass(name="workspaces", model=Workspace))

# The agent-pool set (#1087). Composite key, and the first replicated class
# edited as a *collection*.
#
# Adding or removing a pool mutates `workspace.agent_pool_links`, and the outbox
# hook deliberately asks `is_modified(..., include_collections=False)` — so the
# workspace row is NOT what records a membership change. These rows are, because
# they are a registered class in their own right and land in the session's
# new/deleted sets. If that were wrong, pool membership edits would silently
# never replicate, and a promoted node would dispatch runs to the wrong pools.
WORKSPACE_AGENT_POOLS = register(
    ReplicatedClass(
        name="workspace_agent_pools",
        model=WorkspaceAgentPool,
        pk_attrs=("workspace_id", "agent_pool_id"),
    )
)

# Module-impact analysis. Without it a promoted node stops queueing speculative
# plans when a PR is opened against a module repo, so the blast radius of a
# module change becomes invisible exactly when the estate is least stable.
MODULE_WORKSPACE_LINKS = register(
    ReplicatedClass(name="module_workspace_links", model=ModuleWorkspaceLink)
)


# ---------------------------------------------------------------------------
# Variables and variable sets (#1138)
#
# The classes that decide whether a failover works at all. A promoted node with
# workspaces but no variables is terraform with no inputs and no credentials:
# every run fails at plan, on every workspace, immediately. Not degraded — dead.
#
# Precedence is the subtle part, and it fails silently rather than loudly:
# priority set -> workspace variable -> non-priority set. A node that carries
# every value but loses `priority` hands a run a DIFFERENT value than the leader
# would, with nothing anywhere reporting a problem.
#
# `variables.value` and `variable_set_variables.value` are `EncryptedText`, so
# they take the same per-node path #1132 established: decrypted on read, carried
# over the authenticated peer link, re-encrypted under the receiving node's own
# key. Note what is deliberately NOT sent alongside them — see
# `crypto_keys` below.
# ---------------------------------------------------------------------------

VARIABLES = register(ReplicatedClass(name="variables", model=Variable))

VARIABLE_SETS = register(ReplicatedClass(name="variable_sets", model=VariableSet))

VARIABLE_SET_VARIABLES = register(
    ReplicatedClass(name="variable_set_variables", model=VariableSetVariable)
)

# Which workspaces a set applies to. Composite key, and like the agent-pool set
# it is edited as a collection on its parent — the junction rows are what record
# an assignment change, not the set row.
VARIABLE_SET_WORKSPACES = register(
    ReplicatedClass(
        name="variable_set_workspaces",
        model=VariableSetWorkspace,
        pk_attrs=("variable_set_id", "workspace_id"),
    )
)


# ---------------------------------------------------------------------------
# How a workspace's runs behave (#1141)
#
# Three of these are GATES, and for a gate the failure mode of partial
# replication is not an error — it is a silently weaker posture. A promoted node
# that carries a mandatory policy set but loses its `enforcement_level` has
# turned a hard stop into an advisory note, and nothing anywhere says so.
# ---------------------------------------------------------------------------

# Policy sets and their policies — the governance gap, and the worst of the
# three. A promoted node with none of these applies infrastructure with no
# policy evaluation at all: every guardrail absent, no error raised.
POLICY_SETS = register(ReplicatedClass(name="policy_sets", model=PolicySet))

POLICIES = register(ReplicatedClass(name="policies", model=Policy))

# Nobody is told anything without these — approvals, failures and drift all go
# unannounced at precisely the moment people most need to know. `token` is
# `EncryptedText`, so it takes the per-node path from #1132.
NOTIFICATION_CONFIGURATIONS = register(
    ReplicatedClass(name="notification_configurations", model=NotificationConfiguration)
)

# Pre/post-plan external validation. A mandatory task that vanishes is the second
# gate silently removed.
RUN_TASKS = register(ReplicatedClass(name="run_tasks", model=RunTask))

# Hooks do not merely add a step: they are how operators inject the setup a run
# depends on, so losing them changes what the run IS. `priority` decides
# execution order, which is behaviour rather than presentation.
EXECUTION_HOOKS = register(ReplicatedClass(name="execution_hooks", model=ExecutionHook))

EXECUTION_HOOK_WORKSPACES = register(
    ReplicatedClass(
        name="execution_hook_workspaces",
        model=ExecutionHookWorkspace,
        pk_attrs=("hook_id", "workspace_id"),
    )
)

# Cross-workspace dependency chains. Without them downstream workspaces quietly
# stop tracking upstream applies — no failure, just drift that nobody queued.
RUN_TRIGGERS = register(ReplicatedClass(name="run_triggers", model=RunTrigger))

# The allow-list for cross-workspace state reads: the third gate. It is access
# control, so its DELETIONS matter as much as its rows — a consumer that comes
# back to life can read state somebody deliberately cut it off from.
WORKSPACE_REMOTE_STATE_CONSUMERS = register(
    ReplicatedClass(name="workspace_remote_state_consumers", model=WorkspaceRemoteStateConsumer)
)


# ---------------------------------------------------------------------------
# The artifact plane (#1175)
#
# Everything above describes how the estate is CONFIGURED. These two describe
# what it has actually DONE, and they are the rows that name objects in the
# store.
#
# They were absent while the object store was unreplicated, which was coherent:
# a row naming a blob this node does not hold is a promise it cannot keep. #1114
# copies the blobs, which inverts the argument — the object now arrives and
# nothing points at it. A live pair showed exactly that: the state file present
# on the follower, the workspace present, and zero state versions.
#
# That is the worse of the two failures. An absent state version reads as "this
# workspace has never run", so a promoted node does not error — it plans the
# entire estate as a first-time create. The operator sees a plan, not a fault.
# ---------------------------------------------------------------------------

# Every state version, not only the current one: rollback is a shipped feature,
# so a node holding only HEAD has silently lost rollback depth while looking
# perfectly healthy. Matches what `blob_classes._resolve_state` already copies.
#
# `run_id` is excluded because `runs` is deliberately not replicated (below).
# The column is a nullable FK with ON DELETE SET NULL, so carrying it would make
# the insert fail on the follower against a run that is not there — trading a
# missing provenance link for a missing state version.
STATE_VERSIONS = register(
    ReplicatedClass(
        name="state_versions",
        model=StateVersion,
        exclude=frozenset({"run_id"}),
    )
)

# The tarball's row. #1114 calls this the sharpest omission and it is right: a
# VCS-connected workspace can refetch its configuration, but a CLI-uploaded,
# catalog-provisioned or migrated one cannot — this tarball is the only copy.
# Losing the row means those workspaces can never run again, while the UI still
# lists them as healthy.
CONFIGURATION_VERSIONS = register(
    ReplicatedClass(name="configuration_versions", model=ConfigurationVersion)
)


# ---------------------------------------------------------------------------
# What is deliberately NOT replicated, and why
#
# `runs` — a run row is not history, it is a live execution: `job_name`, the
# claiming pool, and the state the reconciler drives against a Kubernetes Job it
# can see. Replicating it hands the follower runs it believes itself to be
# executing, and a reconciler that will act on them. The run LOG and plan
# artifacts are history worth keeping and are copied as blobs; the row that
# drives execution is not, and a promoted node starting with an empty run queue
# is the correct outcome rather than a gap.
#
# `registry_*_versions` and the provider platform rows are a separate question
# with the same shape as the two above, and are tracked in #1175 rather than
# folded in here: a provider version is client-signed and immutable, so its row
# has to carry signature metadata that wants its own thought, and it needs
# `registry_providers` and `gpg_keys` registered ahead of it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# What must NEVER be replicated
#
# `crypto_keys` holds this node's data-encryption key wrapped by THIS node's
# KEK. Sending it is useless to the peer, which cannot unwrap it, and it puts key
# material on a link that has no need of it. The per-node encryption design —
# decrypt on send, re-encrypt under the receiver's own key — exists precisely so
# that the key never has to travel.
#
# There is nothing to write here; the point is that nothing is written here.
# `TestTheKeysThemselvesNeverTravel` in tests/services/test_replication_variables.py
# fails if that class is ever registered.
# ---------------------------------------------------------------------------
