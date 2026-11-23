"""the ssh host-key policy replaces the host-key-checking boolean (#2592)

OpenSSH has three behaviours and ansible can express two, so a boolean named after
ansible's flag could not say `accept-new` -- trust on first contact, refuse on
change -- which is the state a growing fleet wants once approved host keys exist.
Without it, declaring one approved key makes every unpinned ADDRESS in that
inventory unreachable, because `ssh_args` is process-wide.

A clean replace rather than expand/contract. `ansible_host_key_checking` is a
2.0-development column: it appears on no release line (measured -- zero references
in `db/models.py` on both `release/v1.11` and `release/v1.10`), so no deployment
carries it and there is no rolling-upgrade window to protect.

The values are converted rather than defaulted, which matters in the one direction
that is unsafe: a deployment that had deliberately relaxed verification would
otherwise silently become `strict` and every unpinned host would go unreachable at
once. `true -> strict`, `false -> off`.

Revision ID: b56cc921fd47
Revises: e21cfeb0a416
"""

import sqlalchemy as sa
from alembic import op

revision = "b56cc921fd47"
down_revision = "e21cfeb0a416"
branch_labels = None
depends_on = None

#: The two tables that CARRIED the boolean, so their values are converted.
_TABLES = ("workspaces", "autodiscovery_rules")

#: The run snapshots the policy at creation, as it snapshots `configure_parallelism`
#: -- so a run behaves as it was created. It never held the boolean, so there is
#: nothing to convert: every existing run predates the policy and `strict` is both
#: the default and what those runs actually enforced.
_SNAPSHOT_TABLES = ("runs",)


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "ssh_host_key_policy",
                sa.String(length=16),
                nullable=False,
                server_default="strict",
            ),
        )
        # Faithful in both directions, and the `false` branch is the one that
        # matters: defaulting it would turn a deliberate relaxation into strict
        # verification and make every unpinned host unreachable in one upgrade.
        op.execute(
            sa.text(
                f"UPDATE {table} SET ssh_host_key_policy = "
                f"CASE WHEN ansible_host_key_checking THEN 'strict' ELSE 'off' END"
            )
        )
        op.drop_column(table, "ansible_host_key_checking")

    for table in _SNAPSHOT_TABLES:
        op.add_column(
            table,
            sa.Column(
                "ssh_host_key_policy",
                sa.String(length=16),
                nullable=False,
                server_default="strict",
            ),
        )

    # A pod whose own revision graph does not contain this one must refuse
    # readiness: it reads a column this drops. A BARE string rather than
    # `sa.text(...)`, matching `a7197ada2c0e` -- the gate walks the AST for a
    # string constant passed to `op.execute`, so wrapping it hides the marker
    # from the one check that enforces it.
    op.execute(
        "INSERT INTO schema_contractions (revision, note) "
        "VALUES ('b56cc921fd47', "
        "'Drops workspaces.ansible_host_key_checking and "
        "autodiscovery_rules.ansible_host_key_checking (#2592), replaced by "
        "ssh_host_key_policy. An older pod reads the dropped boolean and must "
        "not serve.') "
        "ON CONFLICT (revision) DO NOTHING"
    )


def downgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "ansible_host_key_checking",
                sa.Boolean(),
                nullable=False,
                server_default="true",
            ),
        )
        # `accept-new` has no boolean -- it is the state the boolean could not
        # express, which is why this column exists. It reverses to TRUE, the safe
        # direction: a host that would have been trusted on first contact is
        # refused instead, rather than a revoked key quietly being accepted.
        op.execute(
            sa.text(
                f"UPDATE {table} SET ansible_host_key_checking = "
                f"CASE WHEN ssh_host_key_policy = 'off' THEN false ELSE true END"
            )
        )
        op.drop_column(table, "ssh_host_key_policy")

    for table in _SNAPSHOT_TABLES:
        op.drop_column(table, "ssh_host_key_policy")

    op.execute("DELETE FROM schema_contractions WHERE revision = 'b56cc921fd47'")
