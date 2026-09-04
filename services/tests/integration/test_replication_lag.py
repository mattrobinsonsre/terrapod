"""How far behind is the follower? (#1165)

An integration test rather than a mocked one: the whole point of these fields
is that they come from real aggregate queries over real rows, and a mocked
session would assert nothing about whether `max(id)` and "the first row of this
page" are the values a follower can actually use.

The property under test is that a follower CANNOT answer "how far behind am I"
from its own page. The page is capped at `limit`, so a full page means "there
is more" and nothing at all about how much more — which is why the leader has
to say.
"""

from datetime import UTC, datetime, timedelta

from terrapod.db.models import ReplicationEvent
from terrapod.db.session import get_db_session
from terrapod.services import replication


async def _seed(count: int, *, first_age_minutes: int = 0) -> int:
    """Insert `count` events; return the id of the newest.

    Returning the id matters. The conftest truncates between tests with
    `CASCADE` but not `RESTART IDENTITY`, so rows are cleared while the sequence
    keeps climbing — meaning a test cannot know what id its first row will get,
    and one that hard-codes it is asserting something it does not own (#1493).
    """
    async with get_db_session() as db:
        base = datetime.now(UTC) - timedelta(minutes=first_age_minutes)
        events = [
            ReplicationEvent(
                entity_class="workspaces",
                entity_id=f"ws-{i}",
                op="upsert",
                occurred_at=base + timedelta(seconds=i),
            )
            for i in range(count)
        ]
        db.add_all(events)
        await db.commit()
        return max(event.id for event in events)


class TestTheLeaderSaysWhereItsStreamEnds:
    async def test_a_capped_page_still_reports_the_newest_event_id(self, app):
        newest = await _seed(5)

        async with get_db_session() as db:
            page = await replication.read_events(db, after=0, limit=2)

        assert len(page.events) == 2, "capped — this is the situation being solved"
        assert page.cursor < page.latest_id, (
            "the cursor alone cannot say how far behind; that is why latest_id exists"
        )
        assert page.latest_id == newest

    async def test_the_oldest_unapplied_timestamp_is_the_first_row_handed_back(self, app):
        await _seed(3, first_age_minutes=10)

        async with get_db_session() as db:
            page = await replication.read_events(db, after=0, limit=10)

        assert page.oldest_unapplied_at is not None
        age = (datetime.now(UTC) - page.oldest_unapplied_at.astimezone(UTC)).total_seconds()
        assert 570 <= age <= 630, age

    async def test_a_caught_up_caller_has_nothing_outstanding(self, app):
        await _seed(1)

        async with get_db_session() as db:
            latest = (await replication.read_events(db, after=0, limit=10)).latest_id
            page = await replication.read_events(db, after=latest, limit=10)

        assert page.events == []
        # Distinct from "age unknown": there is nothing outstanding to be old.
        assert page.oldest_unapplied_at is None
        assert page.latest_id == latest

    async def test_an_empty_outbox_reports_zero_rather_than_nothing(self, app):
        async with get_db_session() as db:
            page = await replication.read_events(db, after=0, limit=10)

        assert page.latest_id == 0
        assert page.oldest_unapplied_at is None


class TestTheFollowerTurnsThatIntoAnAnswer:
    """`read_status` is what the HA page and the indicator actually read."""

    async def _cursor(self, position: str, peer_latest: str | None, oldest=None):
        from terrapod.db.models import ReplicationCursor

        async with get_db_session() as db:
            db.add(
                ReplicationCursor(
                    entity_class=replication.EVENT_STREAM,
                    position=position,
                    peer_latest_position=peer_latest,
                    oldest_unapplied_at=oldest,
                )
            )
            await db.commit()

    async def test_never_pulled_is_unknown_not_zero(self, app):
        # The distinction the whole feature rests on: an operator reads 0 as
        # "fine", and would be wrong exactly when it matters.
        await self._cursor("0", None)

        async with get_db_session() as db:
            status = await replication.read_status(db)

        assert status.events_behind is None

    async def test_caught_up_is_zero(self, app):
        await self._cursor("40", "40")

        async with get_db_session() as db:
            status = await replication.read_status(db)

        assert status.events_behind == 0

    async def test_behind_is_the_difference(self, app):
        await self._cursor("40", "58")

        async with get_db_session() as db:
            status = await replication.read_status(db)

        assert status.events_behind == 18

    async def test_a_cursor_past_the_last_reported_peak_clamps_at_zero(self, app):
        # The peer's newest id is from the LAST pull; the local cursor can have
        # moved on since. Negative "behind" is not a thing.
        await self._cursor("60", "58")

        async with get_db_session() as db:
            status = await replication.read_status(db)

        assert status.events_behind == 0
