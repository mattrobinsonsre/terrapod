"""The object-store class register, and the per-class verify-vs-copy config (#1151).

Two things are being pinned here, and they are different in kind.

The **register** is a description of the object store. Its value is that there is
exactly one of it — readiness reads it, the copier reads it, so the two cannot
disagree about what the store contains or which parts of it matter. The tests
below are mostly about that completeness, and about each resolver being careful in
the specific way its class needs.

The **config** is a decision the operator makes, and the tests are about it not
being possible to make it by accident: a typo'd class name is rejected rather than
silently landing on the default, and nothing in the code escalates a mode on the
operator's behalf. What *is* derived is the **tier** — sealing makes a cold cache
terminal rather than inconvenient, and expecting an operator to notice that and
restate it in a second setting is how a config grows a way to be wrong.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from terrapod.config import HABlobsConfig
from terrapod.services import blob_classes
from terrapod.services.blob_classes import COPY, IRREPLACEABLE, OFF, REDERIVABLE, VERIFY


class TestTheRegisterDescribesTheWholeStore:
    """Every prefix in `storage/keys.py` belongs to exactly one class. A prefix
    with no class is a part of the store no operator can make a decision about."""

    def test_every_key_prefix_in_the_store_belongs_to_a_class(self):
        registered = {p.split("/")[0] for c in blob_classes.CLASSES for p in c.prefixes}

        assert registered == {
            "state",
            "config",
            "registry",
            "logs",
            "plans",
            "runs",
            "cache",
            "vcs_archives",
            "module_overrides",
        }

    def test_class_names_are_unique(self):
        assert len(blob_classes.CLASS_NAMES) == len(set(blob_classes.CLASS_NAMES))

    def test_irreplaceable_classes_come_first(self):
        """The order is the order an operator reads a report in, and the order a
        copier has to work in: the classes whose loss is permanent go over the
        link before the ones that re-derive."""
        tiers = [c.tier for c in blob_classes.CLASSES]
        first_non_irreplaceable = tiers.index(blob_classes.HISTORY)

        assert all(t == IRREPLACEABLE for t in tiers[:first_non_irreplaceable])
        assert IRREPLACEABLE not in tiers[first_non_irreplaceable:]

    def test_state_is_first_of_all(self):
        assert blob_classes.CLASSES[0].name == "state"

    def test_the_irreplaceable_tier_matches_1114(self):
        """#1114 lists what is permanently lost if absent. Each entry is here for
        a reason worth stating, so the list cannot be trimmed casually."""
        irreplaceable = {c.name for c in blob_classes.CLASSES if c.tier == IRREPLACEABLE}

        assert irreplaceable == {
            "state",
            "state_index",
            "configuration_versions",
            "registry_modules",
            "registry_providers",
        }

    def test_policy_sets_are_deliberately_absent(self):
        """#1114's own table lists `policies/{ps}/{v}.tar.gz` as irreplaceable.
        There is no such object — policy Rego lives inline in Postgres, so policy
        sets are covered by settings replication, not by this phase. Registering
        the class would give a report a line that always verifies empty."""
        assert not any("policies" in p for c in blob_classes.CLASSES for p in c.prefixes)

    def test_every_class_carries_at_least_one_prefix(self):
        """The prefixes are what a copier enumerates and what an operator greps a
        bucket listing for. A class without one cannot be acted on."""
        assert all(c.prefixes for c in blob_classes.CLASSES)


class TestVerifiableVsCopyOnly:
    """A class is verifiable only when a row GUARANTEES the object. That promise
    is what makes an absent object a finding rather than a guess, and claiming it
    where it does not hold would manufacture exactly the false signal the
    readiness check exists to remove."""

    def test_a_copy_only_class_always_says_why(self):
        for cls in blob_classes.CLASSES:
            if cls.resolver is None:
                assert cls.unverifiable_reason, (
                    f"{cls.name} cannot be verified and does not say why; the gap "
                    "would read as an omission to be filled rather than a boundary"
                )

    def test_a_verifiable_class_does_not_carry_a_reason_it_is_not(self):
        for cls in blob_classes.CLASSES:
            if cls.resolver is not None:
                assert not cls.unverifiable_reason, cls.name

    def test_the_irreplaceable_tier_is_entirely_verifiable(self):
        """The tier that should stop a failover has to be checkable, or the check
        cannot do the one job it exists for."""
        for cls in blob_classes.CLASSES:
            if cls.tier == IRREPLACEABLE:
                assert cls.resolver is not None, cls.name

    def test_run_history_is_copy_only_on_purpose(self):
        """A run writes its logs and plan artifacts only once it reaches the phase
        that produces them, so no row promises them and an absent one is not a
        finding."""
        for name in ("run_logs", "run_plans", "run_vars"):
            assert blob_classes.get(name).resolver is None


class TestSealingChangesTheTiering:
    """A cold provider cache normally re-warms itself. On a sealed node, reaching
    upstream is precisely what is forbidden — so a promoted node with a cold cache
    has no terraform binary and no providers, and can never run anything again."""

    def test_a_cache_is_re_derivable_normally(self):
        cls = blob_classes.get("provider_cache")

        assert cls.tier == REDERIVABLE
        assert blob_classes.effective_tier(cls, sealed=False) == REDERIVABLE

    @pytest.mark.parametrize(
        "name",
        ["provider_cache", "binary_cache", "platform_provider_cache", "cost_pricesheet"],
    )
    def test_sealing_makes_the_upstream_caches_fatal(self, name):
        cls = blob_classes.get(name)

        assert blob_classes.effective_tier(cls, sealed=True) == IRREPLACEABLE

    def test_sealing_does_not_touch_the_vcs_derived_caches(self):
        """`cache_only` seals upstream REGISTRIES, not the operator's own git — a
        sealed node still reaches its VCS, so these genuinely do re-derive."""
        for name in ("vcs_archives", "module_overrides"):
            cls = blob_classes.get(name)
            assert blob_classes.effective_tier(cls, sealed=True) == REDERIVABLE

    def test_state_is_irreplaceable_either_way(self):
        cls = blob_classes.get("state")

        assert blob_classes.effective_tier(cls, sealed=False) == IRREPLACEABLE
        assert blob_classes.effective_tier(cls, sealed=True) == IRREPLACEABLE

    def test_the_escalation_is_read_from_the_registry_config(self):
        """Derived, not a second setting to keep in step."""
        cls = blob_classes.get("binary_cache")

        with patch.object(blob_classes, "_sealed", return_value=True):
            assert blob_classes.effective_tier(cls) == IRREPLACEABLE
        with patch.object(blob_classes, "_sealed", return_value=False):
            assert blob_classes.effective_tier(cls) == REDERIVABLE


class TestTheModeIsTheOperatorsCall:
    def test_the_default_is_verify(self):
        """It observes the whole store, costs nothing until the readiness endpoint
        is called, and commits the operator to nothing — which is what #1114's
        'do not decide for the operator' asks for."""
        cfg = HABlobsConfig()

        assert cfg.mode == VERIFY
        assert cfg.classes == {}
        assert blob_classes.effective_mode(blob_classes.get("state"), blobs=cfg) == VERIFY

    def test_a_per_class_entry_beats_the_global_default(self):
        cfg = HABlobsConfig(mode=VERIFY, classes={"state": COPY})

        assert blob_classes.effective_mode(blob_classes.get("state"), blobs=cfg) == COPY
        assert blob_classes.effective_mode(blob_classes.get("run_logs"), blobs=cfg) == VERIFY

    def test_the_selectivity_1114_asks_for_is_expressible(self):
        """Copy what cannot be regenerated, verify the rest, ignore the history —
        the position no bucket-level replication policy can express, which is the
        entire reason this is per class."""
        cfg = HABlobsConfig(
            mode=VERIFY,
            classes={
                "state": COPY,
                "configuration_versions": COPY,
                "registry_providers": COPY,
                "run_logs": OFF,
                "provider_cache": OFF,
            },
        )

        modes = {c.name: blob_classes.effective_mode(c, blobs=cfg) for c in blob_classes.CLASSES}

        assert modes["state"] == COPY
        assert modes["run_logs"] == OFF
        assert modes["provider_cache"] == OFF
        assert modes["registry_modules"] == VERIFY, "unnamed classes stay on the default"

    def test_nothing_escalates_a_mode_on_the_operators_behalf(self):
        """Sealing escalates the TIER, because that is a fact about the
        deployment. It must not quietly turn on copying, which is a decision about
        their topology and their bandwidth."""
        cfg = HABlobsConfig(mode=VERIFY)
        cls = blob_classes.get("provider_cache")

        with patch.object(blob_classes, "_sealed", return_value=True):
            assert blob_classes.effective_tier(cls) == IRREPLACEABLE
            assert blob_classes.effective_mode(cls, blobs=cfg) == VERIFY


class TestATypoCannotGoQuiet:
    """The worst outcome available: a class the operator believes they configured,
    silently sitting on the default."""

    def test_an_unknown_class_name_is_rejected(self):
        with pytest.raises(ValidationError) as exc:
            HABlobsConfig(classes={"stat": COPY})

        assert "unknown class" in str(exc.value)

    def test_the_error_lists_the_names_that_would_have_worked(self):
        with pytest.raises(ValidationError) as exc:
            HABlobsConfig(classes={"states": COPY})

        assert "configuration_versions" in str(exc.value)

    @pytest.mark.parametrize("bad", ["sync", "on", "true", "", "VERIFY"])
    def test_an_unknown_mode_is_rejected(self, bad):
        with pytest.raises(ValidationError):
            HABlobsConfig(mode=bad)

    def test_an_unknown_per_class_mode_is_rejected(self):
        with pytest.raises(ValidationError) as exc:
            HABlobsConfig(classes={"state": "mirror"})

        assert "state" in str(exc.value)

    def test_effective_mode_takes_a_class_not_a_name(self):
        """Total by construction: there is no way to ask about a class that does
        not exist, which is where a name-keyed lookup would need a fallback — and
        a fallback is where a typo goes quiet."""
        with pytest.raises(AttributeError):
            blob_classes.effective_mode("state", blobs=HABlobsConfig())

    def test_get_raises_on_an_unknown_class(self):
        with pytest.raises(KeyError):
            blob_classes.get("nope")


class TestResolvers:
    """Each resolver turns rows into the keys they imply. The interesting part is
    what each one is careful to include, or careful not to."""

    def _db(self, rows, count=0):
        db = AsyncMock()
        result = MagicMock()
        result.all.return_value = rows
        result.scalar.return_value = count
        db.execute.return_value = result
        return db

    async def test_state_resolves_every_version_not_just_the_latest(self):
        """Rollback is a shipped feature, so a node holding only HEAD has
        silently lost rollback depth — and looks healthy doing it."""
        db = self._db([("ws-1", "sv-1"), ("ws-1", "sv-2")], count=2)

        total, resolved = await blob_classes._resolve_state(db, None)

        assert total == 2
        assert resolved == ["state/ws-1/sv-1.tfstate", "state/ws-1/sv-2.tfstate"]

    async def test_a_provider_platform_also_pulls_its_signed_manifest(self):
        """A present zip with an absent SHA256SUMS still fails `terraform init`,
        so checking only the binary would report a working registry that is not."""
        db = self._db([("default", "aws", "1.0.0", "linux", "amd64")], count=1)

        _, resolved = await blob_classes._resolve_provider_binaries(db, None)

        assert resolved == [
            "registry/providers/default/aws/1.0.0/aws_1.0.0_linux_amd64.zip",
            "registry/providers/default/aws/1.0.0/SHA256SUMS",
            "registry/providers/default/aws/1.0.0/SHA256SUMS.sig",
        ]

    async def test_config_versions_resolve_to_their_tarball(self):
        """The sharpest omission in the store: a VCS workspace can refetch, a
        CLI-uploaded or catalog-provisioned one cannot — this is the only copy."""
        db = self._db([("ws-1", "cv-1")], count=1)

        _, resolved = await blob_classes._resolve_config_versions(db, None)

        assert resolved == ["config/ws-1/cv-1.tar.gz"]

    async def test_modules_resolve_to_their_tarball(self):
        db = self._db([("default", "vpc", "aws", "1.2.0")], count=1)

        _, resolved = await blob_classes._resolve_modules(db, None)

        assert resolved == ["registry/modules/default/vpc/aws/1.2.0.tar.gz"]

    async def test_the_state_index_is_only_expected_when_state_exists(self):
        """It is the break-glass recovery index. On an empty install its absence
        is correct, and reporting it missing would be noise."""
        empty = self._db([], count=0)
        assert await blob_classes._resolve_state_index(empty, None) == (0, [])

        populated = self._db([], count=7)
        assert await blob_classes._resolve_state_index(populated, None) == (
            1,
            ["state/index.yaml"],
        )

    async def test_the_provider_cache_resolves_from_its_recorded_coordinates(self):
        """The row carries the filename verbatim, so the key is recorded rather
        than reconstructed — upstream naming is not ours to guess at."""
        db = self._db(
            [("registry.terraform.io", "hashicorp", "aws", "5.0.0", "aws_5.0.0_linux_amd64.zip")],
            count=1,
        )

        _, resolved = await blob_classes._resolve_provider_cache(db, None)

        assert resolved == [
            "cache/providers/registry.terraform.io/hashicorp/aws/5.0.0/aws_5.0.0_linux_amd64.zip"
        ]

    async def test_the_binary_cache_checks_the_executable_only(self):
        """The publisher manifest and signature cached alongside it were added
        later and are written opportunistically, so a node that cached a binary
        before that legitimately has none. Checking them would report a false gap
        on a healthy node."""
        db = self._db([("tofu", "1.12.0", "linux", "amd64")], count=1)

        _, resolved = await blob_classes._resolve_binary_cache(db, None)

        assert resolved == ["cache/binaries/tofu/1.12.0/linux_amd64"]
        assert not any("SHA256SUMS" in k for k in resolved)

    async def test_every_resolver_honours_the_limit(self):
        """Sampling is the default, so a resolver that ignored `limit` would turn
        a bounded check into thousands of round trips."""
        import inspect

        for cls in blob_classes.CLASSES:
            if cls.resolver is None:
                continue
            source = inspect.getsource(cls.resolver)
            assert "limit" in source, cls.name
