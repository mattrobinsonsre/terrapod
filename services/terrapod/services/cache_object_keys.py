"""Where a cached artifact's object actually is (#2599).

`storage.keys` is a module of **pure string builders**: every function there
takes strings and returns a key, which is what lets
`tests/storage/test_keys_workspace_scope.py` introspect it and classify each
builder as workspace-scoped or shared by calling it. These two take a ROW, so
they do not belong there -- that gate said so by calling them with strings and
failing on `'str' object has no attribute 'tool'`, which is the module's
contract enforcing itself.

The distinction they exist for: a row records its key when it has one, and
derives it from its coordinates when it does not. `cached_package_files` has
always recorded one; `cached_binaries` and `cached_provider_packages` gained the
column in #2599, so a row written before that has NULL and its object sits at
the derived key.

ONE place for that fallback, deliberately. It is needed at nine read sites
across five modules, and nine copies of `row.storage_key or derive(...)` is nine
chances to write `derive(...)` alone -- which serves, checks or reaps the wrong
object for every uploaded artifact, silently, because the derived key is a
perfectly valid key that simply holds nothing.
"""

from __future__ import annotations

from terrapod.storage.keys import binary_cache_key, provider_cache_key


def binary_storage_key(row) -> str:
    """Where a cached-binary row's object is.

    Duck-typed on the row rather than importing the model, so this stays
    importable from anywhere without a circular import through `db.models`.
    An empty string counts as unset: a blank key is not a location.
    """
    stored = getattr(row, "storage_key", None)
    if stored:
        return stored
    return binary_cache_key(row.tool, row.version, row.os, row.arch)


def provider_storage_key(row) -> str:
    """Where a cached-provider row's object is -- see `binary_storage_key`."""
    stored = getattr(row, "storage_key", None)
    if stored:
        return stored
    return provider_cache_key(row.hostname, row.namespace, row.type, row.version, row.filename)
