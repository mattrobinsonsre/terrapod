"""Tests for the database-backed token signing key (#1994).

The key signs four stateless token families, so three properties matter more
than the rest and each has its own section below:

* **Bring-your-own wins and the table is not read.** Anything else makes the
  first value win for ever and silently ignores a later rotation.
* **The derivation is preserved.** A supplied key stays `sha256(configured)`
  and an adopted key is `sha256(database_url)`, byte for byte, or every token
  in flight across the upgrade stops verifying.
* **A weak key stays visible.** Adoption carries the database-URL weakness
  forward on purpose; if that stopped being reported the advisory would be
  silently closed rather than fixed.
"""

import hashlib
import secrets
from unittest.mock import MagicMock, patch

import pytest

from terrapod.auth import token_signing
from terrapod.db.models import TokenSigningKey

#: A fixed DSN so the adoption test can pin the *literal* pre-2.0 formula rather
#: than restating the implementation back to itself.
_DSN = "postgresql+asyncpg://u:p@h/db"
_PRE_2_0_KEY = hashlib.sha256(_DSN.encode()).digest()

#: 32 random bytes, base64 — what the docs tell an operator to generate.
_STRONG = "Zq3+9xKcHn0vYt7LpR2wJdF8sMbQe5oUaTgXzN1iCv4="


@pytest.fixture(autouse=True)
def _reset():
    token_signing._reset_cache_for_tests()
    yield
    token_signing._reset_cache_for_tests()


class _FakeSession:
    """A session that answers on the STATEMENT, not on a call counter.

    Dispatching on call order would make the tests assert the sequence the fake
    itself produced, and would keep passing if the code queried the wrong table.
    """

    def __init__(self, *, key_row=None):
        self._key_row = key_row
        self.lock_keys: list[int] = []
        self.added: list[object] = []
        self.commits = 0
        self.statements: list[str] = []

    async def execute(self, stmt, params=None):
        text = str(stmt)
        self.statements.append(text)
        if "pg_advisory_xact_lock" in text:
            self.lock_keys.append((params or {}).get("k"))
            return MagicMock()
        result = MagicMock()
        if "token_signing_keys" in text:
            result.scalar_one_or_none.return_value = self._key_row
        # A `runs` branch used to live here, for the has-this-deployment-executed-a-run
        # question that decided whether to adopt a derived key. Nothing asks it any
        # more -- every deployment generates (#2589) -- and leaving the branch would
        # let a future test pass `has_runs=True` and believe it meant something.
        else:  # pragma: no cover - a query this fake was not told about
            raise AssertionError(f"unexpected statement: {text}")
        return result

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _settings(token_signing_key="", *, strict=False):
    s = MagicMock()
    s.token_signing_key = token_signing_key
    s.require_strong_secrets = strict
    s.database_url = _DSN
    return s


# ── Bring-your-own ───────────────────────────────────────────────────────────


class TestASuppliedKeyWinsAndTheTableIsNotRead:
    """The whole point of supporting bring-your-own properly.

    Adopting the operator's key into the database once and reading the database
    thereafter would make the first value win for ever — so an operator who
    rotated their own Secret would see nothing happen, which is the shape of
    defect this project has shipped before (a flag nothing reads).
    """

    async def test_a_supplied_key_is_used(self):
        db = _FakeSession()
        with patch("terrapod.config.settings", _settings(_STRONG)):
            key = await token_signing.init_token_signing_key(db)
        assert key == hashlib.sha256(_STRONG.encode()).digest()
        assert token_signing.get_provenance() == token_signing.PROVENANCE_CONFIG

    async def test_the_database_is_never_touched(self):
        db = _FakeSession()
        with patch("terrapod.config.settings", _settings(_STRONG)):
            await token_signing.init_token_signing_key(db)
        assert db.statements == [], f"the config path queried the database: {db.statements}"
        assert db.added == []
        assert db.commits == 0

    async def test_a_supplied_key_beats_a_stored_one_so_rotation_takes_effect(self):
        """A later change to the operator's Secret must win over the stored key."""
        stale = TokenSigningKey(key=("11" * 32), provenance=token_signing.PROVENANCE_GENERATED)
        db = _FakeSession(key_row=stale)
        with patch("terrapod.config.settings", _settings(_STRONG)):
            key = await token_signing.init_token_signing_key(db)
        assert key == hashlib.sha256(_STRONG.encode()).digest()
        assert key != bytes.fromhex("11" * 32)

    async def test_whitespace_only_is_treated_as_unset(self):
        db = _FakeSession()
        with patch("terrapod.config.settings", _settings("   ")):
            await token_signing.init_token_signing_key(db)
        assert token_signing.get_provenance() == token_signing.PROVENANCE_GENERATED

    async def test_the_supplied_derivation_is_still_sha256_of_the_material(self):
        """Not the raw bytes: deployments already supplying a key use the hash.

        Handing them the raw material as the key would change their key and
        invalidate every token in flight.
        """
        db = _FakeSession()
        with patch("terrapod.config.settings", _settings("super-secret-signing-key")):
            key = await token_signing.init_token_signing_key(db)
        assert key == hashlib.sha256(b"super-secret-signing-key").digest()


