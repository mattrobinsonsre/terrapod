"""Registry of DB columns covered by app-layer encryption (#553).

Single source of truth for the resumable migration (terrapod.cli.encryption_migrate).
Each entry is a ``(table, column)`` whose model column is ``EncryptedText``. Keep
this in sync with the model definitions; the source-introspection test asserts the
model columns are EncryptedText, and the migration drives off this list.
"""

# (table_name, column_name) — all TEXT columns; id is the uuid primary key.
ENCRYPTED_COLUMNS: list[tuple[str, str]] = [
    ("certificate_authority", "ca_key_pem"),
    ("variables", "value"),
    ("variable_set_variables", "value"),
    ("vcs_connections", "token"),
    ("vcs_connections", "webhook_secret"),
    ("notification_configurations", "token"),
    # Both converted to EncryptedText by #1140 over an existing TEXT column, so
    # neither needed a migration — and that is exactly why this list was not
    # revisited alongside them. They were absent for two releases.
    #
    # Absence here is silent and it is not only about new rows. This list is the
    # sole driver of the loop in `cli.encryption_migrate`, so an unlisted column
    # is visited by neither `encrypt` nor `decrypt`: pre-existing rows stay
    # plaintext, `decrypt`-before-disable leaves them unreadable once the key is
    # gone, and — the one that matters most — a DEK rotation never re-keys them.
    # An operator rotating BECAUSE they believe a key is compromised would not
    # have rotated the provider-signing key or the run-task HMAC keys, and the
    # compromised DEK still decrypts both.
    ("gpg_keys", "private_key"),
    ("run_tasks", "hmac_key"),
]
