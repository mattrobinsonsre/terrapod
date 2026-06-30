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
]