# ── Generate or adopt ────────────────────────────────────────────────────────


class TestEveryDeploymentGeneratesAndADerivedKeyIsRotated:
    async def test_no_runs_generates_a_strong_key(self):
        """Without this branch every fresh 2.0 install would start DSN-derived.

        That would be a regression against the chart-generated key of 1.7.7
        onwards, so the branch is load-bearing rather than a nicety.
        """
        db = _FakeSession()
        with patch("terrapod.config.settings", _settings("")):
            key = await token_signing.init_token_signing_key(db)
        assert len(key) == 32
        assert key != _PRE_2_0_KEY
        assert token_signing.get_provenance() == token_signing.PROVENANCE_GENERATED
        assert len(db.added) == 1
        assert db.added[0].provenance == token_signing.PROVENANCE_GENERATED
        assert db.added[0].key == key.hex()

    async def test_a_used_deployment_generates_too_rather_than_adopting(self):
        """The inverse of what this asserted before 2.0, and still pinned against
        the literal formula -- because the formula is what must NOT appear.

        It used to adopt `sha256(database_url)` here so that tokens in flight
        across the upgrade kept verifying. The cost was that the deployment went
        on holding a key derived from a credential every client of that database
        holds (GHSA-hc47-q72v-4vcm), and `require_strong_secrets` could not
        default on, because it fires on exactly that key. 2.0 takes the other
        trade: what breaks is four families of short-lived token, and nothing
        stored.
        """
        db = _FakeSession()
        with patch("terrapod.config.settings", _settings("")):
            key = await token_signing.init_token_signing_key(db)
        assert key != _PRE_2_0_KEY
        assert len(key) == 32
        assert token_signing.get_provenance() == token_signing.PROVENANCE_GENERATED
        assert db.added[0].key == key.hex()

    async def test_a_stored_key_is_loaded_rather_than_replaced(self):
        row = TokenSigningKey(key=("ab" * 32), provenance=token_signing.PROVENANCE_GENERATED)
        db = _FakeSession(key_row=row)
        with patch("terrapod.config.settings", _settings("")):
            key = await token_signing.init_token_signing_key(db)
        assert key == bytes.fromhex("ab" * 32)
        assert db.added == [], "a stored key must not be rewritten"

    async def test_the_advisory_lock_is_taken_before_the_read(self):
        """Two replicas starting together must not each insert a key.

        Without it they sign with different keys and reject each other's tokens
        — the v1.7.3 outage by a different route. The integration tier proves the
        lock actually serializes; this proves it is asked for, and asked for
        first.
        """
        db = _FakeSession()
        with patch("terrapod.config.settings", _settings("")):
            await token_signing.init_token_signing_key(db)
        assert db.lock_keys == [token_signing._INIT_ADVISORY_LOCK]
        assert "pg_advisory_xact_lock" in db.statements[0], (
            f"the lock was not taken first: {db.statements}"
        )

    async def test_the_lock_key_differs_from_the_cas(self):
        """Sharing one would make two unrelated initializations contend."""
        from terrapod.auth.ca import _CA_INIT_ADVISORY_LOCK

        assert token_signing._INIT_ADVISORY_LOCK != _CA_INIT_ADVISORY_LOCK


# ── The key is never silently absent or silently weak ────────────────────────


