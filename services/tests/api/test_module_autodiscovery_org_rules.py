"""The module autodiscovery rules API for org-wide rules (#1620).

Classification on write (422 for input that names nothing, 502 when the
provider cannot be asked), the preview and scan of rules that range over many
repositories, and the per-repository `/repositories` view. GitHub is a fake
behind `github_service._github_request`, so the real wrappers and service run.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.models import (
    ModuleAutodiscoveryRepository,
    ModuleAutodiscoveryRule,
    RegistryModule,
    VCSConnection,
)
from terrapod.db.session import get_db
from tests.services.test_module_autodiscovery_org_poll import TREE, FakeGitHub, _serve

_URL = "/api/terrapod/v1/module-autodiscovery-rules"
_AUTH = {"Authorization": "Bearer dummy"}
A, B, C = "org/terraform-aws-a", "org/terraform-aws-b", "org/terraform-aws-c"
NOW = datetime(2026, 9, 14, tzinfo=UTC)


def _conn():
    return VCSConnection(
        id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
        provider="github",
        name="gh",
        server_url="",
        token="k",
        status="active",
        github_installation_id=7,
        github_account_login="org",
    )


def _resp(status, body=None):
    return httpx.Response(status, json=body, request=httpx.Request("GET", "https://x"))


class _GitHub(FakeGitHub):
    """The fake installation, plus its account."""

    async def request(self, method, url, token, *, conn, **kw):
        path = url.removeprefix("https://api.github.com")
        if path == "/users/org":
            self.calls.append(path)
            return _resp(200, {"login": "org", "id": 1})
        return await super().request(method, url, token, conn=conn, **kw)


def _gh():
    gh = _GitHub()
    gh.add(A, 11)
    gh.add(B, 12)
    return gh


def _rule(**kw):
    fields = {
        "id": uuid.uuid4(),
        "name": "org",
        "vcs_connection_id": _conn().id,
        "repo_url": "org",
        "target_kind": "namespace",
        "target_id": "1",
        "branch": "",
        "pattern": "**/*.tf",
        "ignore_patterns": [],
        "name_template": "",
        "provider": "",
        "vcs_tag_pattern": "v*",
        "labels": {},
        "owner_email": None,
        "enabled": True,
        "first_scan_at": NOW,
        "last_scanned_sha": "",
        "seen_subdirectories": [],
        "last_error": "",
        "last_enumerated_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(kw)
    rule = ModuleAutodiscoveryRule(**fields)
    rule.vcs_connection = _conn()
    return rule


def _row(path, subs, *, status="active", rid="1", **kw):
    fields = {
        "id": uuid.uuid4(),
        "repo_path": path,
        "repo_url": f"https://github.com/{path}",
        "vcs_repo_id": rid,
        "default_branch": "main",
        "origin": "baseline",
        "status": status,
        "last_scanned_sha": "s1",
        "seen_subdirectories": list(subs),
        "candidates": [{"subdirectory": d, "name": "x", "provider": "aws"} for d in subs],
        "last_skips": [],
        "previous_paths": [],
        "first_seen_at": NOW,
        "failure_count": 0,
        "last_error": "",
    }
    fields.update(kw)
    return ModuleAutodiscoveryRepository(**fields)


def _saved(**kw):
    rule = _rule(**kw)
    for row in (
        _row(A, ["", "modules/a"], rid="11"),
        _row(B, ["", "modules/a"], rid="12"),
        _row(C, ["", "modules/a"], rid="13", status="out-of-scope"),
        _row("org/terraform-aws-d", [], rid="14"),
    ):
        row.rule_id = rule.id
        rule.repositories.append(row)
    return rule


class _DB:
    def __init__(self, rule=None, *, registered=()):
        self.rule = rule
        self.connection = _conn()
        self.registered = list(registered)
        self.added: list = []
        self.deleted: list = []
        self.committed = 0
        self.statements: list[str] = []

    def modules(self):
        return [m for m in self.added if isinstance(m, RegistryModule)]

    async def get(self, model, key):
        if model is VCSConnection:
            return self.connection if key == self.connection.id else None
        return self.rule if self.rule is not None and key == self.rule.id else None

    async def execute(self, stmt):
        sql = str(stmt)
        self.statements.append(sql)
        result = MagicMock()
        rows = list(self.rule.repositories) if self.rule is not None else []
        if "module_autodiscovery_rules.target_kind =" in sql:
            result.all.return_value = []
        elif "count(*)" in sql:
            result.scalar_one.return_value = len(rows)
        elif "FROM module_autodiscovery_repositories" in sql:
            result.scalars.return_value.all.return_value = rows
        elif "FROM module_autodiscovery_rules" in sql:
            result.scalars.return_value.all.return_value = [self.rule] if self.rule else []
        elif "registry_modules.subdirectory" in sql:
            result.all.return_value = [
                *self.registered,
                *((m.name, m.provider, m.subdirectory, m.vcs_repo_url) for m in self.modules()),
            ]
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
        self.committed += 1
        for obj in self.added:
            if isinstance(obj, ModuleAutodiscoveryRule):
                obj.id = obj.id or uuid.uuid4()
                obj.created_at = obj.updated_at = NOW
                obj.vcs_tag_pattern = obj.vcs_tag_pattern or "v*"
            if isinstance(obj, RegistryModule):
                obj.id = obj.id or uuid.uuid4()

    async def rollback(self):
        pass

    async def refresh(self, obj):
        pass

    async def flush(self):
        pass


def _user(admin=True):
    return AuthenticatedUser(
        email="admin@test.com",
        display_name="A",
        roles=["admin"] if admin else ["everyone"],
        provider_name="local",
        auth_method="session",
    )


async def _call(db, method, path, *, json=None, params=None, admin=True):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: _user(admin)
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(method, f"{_URL}{path}", json=json, params=params, headers=_AUTH)


def _body(**attrs):
    base = {
        "name": "org",
        "vcs-connection-id": f"vcs-{_conn().id}",
        "repo-url": "https://github.com/org",
        "pattern": "**/*.tf",
    }
    base.update(attrs)
    return {"data": {"type": "module-autodiscovery-rules", "attributes": base}}


_APP_PATCHES = (
    patch("terrapod.api.app.init_storage", new_callable=AsyncMock),
    patch("terrapod.api.app.init_redis"),
    patch("terrapod.api.app.init_db"),
)


def _app(cls):
    for p in _APP_PATCHES:
        cls = p(cls)
    return cls


@_app
class TestClassificationOnWrite:
    async def test_an_account_is_saved_as_a_namespace(self, *_):
        db, gh = _DB(), _gh()
        with _serve(gh):
            resp = await _call(db, "POST", "", json=_body())
        assert resp.status_code == 201, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs["target-kind"] == "namespace" and attrs["repo-url"] == "https://github.com/org"
        assert attrs["last-error"] == "" and attrs["last-enumerated-at"] is None
        assert db.added[0].target_id == "1"
        # Saving lists and registers nothing.
        assert gh.calls == ["/users/org"] and db.modules() == []

    async def test_a_pattern_and_the_owner_placeholder(self, *_):
        db = _DB()
        with _serve(_gh()):
            resp = await _call(
                db,
                "POST",
                "",
                json=_body(**{"repo-url": "org/terraform-*", "name-template": "{owner}-{repo}"}),
            )
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["attributes"]["target-kind"] == "pattern"

    async def test_a_pattern_that_matches_nothing_yet_is_accepted(self, *_):
        with _serve(_gh()):
            resp = await _call(_DB(), "POST", "", json=_body(**{"repo-url": "org/nothing-*"}))
        assert resp.status_code == 201

    async def test_input_that_names_nothing_is_422_and_nothing_is_saved(self, *_):
        cases = {
            "another account": "https://github.com/someone-else",
            "a missing repository": "org/terraform-aws-missing",
            "a glob before the last segment": "org/*/x",
            "a bare glob": "terraform-*",
            "another host": "https://gitlab.com/org/x",
            "too many segments": "org/a/b",
        }
        for what, url in cases.items():
            db = _DB()
            with _serve(_gh()):
                resp = await _call(db, "POST", "", json=_body(**{"repo-url": url}))
            assert resp.status_code == 422, what
            assert db.added == [] and db.committed == 0, what

    async def test_a_provider_outage_is_502_and_nothing_is_saved(self, *_):
        gh = _gh()
        gh.fail["/repos/org/terraform-aws-a"] = 503
        db = _DB()
        with _serve(gh):
            resp = await _call(db, "POST", "", json=_body(**{"repo-url": A}))
        assert resp.status_code == 502 and "try again" in resp.json()["detail"]
        assert db.added == []

    async def test_a_gitlab_user_namespace_is_422(self, *_):
        db = _DB()
        db.connection = VCSConnection(
            id=_conn().id, provider="gitlab", name="gl", server_url="https://gitlab.com", token="t"
        )

        async def gitlab(method, url, conn, **kw):
            if url.endswith("/namespaces/someone"):
                return _resp(200, {"kind": "user"})
            return _resp(404, {})

        with patch("terrapod.services.gitlab_service._gitlab_request", new=gitlab):
            resp = await _call(db, "POST", "", json=_body(**{"repo-url": "someone"}))
        assert resp.status_code == 422 and "user namespace" in resp.json()["detail"]


@_app
class TestPatch:
    async def test_a_new_repo_url_is_classified_again_and_starts_afresh(self, *_):
        rule = _saved()
        rows = list(rule.repositories)
        db = _DB(rule)
        with _serve(_gh()):
            resp = await _call(db, "PATCH", f"/{rule.id}", json=_body(**{"repo-url": A}))
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["target-kind"] == "repository"
        assert rule.target_id == "11" and rule.first_scan_at is None
        # Every state row goes, and through the ORM so the delete replicates
        # (#1666) — a Core DELETE would never reach the outbox.
        assert db.deleted == rows
        assert not any(s.startswith("DELETE FROM") for s in db.statements)

    async def test_a_change_elsewhere_asks_the_provider_nothing(self, *_):
        rule = _saved()
        gh = _gh()
        with _serve(gh):
            resp = await _call(
                _DB(rule), "PATCH", f"/{rule.id}", json={"data": {"attributes": {"name": "x"}}}
            )
        assert resp.status_code == 200 and gh.calls == []
        assert rule.target_kind == "namespace"

    async def test_resaving_the_same_repo_url_classifies_only_a_rule_in_error(self, *_):
        body = {"data": {"attributes": {"repo-url": "org"}}}
        rule = _saved()
        gh = _gh()
        with _serve(gh):
            assert (await _call(_DB(rule), "PATCH", f"/{rule.id}", json=body)).status_code == 200
        assert gh.calls == []

        rule = _saved(last_error="the group this rule names no longer exists")
        db = _DB(rule)
        with _serve(gh):
            resp = await _call(db, "PATCH", f"/{rule.id}", json=body)
        assert resp.status_code == 200 and gh.calls == ["/users/org"]
        assert rule.last_error == ""
        # The same target again: the baseline stays.
        assert rule.first_scan_at == NOW
        assert not any(s.startswith("DELETE") for s in db.statements)

    async def test_a_repo_url_that_names_nothing_leaves_the_rule_alone(self, *_):
        rule = _saved()
        db = _DB(rule)
        with _serve(_gh()):
            resp = await _call(
                db, "PATCH", f"/{rule.id}", json={"data": {"attributes": {"repo-url": "other"}}}
            )
        assert resp.status_code == 422
        assert rule.repo_url == "org" and rule.target_kind == "namespace" and db.committed == 0


@_app
class TestUnsavedPreview:
    async def test_one_page_is_read_live_with_errors_inline(self, *_):
        gh = _gh()
        gh.add(C, 13)
        gh.add("org/terraform-aws-fork", 14, fork=True)
        gh.fail[f"/repos/{B}/git/trees"] = 500
        with _serve(gh):
            resp = await _call(_DB(), "POST", "/preview", json=_body())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        attrs = body["data"]["attributes"]
        assert attrs["target-kind"] == "namespace" and attrs["listing-complete"] is True
        repos = {r["repository"]: r for r in attrs["repositories"]}
        assert set(repos) == {A, B, C}
        assert repos[B]["status"] == "error" and "500" in repos[B]["error"]
        assert {e["repository"] for e in attrs["entries"]} == {A, C}
        entry = next(
            e for e in attrs["entries"] if e["repository"] == A and e["subdirectory"] == ""
        )
        assert entry["repo-url"] == f"https://github.com/{A}" and entry["name"] == "a"
        assert body["meta"]["pagination"]["total-count"] == 3
        assert attrs["files-walked"] == 2 * len(TREE)

    async def test_the_page_is_bounded(self, *_):
        gh = _gh()
        with _serve(gh):
            resp = await _call(
                _DB(), "POST", "/preview", json=_body(), params={"page[size]": "100"}
            )
        assert resp.json()["meta"]["pagination"]["page-size"] == 25
        gh.calls.clear()
        with _serve(gh):
            resp = await _call(
                _DB(),
                "POST",
                "/preview",
                json=_body(),
                params={"page[size]": "1", "page[number]": "2"},
            )
        assert gh.trees_listed() == [B]
        assert [r["repository"] for r in resp.json()["data"]["attributes"]["repositories"]] == [B]


@_app
class TestSavedPreview:
    async def test_served_from_stored_candidates_with_no_provider_calls(self, *_):
        rule = _saved()
        gh = _gh()
        with _serve(gh):
            resp = await _call(_DB(rule), "GET", f"/{rule.id}/preview")
        assert resp.status_code == 200, resp.text
        assert gh.calls == []
        body = resp.json()
        attrs = body["data"]["attributes"]
        # Out-of-scope and candidate-less repositories are not offered.
        assert [r["repository"] for r in attrs["repositories"]] == [A, B]
        assert {(e["repository"], e["subdirectory"]) for e in attrs["entries"]} == {
            (A, ""),
            (A, "modules/a"),
            (B, ""),
            (B, "modules/a"),
        }
        assert body["meta"]["pagination"]["total-count"] == 2

    async def test_paginated_by_repository(self, *_):
        rule = _saved()
        with _serve(_gh()):
            resp = await _call(_DB(rule), "GET", f"/{rule.id}/preview", params={"page[size]": "1"})
        attrs = resp.json()["data"]["attributes"]
        assert [r["repository"] for r in attrs["repositories"]] == [A]
        assert {e["repository"] for e in attrs["entries"]} == {A}

    async def test_one_repository_can_be_read_live(self, *_):
        rule = _saved()
        gh = _gh()
        with _serve(gh):
            resp = await _call(_DB(rule), "GET", f"/{rule.id}/preview", params={"repository": A})
        assert resp.status_code == 200, resp.text
        assert gh.trees_listed() == [A]
        assert resp.json()["data"]["attributes"]["ref"] == "main"

    async def test_an_unknown_repository_is_404(self, *_):
        rule = _saved()
        with _serve(_gh()):
            resp = await _call(
                _DB(rule), "GET", f"/{rule.id}/preview", params={"repository": "org/nope"}
            )
        assert resp.status_code == 404


@_app
class TestScan:
    async def test_an_empty_body_registers_every_stored_candidate(self, *_):
        rule = _saved()
        db = _DB(rule, registered=[("a", "aws", "", f"https://github.com/{A}")])
        gh = _gh()
        with _serve(gh):
            resp = await _call(db, "POST", f"/{rule.id}/scan")
        assert resp.status_code == 200, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert gh.calls == [] and db.committed == 1
        assert attrs["repositories-scanned"] == 2 and attrs["modules-registered"] == 3
        assert {(m["repository"], m["subdirectory"]) for m in attrs["modules"]} == {
            (A, "modules/a"),
            (B, ""),
            (B, "modules/a"),
        }
        assert all(m["repo-url"].startswith("https://github.com/org/") for m in attrs["modules"])
        assert attrs["skipped"] == [
            {
                "repository": A,
                "repo-url": f"https://github.com/{A}",
                "subdirectory": "",
                "reason": "already-registered",
            }
        ]

    async def test_selections_pick_repositories_and_directories(self, *_):
        rule = _saved()
        db = _DB(rule)
        body = {
            "data": {
                "attributes": {
                    "selections": [
                        {"repository": A, "subdirectories": ["modules/a"]},
                        {"repository": f"https://github.com/{B}.git"},
                    ]
                }
            }
        }
        with _serve(_gh()):
            resp = await _call(db, "POST", f"/{rule.id}/scan", json=body)
        assert resp.status_code == 200, resp.text
        assert {(m.vcs_repo_url, m.subdirectory) for m in db.modules()} == {
            (f"https://github.com/{A}", "modules/a"),
            (f"https://github.com/{B}", ""),
            (f"https://github.com/{B}", "modules/a"),
        }

    async def test_refused_before_registering_anything(self, *_):
        cases = {
            "subdirectories on an org-wide rule": {"subdirectories": ["modules/a"]},
            "an unknown repository": {"selections": [{"repository": "org/nope"}]},
            "an out-of-scope repository": {"selections": [{"repository": C}]},
            "an unknown subdirectory": {
                "selections": [
                    {"repository": B},
                    {"repository": A, "subdirectories": ["examples/x"]},
                ]
            },
            "selections not a list": {"selections": "org/x"},
            "a selection without a repository": {"selections": [{"subdirectories": []}]},
            "subdirectories not a list": {
                "selections": [{"repository": A, "subdirectories": "modules/a"}]
            },
            "too many selections": {"selections": [{"repository": A}] * 201},
        }
        for what, attrs in cases.items():
            rule = _saved()
            db = _DB(rule)
            with _serve(_gh()):
                resp = await _call(
                    db, "POST", f"/{rule.id}/scan", json={"data": {"attributes": attrs}}
                )
            assert resp.status_code == 422, what
            assert db.modules() == [] and db.committed == 0, what


@_app
class TestRepositories:
    async def test_lists_the_rules_repository_state(self, *_):
        rule = _saved()
        resp = await _call(_DB(rule), "GET", f"/{rule.id}/repositories")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["meta"]["pagination"]["total-count"] == 4
        item = body["data"][0]
        assert item["type"] == "module-autodiscovery-rule-repositories"
        assert item["id"].startswith("modrepo-")
        assert item["relationships"]["rule"]["data"] == {
            "id": f"modrule-{rule.id}",
            "type": "module-autodiscovery-rules",
        }
        attrs = item["attributes"]
        assert attrs["repository"] == A and attrs["status"] == "active"
        assert attrs["origin"] == "baseline" and attrs["candidates"][0]["subdirectory"] == ""
        for key in (
            "repo-url",
            "vcs-repo-id",
            "last-scanned-sha",
            "previous-paths",
            "next-check-at",
        ):
            assert key in attrs

    async def test_admin_only_and_404_for_an_unknown_rule(self, *_):
        rule = _saved()
        assert (
            await _call(_DB(rule), "GET", f"/{rule.id}/repositories", admin=False)
        ).status_code == 403
        assert (await _call(_DB(rule), "GET", f"/{uuid.uuid4()}/repositories")).status_code == 404

    async def test_show_reports_the_classification(self, *_):
        rule = _saved(last_error="boom")
        resp = await _call(_DB(rule), "GET", f"/{rule.id}")
        attrs = resp.json()["data"]["attributes"]
        assert (attrs["target-kind"], attrs["last-error"]) == ("namespace", "boom")
        assert attrs["last-enumerated-at"] == "2026-09-14T00:00:00Z"
        assert attrs["last-scanned-sha"] == ""
