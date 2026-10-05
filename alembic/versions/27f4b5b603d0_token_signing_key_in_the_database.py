"""Persist the token signing key in the database, like the CA.

Revision ID: 27f4b5b603d0
Revises: 8ee91ab917eb
Create Date: 2026-10-05

The key signs four stateless token families -- runner tokens, run-task callback
tokens, download tickets and Slack link tokens. Before this it came from the Helm
chart: a `randAlphaNum` in a rendered manifest, which is not a pure function of its
inputs, guarded by a `lookup` plus a namespace probe because under `helm template`
(what Argo CD and Flux run) `lookup` returns nothing and `.Release.IsInstall` is
always true -- so the generating branch was a key-rotation machine pointed at a
running deployment. Failing that, the key was derived from `sha256(database_url)`,
which hands token-forgery to anyone holding database credentials
(GHSA-hc47-q72v-4vcm).

The application now owns it, exactly as `init_ca()` owns the CA: generated on first
startup, persisted here, read on every startup, serialized across replicas by a
Postgres advisory lock. No manifest contains a secret and there is nothing for a
GitOps renderer to re-mint.

**This migration creates the table and nothing else -- it deliberately does not
write a key.** Adoption of the pre-2.0 `sha256(database_url)` value has to happen in
the API, because it must be byte-identical to what the API computed, and that is
`str(settings.database_url)` as the API's own settings render it. A migration
reconstructing that string from its own connection would differ on the driver
prefix alone (`postgresql+asyncpg://` vs `postgresql://`) and produce a *different*
key -- silently invalidating every token in flight, which is the single outcome the
adoption exists to prevent. See `auth/token_signing.py`.

Expand-only: one new table, nothing altered, nothing dropped.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "27f4b5b603d0"
down_revision: str | None = "8ee91ab917eb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "token_signing_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # The derived 32-byte key, hex-encoded. TEXT because EncryptedText
        # envelope-encrypts into the same column when encryption at rest is on.
        sa.Column("key", sa.Text(), nullable=False),
        # "generated" or "database-url" -- see the model docstring for why the
        # provenance is load-bearing rather than bookkeeping.
        sa.Column("provenance", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    # Dropping the table returns the deployment to deriving its key from the
    # database URL, so every token signed under the stored key stops verifying.
    # That is the honest consequence of going back, and it is why this is a major.
    op.drop_table("token_signing_keys")
