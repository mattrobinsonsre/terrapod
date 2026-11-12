"""Integration tests for the workspace selector's `exclude` list (real Postgres)."""

import uuid
from unittest.mock import AsyncMock

import pytest

from terrapod.db.models import VCSConnection, Workspace
from terrapod.db.session import get_db_session
from terrapod.services.workspace_search_service import (
    ExclusionReport,
    WorkspaceFilter,
    exclusion_report,
    parse_filter,
)
from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"


async def _ws(client, name, labels=None, **attrs):
    resp = await client.post(
        WS_ENDPOINT,
        json={
            "data": {
                "type": "workspaces",
                "attributes": {"name": name, "labels": labels or {}, **attrs},
            }
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _seed(client):
    """Team `a` (two prod, one dev) plus one dev workspace in team `b`."""
    tag = uuid.uuid4().hex[:8]
    await _ws(client, f"ex-{tag}-1", labels={"team": "a", "env": "prod"})
    await _ws(client, f"ex-{tag}-2", labels={"team": "a", "env": "prod"})
    await _ws(client, f"ex-{tag}-3", labels={"team": "a", "env": "dev"})
    await _ws(client, f"ex-{tag}-4", labels={"team": "b", "env": "dev"})


async def _report(raw):
    async with get_db_session() as db:
        return await exclusion_report(db, parse_filter(raw))


class TestExclusionReport:
    async def test_label_exclude_removes_one_of_three(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"ex-{tag}-1", labels={"team": "a", "env": "prod"})
        await _ws(client, f"ex-{tag}-2", labels={"team": "a", "env": "prod"})
        await _ws(client, f"ex-{tag}-3", labels={"team": "a", "env": "dev"})

        report = await _report({"labels": {"team": "a"}, "exclude": [{"labels": {"env": "dev"}}]})

        assert report == ExclusionReport(excluded=1, empty=[])

    async def test_entry_matching_no_workspace_is_listed_empty(self, app, client):
        set_auth(app, admin_user())
        await _seed(client)

        report = await _report(
            {
                "labels": {"team": "a"},
                "exclude": [{"labels": {"env": "dev"}}, {"labels": {"env": "nowhere"}}],
            }
        )

        assert report.excluded == 1
        assert report.empty == [1]

    async def test_entry_matching_only_outside_the_positive_match_is_not_empty(self, app, client):
        set_auth(app, admin_user())
        await _seed(client)

        # team=b/env=dev exists in the fleet but not in the team=a selection.
        report = await _report(
            {"labels": {"team": "a", "env": "prod"}, "exclude": [{"labels": {"team": "b"}}]}
        )

        assert report == ExclusionReport(excluded=0, empty=[])

    async def test_all_true_with_exclude_counts_the_carve_out(self, app, client):
        set_auth(app, admin_user())
        await _seed(client)

        report = await _report({"all": True, "exclude": [{"labels": {"env": "dev"}}]})

        assert report == ExclusionReport(excluded=2, empty=[])

    async def test_overlapping_entries_count_each_workspace_once(self, app, client):
        set_auth(app, admin_user())
        await _seed(client)

        report = await _report(
            {
                "all": True,
                "exclude": [{"labels": {"env": "dev"}}, {"labels": {"team": "b"}}],
            }
        )

        assert report == ExclusionReport(excluded=2, empty=[])


class TestNoExclude:
    async def test_returns_zero_without_querying(self):
        db = AsyncMock()

        report = await exclusion_report(db, WorkspaceFilter(all=True))

        assert report == ExclusionReport(excluded=0, empty=[])
        db.execute.assert_not_called()
        db.scalar.assert_not_called()


SEARCH = "/api/terrapod/v1/workspaces/actions/search"
BULK = "/api/terrapod/v1/workspaces/actions/bulk-update"


async def _seed_group(client):
    """Two prod and one dev workspace sharing a unique `grp` label."""
    grp = uuid.uuid4().hex[:8]
    for i, env in enumerate(["prod", "prod", "dev"]):
        await _ws(client, f"exr-{grp}-{i}", labels={"grp": grp, "env": env})
    return grp


async def _post(client, path, body):
    return await client.post(path, json=body, headers=AUTH)


async def _backends_by_env(client, grp):
    resp = await _post(client, SEARCH, {"filter": {"labels": {"grp": grp}}})
    assert resp.status_code == 200, resp.text
    return sorted((w["labels"]["env"], w["execution-backend"]) for w in resp.json()["workspaces"])


class TestExcludeRoutes:
    async def test_search_honours_exclude_and_reports_the_carve_out(self, app, client):
        set_auth(app, admin_user())
        grp = await _seed_group(client)

        resp = await _post(
            client,
            SEARCH,
            {"filter": {"labels": {"grp": grp}, "exclude": [{"labels": {"env": "dev"}}]}},
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["matched"] == 2
        assert {w["labels"]["env"] for w in data["workspaces"]} == {"prod"}
        assert data["excluded"] == 1
        assert data["exclude_empty"] == []

    async def test_all_true_with_exclude_through_the_route(self, app, client):
        set_auth(app, admin_user())
        grp = await _seed_group(client)

        resp = await _post(
            client,
            SEARCH,
            {"filter": {"all": True, "exclude": [{"labels": {"env": "dev"}}]}},
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        mine = [w for w in data["workspaces"] if w["labels"].get("grp") == grp]
        assert {w["labels"]["env"] for w in mine} == {"prod"}
        assert all(w["labels"].get("env") != "dev" for w in data["workspaces"])
        assert data["excluded"] >= 1

    async def test_responses_without_exclude_have_no_new_keys(self, app, client):
        set_auth(app, admin_user())
        grp = await _seed_group(client)
        selector = {"labels": {"grp": grp}}

        searched = await _post(client, SEARCH, {"filter": selector})
        dry = await _post(
            client, BULK, {"filter": selector, "update": {"execution-backend": "terraform"}}
        )

        applied = await _post(
            client,
            BULK,
            {"filter": selector, "update": {"execution-backend": "terraform"}, "dry_run": False},
        )

        for resp in (searched, dry, applied):
            assert resp.status_code == 200, resp.text
            assert "excluded" not in resp.json()
            assert "exclude_empty" not in resp.json()

    async def test_dry_run_reports_excluded_and_empty_entries(self, app, client):
        set_auth(app, admin_user())
        grp = await _seed_group(client)

        resp = await _post(
            client,
            BULK,
            {
                "filter": {
                    "labels": {"grp": grp},
                    "exclude": [{"labels": {"env": "dev"}}, {"labels": {"env": "nowhere"}}],
                },
                "update": {"execution-backend": "terraform"},
            },
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["dry_run"] is True
        assert data["matched"] == 2
        assert data["excluded"] == 1
        assert data["exclude_empty"] == [1]
        assert await _backends_by_env(client, grp) == [
            ("dev", "tofu"),
            ("prod", "tofu"),
            ("prod", "tofu"),
        ]

    async def test_apply_with_an_empty_entry_is_refused_and_changes_nothing(self, app, client):
        set_auth(app, admin_user())
        grp = await _seed_group(client)

        resp = await _post(
            client,
            BULK,
            {
                "filter": {
                    "labels": {"grp": grp},
                    "exclude": [{"labels": {"env": "dev"}}, {"labels": {"env": "nowhere"}}],
                },
                "update": {"execution-backend": "terraform"},
                "dry_run": False,
            },
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == (
            "exclude[1] matches no workspace; an exclusion that matches nothing protects nothing"
        )
        assert await _backends_by_env(client, grp) == [
            ("dev", "tofu"),
            ("prod", "tofu"),
            ("prod", "tofu"),
        ]

    async def test_apply_with_exclude_changes_only_the_non_excluded(self, app, client):
        set_auth(app, admin_user())
        grp = await _seed_group(client)

        resp = await _post(
            client,
            BULK,
            {
                "filter": {"labels": {"grp": grp}, "exclude": [{"labels": {"env": "dev"}}]},
                "update": {"execution-backend": "terraform"},
                "dry_run": False,
            },
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["applied"] == 2
        assert resp.json()["excluded"] == 1
        assert resp.json()["exclude_empty"] == []
        assert await _backends_by_env(client, grp) == [
            ("dev", "tofu"),
            ("prod", "terraform"),
            ("prod", "terraform"),
        ]


async def _ws_ids(client, *names, tag):
    return [await _ws(client, f"exm-{tag}-{n}", labels={"grp": tag, "n": n}) for n in names]


class TestMissingWorkspaceIds:
    async def test_entry_with_a_nonexistent_id_is_reported_with_it_named(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        a, _b = await _ws_ids(client, "a", "b", tag=tag)
        ghost = f"ws-{uuid.uuid4()}"

        async with get_db_session() as db:
            report = await exclusion_report(
                db,
                parse_filter({"labels": {"grp": tag}, "exclude": [{"workspace_ids": [a, ghost]}]}),
            )

        assert report.excluded == 1
        assert report.empty == [0]
        assert report.missing_ids == {0: [ghost]}

    async def test_entry_whose_ids_all_exist_is_not_reported(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        a, b = await _ws_ids(client, "a", "b", tag=tag)

        async with get_db_session() as db:
            report = await exclusion_report(
                db,
                parse_filter({"labels": {"grp": tag}, "exclude": [{"workspace_ids": [a, b]}]}),
            )

        assert report == ExclusionReport(excluded=2, empty=[])

    async def test_missing_id_inside_a_multi_dimension_entry_is_reported(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        a, _b = await _ws_ids(client, "a", "b", tag=tag)
        ghost = f"ws-{uuid.uuid4()}"
        entry = {"workspace_ids": [a, ghost], "labels": {"n": "a"}}

        async with get_db_session() as db:
            report = await exclusion_report(
                db, parse_filter({"labels": {"grp": tag}, "exclude": [entry]})
            )

        assert report.excluded == 1
        assert report.empty == [0]
        assert report.missing_ids == {0: [ghost]}

    async def test_dry_run_reports_and_apply_is_refused_without_changes(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        a, _b = await _ws_ids(client, "a", "b", tag=tag)
        ghost = f"ws-{uuid.uuid4()}"
        body = {
            "filter": {"labels": {"grp": tag}, "exclude": [{"workspace_ids": [a, ghost]}]},
            "update": {"execution-backend": "terraform"},
        }

        dry = await _post(client, BULK, body)
        assert dry.status_code == 200, dry.text
        assert dry.json()["exclude_empty"] == [0]

        applied = await _post(client, BULK, {**body, "dry_run": False})
        assert applied.status_code == 422, applied.text
        assert applied.json()["detail"] == (
            f"exclude[0] names workspace ids that do not exist: {ghost}; "
            "an exclusion that matches nothing protects nothing"
        )
        found = await _post(client, SEARCH, {"filter": {"labels": {"grp": tag}}})
        assert {w["execution-backend"] for w in found.json()["workspaces"]} == {"tofu"}


class TestNullableSelectorColumns:
    async def test_null_connection_rows_survive_an_exclusion_that_misses_them(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        async with get_db_session() as db:
            conn = VCSConnection(
                provider="github",
                name=f"conn-{tag}",
                server_url="",
                token="x",
                status="active",
                github_installation_id=int(tag[:7], 16),
            )
            db.add(conn)
            await db.flush()
            db.add(
                Workspace(
                    name=f"exn-{tag}-vcs",
                    labels={"grp": tag},
                    vcs_connection_id=conn.id,
                    vcs_repo_url="https://github.com/acme/repo",
                )
            )
            await db.commit()
            conn_id = conn.id
        await _ws(client, f"exn-{tag}-plain-1", labels={"grp": tag})
        await _ws(client, f"exn-{tag}-plain-2", labels={"grp": tag})
        base = {"labels": {"grp": tag}}

        by_connection = await _post(
            client,
            SEARCH,
            {"filter": {**base, "exclude": [{"vcs_connection_id": f"vcs-{conn_id}"}]}},
        )
        assert by_connection.json()["matched"] == 2
        assert by_connection.json()["excluded"] == 1
        assert by_connection.json()["exclude_empty"] == []

        unused = await _post(
            client,
            SEARCH,
            {"filter": {**base, "exclude": [{"vcs_connection_id": f"vcs-{uuid.uuid4()}"}]}},
        )
        assert unused.json()["matched"] == 3
        assert unused.json()["excluded"] == 0
        assert unused.json()["exclude_empty"] == [0]

        no_vcs = await _post(client, SEARCH, {"filter": {**base, "exclude": [{"has_vcs": False}]}})
        assert no_vcs.json()["matched"] == 1
        assert no_vcs.json()["excluded"] == 2
