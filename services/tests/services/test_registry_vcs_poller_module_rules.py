"""The registry poll cycle runs module autodiscovery first (#1584).

A module a rule registers is polled for tags in the same cycle, and a failure in
the autodiscovery pass never stops tag polling for modules already registered.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services import registry_vcs_poller, vcs_rate_limit

_P = "terrapod.services.registry_vcs_poller"
_SVC = "terrapod.services.module_autodiscovery_service"


def _session(order, modules):
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = modules

    async def execute(_stmt):
        order.append("select-modules")
        return result

    db.execute = execute
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    @asynccontextmanager
    async def session():
        yield db

    return db, session


async def test_rules_run_before_the_module_select_under_their_own_label():
    order: list[str] = []
    labels: list[str] = []

    async def poll_rules(_db):
        order.append("rules")
        labels.append(vcs_rate_limit.current_source())
        return 1

    db, session = _session(order, [])
    with (
        patch(f"{_P}.get_db_session", session),
        patch(f"{_P}.get_storage"),
        patch(f"{_SVC}.poll_rules", new=poll_rules),
    ):
        await registry_vcs_poller.registry_vcs_poll_cycle()

    assert order == ["rules", "select-modules"]
    assert labels == ["module-autodiscovery"]
    db.commit.assert_awaited()


async def test_a_failing_autodiscovery_pass_does_not_stop_tag_polling():
    order: list[str] = []
    module = MagicMock(namespace="default", name="m", provider="aws", labels={})
    db, session = _session(order, [module])
    polled = AsyncMock()
    with (
        patch(f"{_P}.get_db_session", session),
        patch(f"{_P}.get_storage"),
        patch(f"{_SVC}.poll_rules", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch(f"{_P}._poll_module", new=polled),
    ):
        await registry_vcs_poller.registry_vcs_poll_cycle()

    db.rollback.assert_awaited()
    polled.assert_awaited_once()