class TestFailingClosed:
    def test_the_getter_raises_before_initialization(self):
        """There is deliberately no lazy fallback.

        The only value available without the database is the database-URL
        derivation this change removes, so falling back to it would reintroduce
        the vulnerability exactly when something had gone wrong.
        """
        with pytest.raises(RuntimeError, match="not initialized"):
            token_signing.get_token_signing_key()

    async def test_a_non_hex_stored_key_is_refused(self):
        """Most likely the encryption key changed, so this is not the key."""
        db = _FakeSession(key_row=TokenSigningKey(key="not-hex", provenance="generated"))
        with patch("terrapod.config.settings", _settings("")):
            with pytest.raises(RuntimeError, match="not valid hex"):
                await token_signing.init_token_signing_key(db)

    async def test_a_wrong_length_stored_key_is_refused(self):
        db = _FakeSession(key_row=TokenSigningKey(key="abcd", provenance="generated"))
        with patch("terrapod.config.settings", _settings("")):
            with pytest.raises(RuntimeError, match="expected 32"):
                await token_signing.init_token_signing_key(db)

    async def test_a_strict_refusal_leaves_nothing_initialized(self):
        """A refused key must not end up usable.

        The app lifespan lets the error propagate and the pod dies — but the CA
        initialization beside it is wrapped in a try/except that warns and
        continues, so a future caller copying that shape must not end up running
        happily on a key this function refused. Publishing the globals only after
        the check is what guarantees that.
        """
        # A stored row whose provenance this build does not know. Before 2.0 this
        # case was reached with `has_runs=True`, because a used deployment adopted
        # a derived key and strict mode refused it; a derived key is now replaced
        # rather than adopted, so the stored path needs a different trigger. The
        # supplied-key path is covered by the sibling test below, and this one
        # exists to cover the TABLE path separately.
        db = _FakeSession(
            key_row=TokenSigningKey(key=secrets.token_bytes(32).hex(), provenance="from-the-future")
        )
        with patch("terrapod.config.settings", _settings("", strict=True)):
            with pytest.raises(ValueError):
                await token_signing.init_token_signing_key(db)
        assert token_signing.get_provenance() is None
        with pytest.raises(RuntimeError, match="not initialized"):
            token_signing.get_token_signing_key()

    async def test_a_strict_refusal_of_a_supplied_key_leaves_nothing_initialized(self):
        db = _FakeSession()
        with patch("terrapod.config.settings", _settings("changeme", strict=True)):
            with pytest.raises(ValueError):
                await token_signing.init_token_signing_key(db)
        assert token_signing.get_provenance() is None
        with pytest.raises(RuntimeError, match="not initialized"):
            token_signing.get_token_signing_key()


class TestWhatTheOperatorIsTold:
    def test_a_generated_key_has_no_problem(self):
        assert (
            token_signing.describe_live_key_problem(
                configured="", provenance=token_signing.PROVENANCE_GENERATED
            )
            is None
        )

    def test_an_adopted_key_is_reported_on_provenance_not_score(self):
        """The shipped default DSN measures as STRONG, so a score cannot see this.

        It is weak because the database URL is shared with everything that talks
        to the database, not because it is guessable.
        """
        from terrapod.secret_strength import describe_weakness

        assert describe_weakness(_DSN, name="token_signing_key") is None, (
            "the DSN now measures as weak, which would make this test pass for "
            "the wrong reason — re-derive the point it is making"
        )
        problem = token_signing.describe_live_key_problem(
            configured="", provenance=token_signing.PROVENANCE_DATABASE_URL
        )
        assert problem is not None
        assert "database URL" in problem
        assert "token_signing_key" in problem, "the message must say how to replace it"

    def test_a_weak_supplied_key_is_still_reported(self):
        problem = token_signing.describe_live_key_problem(
            configured="changeme", provenance=token_signing.PROVENANCE_CONFIG
        )
        assert problem is not None

    def test_a_strong_supplied_key_has_no_problem(self):
        assert (
            token_signing.describe_live_key_problem(
                configured=_STRONG, provenance=token_signing.PROVENANCE_CONFIG
            )
            is None
        )

    def test_an_unknown_provenance_is_a_problem(self):
        """A row written by a newer build. Treating it as trustworthy is worse."""
        problem = token_signing.describe_live_key_problem(configured="", provenance="future")
        assert problem is not None and "provenance" in problem

    async def test_strict_does_not_object_to_a_rotated_key(self):
        """What makes `require_strong_secrets` safe to default on in 2.0.

        It used to fire on an adopted key, which is the state an upgrade of a used
        deployment landed in -- so defaulting it on would have refused startup for
        precisely the deployments that had adopted one, which is most of them.
        Replacing such a key on sight is what removes that, and strict mode must
        be satisfied by the replacement rather than by the row it replaced.
        """
        db = _FakeSession(
            key_row=TokenSigningKey(
                key=_PRE_2_0_KEY.hex(), provenance=token_signing.PROVENANCE_DATABASE_URL
            )
        )
        with patch("terrapod.config.settings", _settings("", strict=True)):
            key = await token_signing.init_token_signing_key(db)
        assert key != _PRE_2_0_KEY
        assert token_signing.get_provenance() == token_signing.PROVENANCE_GENERATED

    async def test_strict_does_not_object_to_a_generated_key(self):
        """`require_strong_secrets` used to be fatal whenever no key was CONFIGURED.

        Under the stored-key model an unset `token_signing_key` is the normal,
        strong case, so that would now fail every hardened deployment for doing
        the right thing.
        """
        db = _FakeSession()
        with patch("terrapod.config.settings", _settings("", strict=True)):
            key = await token_signing.init_token_signing_key(db)
        assert len(key) == 32

    async def test_strict_makes_a_weak_supplied_key_fatal(self):
        db = _FakeSession()
        with patch("terrapod.config.settings", _settings("changeme", strict=True)):
            with pytest.raises(ValueError):
                await token_signing.init_token_signing_key(db)


