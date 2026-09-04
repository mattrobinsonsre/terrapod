"""Credential columns are encrypted at rest, and the sweep that found the gaps (#1140).

Two columns holding shared secrets sat side by side with opposite treatment:
`notification_configurations.token` was `EncryptedText`, `run_tasks.hmac_key` was
plain `Text`. A third — `gpg_keys.private_key`, a GPG **private** key — was also
plain, and is the most sensitive of the lot: it is what proves a published
provider came from this registry.

Rather than assert the three fixes one by one, this pins the **rule**: every
column whose name says it holds a credential is `EncryptedText`. A new one added
later has to either satisfy the rule or be listed as a considered exception, so
the next gap fails the build instead of waiting to be noticed.
"""

from sqlalchemy import inspect as sa_inspect

from terrapod.crypto.types import EncryptedText
from terrapod.db import models

#: Column names that name a credential. Substring match, deliberately broad —
#: a false positive costs one line in the exception list below; a false negative
#: is a secret stored in the clear.
CREDENTIAL_HINTS = (
    "hmac_key",
    "private_key",
    "client_secret",
    "webhook_secret",
    "password",
    "api_key",
    "signing_key",
    "bot_token",
)

#: Considered exceptions, each with the reason it is not `EncryptedText`.
#:
#: `crypto_keys.wrapped_dek` cannot be: it is the key the encryption layer
#: unwraps at boot, so encrypting it under itself is circular. It is already
#: KEK-wrapped ciphertext, which is the point.
#:
#: `task_stage_results.callback_token` is `String(255)`, so an encryption
#: envelope could overflow the column — adopting it needs a widen to TEXT first.
#: It is also short-lived and single-use. Tracked rather than silently skipped.
EXPECTED_PLAINTEXT = {
    ("crypto_keys", "wrapped_dek"),
    ("task_stage_results", "callback_token"),
    # A GPG key *id* is a public identifier, not a key — we hand it to clients
    # as `pubkey_fingerprint` so they know which registered key to verify a
    # collection's signature with (#1482). Encrypting it would hide a value
    # whose whole purpose is to be published, and the column matches here only
    # because its name contains "key".
    ("registry_collection_versions", "signing_key_id"),
}


def _credential_columns():
    """Columns whose name says they hold a recoverable credential.

    A `_hash` suffix is excluded as a class, not case by case. A one-way digest is
    not a secret to recover, so encrypting it protects nothing — and it would
    break the comparison that verifies it, since verification works by hashing
    the candidate and matching the stored value. `users.password_hash` and
    `oauth_clients.client_secret_hash` are right as they are.
    """
    for mapper in models.Base.registry.mappers:
        table = mapper.local_table
        if table is None:
            continue
        for column in table.columns:
            name = column.name
            if name.endswith("_hash"):
                continue
            if name.endswith("_token") or name == "token":
                yield table.name, column
            elif any(hint in name for hint in CREDENTIAL_HINTS):
                yield table.name, column


class TestCredentialColumnsAreEncrypted:
    def test_every_credential_column_is_encrypted_or_a_listed_exception(self):
        plaintext = {
            (table, column.name)
            for table, column in _credential_columns()
            if not isinstance(column.type, EncryptedText)
        }

        assert plaintext <= EXPECTED_PLAINTEXT, (
            "Credential columns stored in the clear:\n  "
            + "\n  ".join(sorted(f"{t}.{c}" for t, c in plaintext - EXPECTED_PLAINTEXT))
            + "\n\nUse EncryptedText. The DB column is already TEXT for most of these, so "
            "no migration is needed and reads pass legacy plaintext through unchanged. "
            "If it genuinely cannot be encrypted, add it to EXPECTED_PLAINTEXT with the "
            "reason."
        )

    def test_the_sweep_actually_finds_columns(self):
        """A rule that matches nothing enforces nothing."""
        found = {f"{t}.{c.name}" for t, c in _credential_columns()}

        assert len(found) >= 6, found
        assert "run_tasks.hmac_key" in found
        assert "gpg_keys.private_key" in found
        assert "notification_configurations.token" in found

    def test_hashes_are_excluded_rather_than_excused(self):
        """The `_hash` exclusion is a real rule, not a way past a failure: a
        one-way digest is not a recoverable secret, and encrypting it would break
        the comparison that verifies it."""
        found = {f"{t}.{c.name}" for t, c in _credential_columns()}

        assert "users.password_hash" not in found
        assert "oauth_clients.client_secret_hash" not in found

    def test_the_exception_list_has_not_grown_silently(self):
        """Every entry here is a documented decision. Growing it should be a
        conscious act, not the easy way past a failing assertion."""
        assert EXPECTED_PLAINTEXT == {
            ("crypto_keys", "wrapped_dek"),
            ("task_stage_results", "callback_token"),
            ("registry_collection_versions", "signing_key_id"),
        }

    def test_the_exceptions_still_exist(self):
        """If one is renamed or dropped, the entry becomes dead weight that
        silently excuses nothing — or worse, excuses something else later."""
        tables = {m.local_table.name: m.local_table for m in models.Base.registry.mappers}

        for table_name, column_name in EXPECTED_PLAINTEXT:
            assert table_name in tables, table_name
            assert column_name in tables[table_name].columns, f"{table_name}.{column_name}"


class TestTheTwoFixes:
    """Named directly, so the reason each changed is findable from the test name."""

    def test_the_run_task_hmac_key_is_encrypted(self):
        """Anyone holding it can forge a task result, and a mandatory task's
        result gates the apply."""
        column = sa_inspect(models.RunTask).local_table.c["hmac_key"]

        assert isinstance(column.type, EncryptedText)

    def test_the_gpg_private_key_is_encrypted(self):
        """The most sensitive column in the registry: it is what proves a
        published provider came from here."""
        column = sa_inspect(models.GPGKey).local_table.c["private_key"]

        assert isinstance(column.type, EncryptedText)

    def test_null_is_preserved_so_the_signing_key_query_still_works(self):
        """`gpg_key_service` selects signing-capable keys with
        `private_key IS NOT NULL`. That only keeps working because the type maps
        None to NULL rather than encrypting it."""
        assert EncryptedText().process_bind_param(None, None) is None
        assert EncryptedText().process_result_value(None, None) is None
