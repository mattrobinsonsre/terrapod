"""The module autodiscovery rules API (#1584).

Platform admin only. The VCS provider is patched at `github_service`, beneath
the repository walk, so preview and scan run the real service code. The database
is a fake that answers each query by what it selects.
"""

import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.models import ModuleAutodiscoveryRule, VCSConnection
from terrapod.db.session import get_db
from terrapod.services import vcs_rate_limit

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}
_GH = "terrapod.services.github_service"
_URL = "/api/terrapod/v1/module-autodiscovery-rules"
REPO = "https://github.com/org/terraform-azurerm-management-groups"
PATHS = ["main.tf", "modules/create/main.tf", "modules/update/main.tf", "examples/x/main.tf"]
CONN_ID = uuid.uuid4()


def _conn():
    conn = MagicMock(spec=VCSConnection)
    conn.id = CONN_ID
    conn.provider = "github"
    conn.status = "active"
    return conn


def _saved_rule(**kw):
    fields = {
        "id": uuid.uuid4(),
        "name": "mg",
        "vcs_connection_id": CONN_ID,
        "repo_url": REPO,
        "branch": "",
        "pattern": "**/*.tf",
        "ignore_patterns": [],
        "name_template": "",
        "provider": "",
        "vcs_tag_pattern": "v*",
        "labels": {},
        "owner_email": None,
        "enabled": True,
        "first_scan_at": None,
        "last_scanned_sha": "",
        "seen_subdirectories": [],
        "created_at": datetime(2026, 9, 14, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 14, tzinfo=UTC),
    }
    fields.update(kw)
    rule = ModuleAutodiscoveryRule(**fields)
    rule.vcs_connection = _conn()
    return rule


class _FakeDB:
    def __init__(self, rule=None, *, connection=True, registered=(), commit_error=None):
        self.rule = rule
        self.connection = _conn() if connection else None
        self.registered = list(registered)
        self.commit_error = commit_error
        self.added = []
        self.deleted = []
        self.committed = 0

    async def get(self, model, key):
        if model is VCSConnection:
            return self.connection if key == CONN_ID else None
        return self.rule if self.rule is not None and key == self.rule.id else None

    async def execute(self, stmt):
        sql = str(stmt)
        result = MagicMock()
        if "FROM module_autodiscovery_rules" in sql:
            result.scalars.return_value.all.return_value = [self.rule] if self.rule else []
        elif "registry_modules.subdirectory" in sql:
            result.all.return_value = self.registered
        else:
            result.all.return_value = []
        return result

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    @asynccontextmanager
    async def _nested(self):
        yield

    def begin_nested(self):
        return self._nested()

    async def commit(self):
        if self.commit_error:
            raise self.commit_error
        self.committed += 1
        for obj in self.added:
            if isinstance(obj, ModuleAutodiscoveryRule):
                obj.id = obj.id or uuid.uuid4()
                obj.created_at = obj.updated_at = datetime(2026, 9, 14, tzinfo=UTC)
                obj.vcs_tag_pattern = obj.vcs_tag_pattern or "v*"

    async def rollback(self):
        pass

    async def refresh(self, obj):
        pass

    async def flush(self):
        pass


@contextmanager
def _github(paths=PATHS, *, sha="sha1", tree=None):
    tree = tree or AsyncMock(return_value=paths)
    with (
        patch(f"{_GH}.get_repo_default_branch", new=AsyncMock(return_value="main")),
        patch(f"{_GH}.get_repo_branch_sha", new=AsyncMock(return_value=sha)),
        patch(f"{_GH}.list_repo_tree", new=tree),
    ):
        yield tree


def _user(admin=True):
    return AuthenticatedUser(
        email="admin@test.com",
        display_name="A",
        roles=["admin"] if admin else ["everyone"],
        provider_name="local",
        auth_method="session",
    )


async def _call(db, method, path, *, json=None, admin=True):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: _user(admin)
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
        return await c.request(method, f"{_URL}{path}", json=json, headers=_AUTH)


