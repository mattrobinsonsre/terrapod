"""The registry's rows, which name signed and immutable artifacts (#1175).

The blobs already copy (#1114). These are the rows that make them reachable, and
they are unusually unforgiving: a published provider version is **client-signed
and immutable**, so Terrapod cannot reconstruct any of it. Terrapod never
re-signs — the publisher owns the signature — so a promoted node either has
these rows or the provider is simply gone until somebody re-publishes it with a
key they may no longer hold.

The failure without them is quiet in the way that matters: the registry lists
the module or provider (its parent row replicates), reports no versions, and
every `terraform init` against it 404s.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.db.models import (
    GPGKey,
    RegistryModuleVersion,
    RegistryProvider,
    RegistryProviderPlatform,
    RegistryProviderVersion,
)
from terrapod.services import replication, replication_registry

GPG_KEYS = replication_registry.GPG_KEYS
PROVIDERS = replication_registry.REGISTRY_PROVIDERS
PROVIDER_VERSIONS = replication_registry.REGISTRY_PROVIDER_VERSIONS
PROVIDER_PLATFORMS = replication_registry.REGISTRY_PROVIDER_PLATFORMS
MODULE_VERSIONS = replication_registry.REGISTRY_MODULE_VERSIONS


def _rows_db(rows):
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db.execute.return_value = result
    return db


def _keys_db(keys):
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = keys
    db.execute.return_value = result
    return db


async def _converges_deletion(spec):
    """Shared shape: a backfill must remove what the peer no longer has."""
    keep, gone = str(uuid.uuid4()), str(uuid.uuid4())
    db = _keys_db([(keep,), (gone,)])

    removed = await replication.reconcile_deletions(db, spec, {keep})

    assert removed == [gone]


async def _delete_applies(spec):
    db = AsyncMock()

    await replication.apply_delete(db, spec, str(uuid.uuid4()))

    assert db.execute.await_count == 1


class TestGPGKeys:
    """Without these a follower serves binaries nobody will install: the client
    verifies against the key the registry names, and a registry naming no key
    fails closed."""

    @staticmethod
    def _key(**kw):
        base = {
            "id": uuid.uuid4(),
            "key_id": "1C11AB2FF1189D6C",
            "ascii_armor": "-----BEGIN PGP PUBLIC KEY BLOCK-----\npublic\n-----END-----",
            "source": "terrapod",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        base.update(kw)
        return GPGKey(**base)

    @pytest.mark.replication_matrix("gpg_keys", "encrypted-columns")
    def test_the_private_key_never_travels(self):
        """This class discharges its encrypted-column obligation by EXCLUSION
        rather than by a decrypt/re-encrypt round trip, and that is the stronger
        answer here: nothing on the receiving node signs. `sign_data` exists in
        `gpg_key_service` and has no caller, because Terrapod does not re-sign.
        Sending a signing key would widen what a compromised peer costs in
        exchange for a capability neither node uses."""
        key = self._key(private_key="-----BEGIN PGP PRIVATE KEY BLOCK-----\nsecret\n-----END-----")

        payload = replication.serialize_row(GPG_KEYS, key)

        assert "private_key" not in payload
        assert "secret" not in repr(payload)
        assert payload["ascii_armor"].startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----"), (
            "the PUBLIC half must travel — it is what the download response advertises"
        )

    @pytest.mark.replication_matrix("gpg_keys", "backfill-from-empty")
    async def test_backfill_carries_the_public_keys(self):
        db = _rows_db([self._key(key_id="AAAA"), self._key(key_id="BBBB")])

        page = await replication.read_backfill(db, GPG_KEYS)

        assert {row["key_id"] for row in page} == {"AAAA", "BBBB"}
        assert all("private_key" not in row for row in page)

    @pytest.mark.replication_matrix("gpg_keys", "delta-apply")
    async def test_a_new_key_applies(self):
        db = AsyncMock()
        db.add = MagicMock()
        db.scalar.return_value = None
        key = self._key()

        await replication.apply_upsert(db, GPG_KEYS, replication.serialize_row(GPG_KEYS, key))

        assert db.add.call_args[0][0].key_id == "1C11AB2FF1189D6C"

    @pytest.mark.replication_matrix("gpg_keys", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = self._key()
        db.scalar.return_value = existing
        payload = replication.serialize_row(GPG_KEYS, existing)

        await replication.apply_upsert(db, GPG_KEYS, payload)
        await replication.apply_upsert(db, GPG_KEYS, payload)

        assert existing.key_id == "1C11AB2FF1189D6C"

    @pytest.mark.replication_matrix("gpg_keys", "delete")
    async def test_deleting_a_key_applies(self):
        """A revoked signing key that lingers on the follower keeps vouching for
        artifacts the operator stopped trusting."""
        await _delete_applies(GPG_KEYS)

    @pytest.mark.replication_matrix("gpg_keys", "backfill-converges-deletion")
    async def test_backfill_removes_keys_the_peer_no_longer_has(self):
        await _converges_deletion(GPG_KEYS)


class TestRegistryProviders:
    @staticmethod
    def _provider(**kw):
        base = {
            "id": uuid.uuid4(),
            "namespace": "default",
            "name": "aws",
            "labels": {},
            "owner_email": "a@example.com",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        base.update(kw)
        return RegistryProvider(**base)

    @pytest.mark.replication_matrix("registry_providers", "backfill-from-empty")
    async def test_backfill_carries_the_providers(self):
        db = _rows_db([self._provider(name="aws"), self._provider(name="azurerm")])

        page = await replication.read_backfill(db, PROVIDERS)

        assert {row["name"] for row in page} == {"aws", "azurerm"}

    @pytest.mark.replication_matrix("registry_providers", "delta-apply")
    async def test_a_new_provider_applies(self):
        db = AsyncMock()
        db.add = MagicMock()
        db.scalar.return_value = None
        p = self._provider()

        await replication.apply_upsert(db, PROVIDERS, replication.serialize_row(PROVIDERS, p))

        added = db.add.call_args[0][0]
        assert added.id == p.id
        assert added.namespace == "default"

    @pytest.mark.replication_matrix("registry_providers", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = self._provider(labels={"team": "platform"})
        db.scalar.return_value = existing
        payload = replication.serialize_row(PROVIDERS, existing)

        await replication.apply_upsert(db, PROVIDERS, payload)
        await replication.apply_upsert(db, PROVIDERS, payload)

        assert existing.labels == {"team": "platform"}

    @pytest.mark.replication_matrix("registry_providers", "delete")
    async def test_deleting_a_provider_applies(self):
        await _delete_applies(PROVIDERS)

    @pytest.mark.replication_matrix("registry_providers", "backfill-converges-deletion")
    async def test_backfill_removes_providers_the_peer_no_longer_has(self):
        await _converges_deletion(PROVIDERS)


class TestRegistryProviderVersions:
    @staticmethod
    def _version(**kw):
        base = {
            "id": uuid.uuid4(),
            "provider_id": uuid.uuid4(),
            "version": "5.42.0",
            "shasums_uploaded": True,
            "shasums_sig_uploaded": True,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        base.update(kw)
        return RegistryProviderVersion(**base)

    @pytest.mark.replication_matrix("registry_provider_versions", "backfill-from-empty")
    async def test_backfill_carries_the_versions(self):
        db = _rows_db([self._version(version="5.42.0"), self._version(version="5.43.0")])

        page = await replication.read_backfill(db, PROVIDER_VERSIONS)

        assert {row["version"] for row in page} == {"5.42.0", "5.43.0"}

    @pytest.mark.replication_matrix("registry_provider_versions", "delta-apply")
    async def test_the_publish_gate_state_travels_with_the_version(self):
        """Binaries are refused until the signature verifies against a registered
        key. Losing that answer lets a promoted node offer a half-published
        version as complete."""
        db = AsyncMock()
        db.add = MagicMock()
        db.scalar.return_value = None
        v = self._version(shasums_uploaded=True, shasums_sig_uploaded=False)

        await replication.apply_upsert(
            db, PROVIDER_VERSIONS, replication.serialize_row(PROVIDER_VERSIONS, v)
        )

        added = db.add.call_args[0][0]
        assert added.shasums_uploaded is True
        assert added.shasums_sig_uploaded is False

    @pytest.mark.replication_matrix("registry_provider_versions", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = self._version()
        db.scalar.return_value = existing
        payload = replication.serialize_row(PROVIDER_VERSIONS, existing)

        await replication.apply_upsert(db, PROVIDER_VERSIONS, payload)
        await replication.apply_upsert(db, PROVIDER_VERSIONS, payload)

        assert existing.version == "5.42.0"

    @pytest.mark.replication_matrix("registry_provider_versions", "delete")
    async def test_deleting_a_version_applies(self):
        await _delete_applies(PROVIDER_VERSIONS)

    @pytest.mark.replication_matrix("registry_provider_versions", "backfill-converges-deletion")
    async def test_backfill_removes_versions_the_peer_no_longer_has(self):
        await _converges_deletion(PROVIDER_VERSIONS)

    def test_the_signing_key_link_travels(self):
        """The download response advertises the publisher's key. A version that
        arrives without its `gpg_key_id` advertises nothing, and the client
        refuses to install."""
        key_id = uuid.uuid4()

        payload = replication.serialize_row(PROVIDER_VERSIONS, self._version(gpg_key_id=key_id))

        assert payload["gpg_key_id"] == str(key_id)


class TestRegistryProviderPlatforms:
    @staticmethod
    def _platform(**kw):
        base = {
            "id": uuid.uuid4(),
            "version_id": uuid.uuid4(),
            "os": "linux",
            "arch": "amd64",
            "shasum": "b" * 64,
            "filename": "terraform-provider-aws_5.42.0_linux_amd64.zip",
            "upload_status": "uploaded",
            "h1_hash": "h1:abc=",
            "created_at": datetime.now(UTC),
        }
        base.update(kw)
        return RegistryProviderPlatform(**base)

    @pytest.mark.replication_matrix("registry_provider_platforms", "backfill-from-empty")
    async def test_backfill_carries_every_platform(self):
        db = _rows_db([self._platform(arch="amd64"), self._platform(arch="arm64")])

        page = await replication.read_backfill(db, PROVIDER_PLATFORMS)

        assert {row["arch"] for row in page} == {"amd64", "arm64"}

    @pytest.mark.replication_matrix("registry_provider_platforms", "delta-apply")
    async def test_the_checksums_travel(self):
        """`shasum` and `h1_hash` are what the client checks the zip against. A
        platform row arriving without them is worse than absent: the download
        proceeds and verification has nothing to compare."""
        db = AsyncMock()
        db.add = MagicMock()
        db.scalar.return_value = None
        p = self._platform()

        await replication.apply_upsert(
            db, PROVIDER_PLATFORMS, replication.serialize_row(PROVIDER_PLATFORMS, p)
        )

        added = db.add.call_args[0][0]
        assert added.shasum == "b" * 64
        assert added.h1_hash == "h1:abc="

    @pytest.mark.replication_matrix("registry_provider_platforms", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = self._platform()
        db.scalar.return_value = existing
        payload = replication.serialize_row(PROVIDER_PLATFORMS, existing)

        await replication.apply_upsert(db, PROVIDER_PLATFORMS, payload)
        await replication.apply_upsert(db, PROVIDER_PLATFORMS, payload)

        assert existing.shasum == "b" * 64

    @pytest.mark.replication_matrix("registry_provider_platforms", "delete")
    async def test_deleting_a_platform_applies(self):
        await _delete_applies(PROVIDER_PLATFORMS)

    @pytest.mark.replication_matrix("registry_provider_platforms", "backfill-converges-deletion")
    async def test_backfill_removes_platforms_the_peer_no_longer_has(self):
        await _converges_deletion(PROVIDER_PLATFORMS)


class TestRegistryModuleVersions:
    @staticmethod
    def _version(**kw):
        base = {
            "id": uuid.uuid4(),
            "module_id": uuid.uuid4(),
            "version": "1.2.3",
            "upload_status": "uploaded",
            "vcs_commit_sha": "",
            "vcs_tag": "",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        base.update(kw)
        return RegistryModuleVersion(**base)

    @pytest.mark.replication_matrix("registry_module_versions", "backfill-from-empty")
    async def test_backfill_carries_the_versions(self):
        db = _rows_db([self._version(version="1.2.3"), self._version(version="1.3.0")])

        page = await replication.read_backfill(db, MODULE_VERSIONS)

        assert {row["version"] for row in page} == {"1.2.3", "1.3.0"}

    @pytest.mark.replication_matrix("registry_module_versions", "delta-apply")
    async def test_a_new_version_applies(self):
        db = AsyncMock()
        db.add = MagicMock()
        db.scalar.return_value = None
        v = self._version()

        await replication.apply_upsert(
            db, MODULE_VERSIONS, replication.serialize_row(MODULE_VERSIONS, v)
        )

        added = db.add.call_args[0][0]
        assert added.version == "1.2.3"
        assert added.upload_status == "uploaded"

    @pytest.mark.replication_matrix("registry_module_versions", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = self._version(inputs=[{"name": "region"}])
        db.scalar.return_value = existing
        payload = replication.serialize_row(MODULE_VERSIONS, existing)

        await replication.apply_upsert(db, MODULE_VERSIONS, payload)
        await replication.apply_upsert(db, MODULE_VERSIONS, payload)

        assert existing.inputs == [{"name": "region"}]

    @pytest.mark.replication_matrix("registry_module_versions", "delete")
    async def test_deleting_a_version_applies(self):
        await _delete_applies(MODULE_VERSIONS)

    @pytest.mark.replication_matrix("registry_module_versions", "backfill-converges-deletion")
    async def test_backfill_removes_versions_the_peer_no_longer_has(self):
        await _converges_deletion(MODULE_VERSIONS)

    def test_the_module_interface_travels(self):
        """The catalog renders its wrapper from the module's declared inputs. A
        version arriving without them provisions an item with no parameters."""
        payload = replication.serialize_row(
            MODULE_VERSIONS, self._version(inputs=[{"name": "region"}], outputs=[{"name": "id"}])
        )

        assert payload["inputs"] == [{"name": "region"}]
        assert payload["outputs"] == [{"name": "id"}]


class TestOrdering:
    """Backfill walks the registry in order, so a child cannot land before the
    parent whose foreign key it carries."""

    def test_parents_come_before_their_children(self):
        names = list(replication.registered())

        assert names.index("registry_modules") < names.index("registry_module_versions")
        assert names.index("registry_providers") < names.index("registry_provider_versions")
        assert names.index("registry_provider_versions") < names.index(
            "registry_provider_platforms"
        )

    def test_signing_keys_come_before_the_versions_that_reference_them(self):
        names = list(replication.registered())

        assert names.index("gpg_keys") < names.index("registry_provider_versions")


class TestNewlyRegisteredClassesBackfill:
    """Registering a class is one line, and on an EXISTING pair that line moves
    no data on its own (#1175).

    A newly registered class has the same problem as a newly added node: there
    are no deltas to carry it. Everything predating the upgrade is outside the
    event stream the follower is following, and anything written during the
    rolling upgrade — while the follower still ran the older code — was skipped
    as an unknown class with its cursor advanced past it. Deltas never replay.

    Found on the live pair: the module row and its tarball both reached the
    follower and the version row did not, because the event naming it was
    consumed by a pod that had never heard of the class.
    """

    async def test_a_class_with_no_cursor_is_backfilled(self):
        from unittest.mock import patch

        from terrapod.services import replication_sync

        db = AsyncMock()
        db.scalar.return_value = None  # no cursor row for anything
        pulled = []

        async def fake_backfill(_db, _client, _token, entity_class):
            pulled.append(entity_class)
            return 0

        with patch.object(replication_sync, "backfill_class", fake_backfill):
            started = await replication_sync.backfill_new_classes(db, AsyncMock(), "tok")

        assert "registry_module_versions" in started
        assert "state_versions" in started
        assert pulled == started, "every class reported must actually have been pulled"

    async def test_a_class_that_has_been_pulled_is_left_alone(self):
        """Otherwise every cycle would re-walk the whole estate."""
        from unittest.mock import patch

        from terrapod.services import replication_sync

        db = AsyncMock()
        db.scalar.return_value = MagicMock()  # a cursor exists for every class
        pulled = []

        async def fake_backfill(_db, _client, _token, entity_class):
            pulled.append(entity_class)
            return 0

        with patch.object(replication_sync, "backfill_class", fake_backfill):
            started = await replication_sync.backfill_new_classes(db, AsyncMock(), "tok")

        assert started == []
        assert pulled == []

    def test_it_runs_before_deltas_are_applied(self):
        """Order matters: a delta for a class that has not been backfilled can
        reference a parent row that is not there yet."""
        import inspect as py_inspect

        from terrapod.services import replication_sync

        source = py_inspect.getsource(replication_sync.sync_cycle)

        assert source.index("backfill_new_classes") < source.index("_apply_event")