async def test_the_key_is_cached_for_the_process():
    db = _FakeSession()
    with patch("terrapod.config.settings", _settings("")):
        first = await token_signing.init_token_signing_key(db)
    # No settings patch and no session: the getter must not re-derive anything.
    assert token_signing.get_token_signing_key() == first


class TestRotatingAStoredDerivedKey:
    """The branch that makes `require_strong_secrets` safe to default on.

    Changing the no-stored-key path alone would not have been enough: a
    deployment that upgraded under the old rule has a `database-url` row in the
    table, and that row would be LOADED on every later startup -- so the
    exposure persists and strict mode refuses to start. Replacing it on sight is
    what ends with no deployment holding a derived key.
    """

    async def test_a_stored_derived_key_is_replaced_not_loaded(self):
        db = _FakeSession(
            key_row=TokenSigningKey(
                key=_PRE_2_0_KEY.hex(), provenance=token_signing.PROVENANCE_DATABASE_URL
            )
        )
        with patch("terrapod.config.settings", _settings("")):
            key = await token_signing.init_token_signing_key(db)

        assert key != _PRE_2_0_KEY, "the derived key was loaded rather than replaced"
        assert len(key) == 32
        assert token_signing.get_provenance() == token_signing.PROVENANCE_GENERATED
        assert len(db.added) == 1, "the replacement was not persisted"
        assert db.added[0].key == key.hex()
        assert db.added[0].provenance == token_signing.PROVENANCE_GENERATED

    async def test_the_replacement_is_persisted_so_it_happens_once(self):
        """A replacement that is not written would re-rotate on every restart,
        invalidating every token minted since the last one -- which is worse than
        the derived key it replaced, because it never settles."""
        db = _FakeSession(
            key_row=TokenSigningKey(
                key=_PRE_2_0_KEY.hex(), provenance=token_signing.PROVENANCE_DATABASE_URL
            )
        )
        with patch("terrapod.config.settings", _settings("")):
            await token_signing.init_token_signing_key(db)
        assert db.commits >= 1, "nothing was committed, so the next restart rotates again"

        # The row the first startup wrote is what a later startup reads.
        second = _FakeSession(
            key_row=TokenSigningKey(
                **{
                    "key": db.added[0].key,
                    "provenance": db.added[0].provenance,
                }
            )
        )
        with patch("terrapod.config.settings", _settings("")):
            again = await token_signing.init_token_signing_key(second)
        assert again.hex() == db.added[0].key, "the stored replacement was not loaded"
        assert len(second.added) == 0, "it rotated a second time"

    async def test_a_generated_row_is_left_alone(self):
        """Only the derived provenance rotates. A generated key is already strong,
        and replacing it would invalidate tokens for no gain."""
        stored = secrets.token_bytes(32)
        db = _FakeSession(
            key_row=TokenSigningKey(key=stored.hex(), provenance=token_signing.PROVENANCE_GENERATED)
        )
        with patch("terrapod.config.settings", _settings("")):
            key = await token_signing.init_token_signing_key(db)
        assert key == stored
        assert len(db.added) == 0

    async def test_a_supplied_key_is_read_before_the_table_so_nothing_rotates(self):
        """An operator-supplied key wins on every startup, so a deployment that
        supplies one never reaches the rotation at all -- and its derived row is
        left where it is rather than being quietly replaced underneath it."""
        db = _FakeSession(
            key_row=TokenSigningKey(
                key=_PRE_2_0_KEY.hex(), provenance=token_signing.PROVENANCE_DATABASE_URL
            )
        )
        with patch("terrapod.config.settings", _settings(_STRONG)):
            await token_signing.init_token_signing_key(db)
        assert token_signing.get_provenance() == token_signing.PROVENANCE_CONFIG
        assert len(db.added) == 0
        assert db.statements == [], "the table was read at all"