def _body(**attrs):
    base = {
        "name": "mg",
        "vcs-connection-id": f"vcs-{CONN_ID}",
        "repo-url": REPO,
        "pattern": "**/*.tf",
    }
    base.update(attrs)
    return {"data": {"type": "module-autodiscovery-rules", "attributes": base}}


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestCrud:
    async def test_create_returns_the_rule_and_registers_nothing(self, *_):
        db = _FakeDB()
        with _github() as tree:
            resp = await _call(db, "POST", "", json=_body(provider="azurerm"))
        assert resp.status_code == 201, resp.text
        data = resp.json()["data"]
        assert data["type"] == "module-autodiscovery-rules"
        assert data["id"].startswith("modrule-")
        attrs = data["attributes"]
        assert attrs["vcs-connection-id"] == f"vcs-{CONN_ID}"
        assert attrs["provider"] == "azurerm" and attrs["vcs-tag-pattern"] == "v*"
        assert data["relationships"]["vcs-connection"]["data"]["id"] == f"vcs-{CONN_ID}"
        assert [type(o) for o in db.added] == [ModuleAutodiscoveryRule]
        tree.assert_not_awaited()

    async def test_required_fields(self, *_):
        for missing in ("name", "vcs-connection-id", "repo-url", "pattern"):
            body = _body()
            del body["data"]["attributes"][missing]
            resp = await _call(_FakeDB(), "POST", "", json=body)
            assert resp.status_code == 422, missing

    async def test_validation(self, *_):
        cases = {
            "trailing-slash pattern": {"pattern": "modules/*/"},
            "unknown template placeholder": {"name-template": "{nope}"},
            "bad provider": {"provider": "Not Valid"},
            "reserved label": {"labels": {"owner": "x"}},
            "ignore-patterns not a list": {"ignore-patterns": "modules/**"},
            "malformed connection": {"vcs-connection-id": "nope"},
        }
        for what, attrs in cases.items():
            resp = await _call(_FakeDB(), "POST", "", json=_body(**attrs))
            assert resp.status_code == 422, what

    async def test_an_unknown_connection_is_422(self, *_):
        resp = await _call(_FakeDB(connection=False), "POST", "", json=_body())
        assert resp.status_code == 422

    async def test_a_duplicate_name_is_409(self, *_):
        db = _FakeDB(commit_error=IntegrityError("insert", {}, Exception("dup")))
        resp = await _call(db, "POST", "", json=_body())
        assert resp.status_code == 409

    async def test_non_admins_are_refused_everywhere(self, *_):
        rule = _saved_rule()
        for method, path in (
            ("GET", ""),
            ("POST", ""),
            ("POST", "/preview"),
            ("GET", f"/{rule.id}"),
            ("PATCH", f"/{rule.id}"),
            ("DELETE", f"/{rule.id}"),
            ("GET", f"/{rule.id}/preview"),
            ("POST", f"/{rule.id}/scan"),
        ):
            resp = await _call(_FakeDB(rule), method, path, json=_body(), admin=False)
            assert resp.status_code == 403, (method, path)

    async def test_show_accepts_prefixed_and_raw_ids_and_404s_otherwise(self, *_):
        rule = _saved_rule()
        for rid in (f"modrule-{rule.id}", str(rule.id)):
            assert (await _call(_FakeDB(rule), "GET", f"/{rid}")).status_code == 200
        assert (await _call(_FakeDB(rule), "GET", f"/{uuid.uuid4()}")).status_code == 404
        assert (await _call(_FakeDB(rule), "GET", "/not-a-uuid")).status_code == 404

    async def test_list(self, *_):
        resp = await _call(_FakeDB(_saved_rule()), "GET", "")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 1 and "pagination" in body["meta"]

    async def test_moving_the_rule_to_another_repository_starts_it_afresh(self, *_):
        rule = _saved_rule(
            first_scan_at=datetime.now(UTC), last_scanned_sha="s1", seen_subdirectories=["a"]
        )
        other = "https://github.com/org/terraform-aws-other"
        resp = await _call(_FakeDB(rule), "PATCH", f"/{rule.id}", json=_body(**{"repo-url": other}))
        assert resp.status_code == 200
        assert rule.repo_url == other
        assert (rule.seen_subdirectories, rule.last_scanned_sha, rule.first_scan_at) == (
            [],
            "",
            None,
        )

    async def test_renaming_keeps_what_the_rule_has_seen(self, *_):
        rule = _saved_rule(seen_subdirectories=["a"], last_scanned_sha="s1")
        body = {"data": {"attributes": {"name": "renamed"}}}
        assert (await _call(_FakeDB(rule), "PATCH", f"/{rule.id}", json=body)).status_code == 200
        assert rule.name == "renamed" and rule.seen_subdirectories == ["a"]

    async def test_delete(self, *_):
        rule = _saved_rule()
        db = _FakeDB(rule)
        resp = await _call(db, "DELETE", f"/{rule.id}")
        assert resp.status_code == 204 and db.deleted == [rule]


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestPreviewAndScan:
    async def test_preview_of_an_unsaved_rule_reads_the_repository_and_saves_nothing(self, *_):
        db = _FakeDB(registered=[("taken", "azurerm", "modules/create")])
        with _github():
            resp = await _call(db, "POST", "/preview", json=_body())
        assert resp.status_code == 200, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs["ref"] == "main" and attrs["files-walked"] == len(PATHS)
        entries = {e["subdirectory"]: e for e in attrs["entries"]}
        assert list(entries) == ["", "modules/create", "modules/update"]
        assert entries["modules/create"]["registered-as"] == {
            "name": "taken",
            "provider": "azurerm",
        }
        assert entries["modules/update"]["name"] == "management-groups-update"
        assert db.added == [] and db.committed == 0

    async def test_preview_of_a_saved_rule(self, *_):
        rule = _saved_rule()
        with _github():
            resp = await _call(_FakeDB(rule), "GET", f"/{rule.id}/preview")
        assert resp.status_code == 200
        assert resp.json()["data"]["type"] == "module-autodiscovery-rule-previews"

    async def test_scan_registers_everything_and_marks_it_seen(self, *_):
        rule = _saved_rule(enabled=False)
        db = _FakeDB(rule)
        with _github(sha="s7"):
            resp = await _call(db, "POST", f"/{rule.id}/scan")
        assert resp.status_code == 200, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs["modules-registered"] == 3
        assert {m["subdirectory"] for m in attrs["modules"]} == {
            "",
            "modules/create",
            "modules/update",
        }
        assert rule.last_scanned_sha == "s7" and rule.first_scan_at is not None
        assert db.committed == 1

    async def test_scan_registers_only_the_chosen_subset_but_sees_everything(self, *_):
        rule = _saved_rule()
        db = _FakeDB(rule)
        body = {"data": {"attributes": {"subdirectories": ["modules/update"]}}}
        with _github():
            resp = await _call(db, "POST", f"/{rule.id}/scan", json=body)
        assert resp.status_code == 200
        assert [m["subdirectory"] for m in resp.json()["data"]["attributes"]["modules"]] == [
            "modules/update"
        ]
        # Candidates left out are seen, so automatic registration leaves them alone.
        assert set(rule.seen_subdirectories) == {"", "modules/create", "modules/update"}

    async def test_scan_reports_what_it_skipped(self, *_):
        rule = _saved_rule()
        db = _FakeDB(rule, registered=[("x", "azurerm", "modules/create")])
        with _github():
            resp = await _call(db, "POST", f"/{rule.id}/scan")
        skipped = resp.json()["data"]["attributes"]["skipped"]
        assert skipped == [{"subdirectory": "modules/create", "reason": "already-registered"}]

    async def test_an_unknown_subdirectory_is_422_and_nothing_is_registered(self, *_):
        rule = _saved_rule()
        db = _FakeDB(rule)
        body = {"data": {"attributes": {"subdirectories": ["examples/x"]}}}
        with _github():
            resp = await _call(db, "POST", f"/{rule.id}/scan", json=body)
        assert resp.status_code == 422
        assert db.committed == 0

    async def test_subdirectories_must_be_a_list_of_strings(self, *_):
        rule = _saved_rule()
        body = {"data": {"attributes": {"subdirectories": "modules/create"}}}
        with _github() as tree:
            resp = await _call(_FakeDB(rule), "POST", f"/{rule.id}/scan", json=body)
        assert resp.status_code == 422
        tree.assert_not_awaited()

    async def test_a_truncated_tree_is_413(self, *_):
        rule = _saved_rule()
        with _github(tree=AsyncMock(return_value=None)):
            resp = await _call(_FakeDB(rule), "POST", f"/{rule.id}/scan")
        assert resp.status_code == 413

    async def test_the_repository_read_has_its_own_rate_limit_label(self, *_):
        seen = []

        async def tree(*_a):
            seen.append(vcs_rate_limit.current_source())
            return PATHS

        rule = _saved_rule()
        with _github(tree=tree):
            await _call(_FakeDB(rule), "GET", f"/{rule.id}/preview")
        assert seen == ["module-autodiscovery"]
