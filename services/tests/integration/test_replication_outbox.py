"""The outbox records what was actually written (#1173).

Integration rather than mocked, and that is the whole point. The bug these pin
survived every existing test because those tests read this module's *source*
(`inspect.getsource`) and assert on what it says, rather than flushing a row and
looking at what came out. A hook can be present, correct-looking, registered
against the right classes, and still record nothing.

**The bug:** the outbox ran on `before_flush`, where a pending INSERT has no
primary key yet — almost every model takes its id from a column `default=`
(`generate_uuid7`), which SQLAlchemy evaluates *during* the flush. So
`row_entity_id` returned None and the event was silently dropped. Creating a
workspace replicated nothing; updating or deleting one (ids already loaded)
worked fine, which is what made it so easy to miss.

It is invisible from inside a single node: the row lands, the API returns 201,
and the only symptom is a promoted follower that has never heard of the
workspace. A live two-node pair found it in minutes.
"""

from sqlalchemy import func, select

from terrapod.db.models import ReplicationEvent, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import replication


async def _events(entity_class: str) -> list[ReplicationEvent]:
    async with get_db_session() as db:
        return list(
            (
                await db.scalars(
                    select(ReplicationEvent).where(ReplicationEvent.entity_class == entity_class)
                )
            ).all()
        )


class TestInsertWithAGeneratedPrimaryKey:
    """The regression. Every one of these fails on the pre-fix code."""

    async def test_creating_a_workspace_records_an_event(self, app):
        replication.install_outbox_hooks()

        async with get_db_session() as db:
            db.add(Workspace(name="outbox-create"))
            await db.commit()

        events = await _events("workspaces")
        assert len(events) == 1, (
            "a created workspace produced no outbox event, so it would never "
            "reach the follower and a promotion would silently lose it"
        )
        assert events[0].op == replication.UPSERT

    async def test_the_event_carries_the_id_the_row_actually_got(self, app):
        """Not merely "an event exists" — it must identify the right row. A
        placeholder id would replicate a workspace that does not exist."""
        replication.install_outbox_hooks()

        async with get_db_session() as db:
            ws = Workspace(name="outbox-identity")
            db.add(ws)
            await db.commit()
            written_id = str(ws.id)

        events = await _events("workspaces")
        assert written_id in events[0].entity_id
        assert "None" not in events[0].entity_id

    async def test_the_event_is_in_the_same_transaction_as_the_write(self, app):
        """A rolled-back write must not leave an event claiming it happened —
        the follower would try to fetch a row the leader never kept."""
        replication.install_outbox_hooks()

        async with get_db_session() as db:
            db.add(Workspace(name="outbox-rollback"))
            await db.flush()
            await db.rollback()

        async with get_db_session() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(Workspace)
                    .where(Workspace.name == "outbox-rollback")
                )
                == 0
            )
        assert await _events("workspaces") == []


class TestUpdateAndDelete:
    """These always worked — the ids were already loaded. Pinned so the fix for
    the insert path cannot break them."""

    async def test_updating_a_workspace_records_an_upsert(self, app):
        replication.install_outbox_hooks()
        async with get_db_session() as db:
            ws = Workspace(name="outbox-update")
            db.add(ws)
            await db.commit()
            ws_id = ws.id

        async with get_db_session() as db:
            ws = await db.get(Workspace, ws_id)
            ws.auto_apply = True
            await db.commit()

        ops = [e.op for e in await _events("workspaces")]
        assert ops == [replication.UPSERT, replication.UPSERT]

    async def test_deleting_a_workspace_records_a_delete(self, app):
        replication.install_outbox_hooks()
        async with get_db_session() as db:
            ws = Workspace(name="outbox-delete")
            db.add(ws)
            await db.commit()
            ws_id = ws.id

        async with get_db_session() as db:
            await db.delete(await db.get(Workspace, ws_id))
            await db.commit()

        ops = [e.op for e in await _events("workspaces")]
        assert ops == [replication.UPSERT, replication.DELETE], (
            "a delete that does not replicate leaves the follower serving a "
            "workspace the leader has removed"
        )


class TestOrigin:
    async def test_the_event_is_tagged_with_this_node(self, app):
        """Origin tags are what stop a pair echoing each other's writes back and
        forth, so an untagged event is not a cosmetic problem."""
        replication.install_outbox_hooks()

        async with get_db_session() as db:
            db.add(Workspace(name="outbox-origin"))
            await db.commit()

        from terrapod.config import settings

        assert (await _events("workspaces"))[0].origin_node == settings.ha.node_name
