"""Module autodiscovery (#1584): matching, naming, preview, registration, polling.

The VCS provider is patched at `github_service` — beneath the service's own
repository helpers — so every test drives the real `resolve_head` /
`list_files`. The database is a small fake that answers each query by what it
selects, so registration runs through the real preview.
"""

import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from terrapod.db.models import ModuleAutodiscoveryRule, RegistryModule
from terrapod.services import module_autodiscovery_service as svc

_GH = "terrapod.services.github_service"
_GL = "terrapod.services.gitlab_service"
REPO = "https://github.com/org/terraform-azurerm-management-groups"
PATHS = [
    "main.tf",
    "variables.tf",
    "modules/create/main.tf",
    "modules/update/main.tf",
    "modules/legacy/main.tf",
    "examples/basic/main.tf",
    "envs/prod/terraform.tfvars",
    ".github/workflows/x.tf",
    "README.md",
]


def _rule(**kw):
    conn = MagicMock()
    conn.provider = "github"
    conn.status = "active"
    fields = {
        "id": uuid.uuid4(),
        "name": "mg",
        "vcs_connection_id": uuid.uuid4(),
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
    }
    fields.update(kw)
    rule = ModuleAutodiscoveryRule(**fields)
    rule.vcs_connection = conn
    return rule


class _FakeDB:
    """Answers the service's three queries by what each one selects.

    Savepoints nest like the real thing: leaving one with an error discards
    everything added inside it, and an insert listed in `fail_add_for` or
    `fail_name_for` fails as it leaves its savepoint, the way a flush does.
    """

    def __init__(self, rules=(), registered=(), taken=(), fail_add_for=(), fail_name_for=()):
        self.rules = list(rules)
        self.registered = list(registered)  # (name, provider, subdirectory)
        self.taken = set(taken)  # names already used by the provider
        # Subdirectories someone else registers between preview and insert.
        self.fail_add_for = set(fail_add_for)
        # Subdirectories whose name someone else takes between preview and insert.
        self.fail_name_for = set(fail_name_for)
        self.added: list[RegistryModule] = []
        self.flush = AsyncMock()

    async def execute(self, stmt):
        sql = str(stmt)
        result = MagicMock()
        if "FROM module_autodiscovery_rules" in sql:
            result.scalars.return_value.all.return_value = self.rules
        elif "registry_modules.subdirectory" in sql:
            result.all.return_value = self.registered
        else:
            result.all.return_value = [(n,) for n in self.taken]
        return result

    def add(self, obj):
        self.added.append(obj)

    @asynccontextmanager
    async def _nested(self):
        mark = len(self.added)
        try:
            yield
        except BaseException:
            del self.added[mark:]
            raise
        for obj in self.added[mark:]:
            if not isinstance(obj, RegistryModule):
                continue
            if obj.subdirectory in self.fail_add_for:
                del self.added[mark:]
                self.registered.append(("other", obj.provider, obj.subdirectory))
                raise IntegrityError("insert", {}, Exception("duplicate subdirectory"))
            if obj.subdirectory in self.fail_name_for:
                del self.added[mark:]
                raise IntegrityError("insert", {}, Exception("duplicate name"))

    def begin_nested(self):
        return self._nested()


@contextmanager
def _github(paths=PATHS, *, sha="sha1", tree=None, default_branch="main"):
    tree = tree or AsyncMock(return_value=paths)
    with (
        patch(f"{_GH}.get_repo_default_branch", new=AsyncMock(return_value=default_branch)),
        patch(f"{_GH}.get_repo_branch_sha", new=AsyncMock(return_value=sha)),
        patch(f"{_GH}.list_repo_tree", new=tree),
    ):
        yield tree


class TestMatching:
    def test_only_module_files_count_whatever_the_pattern_allows(self):
        rule = _rule(pattern="**")
        assert svc.rule_claims_path(rule, "modules/a/main.tf")
        assert svc.rule_claims_path(rule, "modules/a/main.tf.json")
        assert not svc.rule_claims_path(rule, "envs/prod/terraform.tfvars")
        assert not svc.rule_claims_path(rule, "README.md")

    def test_the_pattern_is_a_file_glob(self):
        # `**/*.tf` means files ending in .tf; a .tf.json module needs a wider glob.
        rule = _rule(pattern="**/*.tf")
        assert svc.rule_claims_path(rule, "modules/a/main.tf")
        assert not svc.rule_claims_path(rule, "modules/a/main.tf.json")

    def test_candidates_skip_examples_hidden_and_tfvars_only_dirs_root_first(self):
        assert svc.candidate_subdirectories(_rule(), PATHS) == [
            "",
            "modules/create",
            "modules/legacy",
            "modules/update",
        ]

    def test_the_pattern_narrows_and_ignore_patterns_exclude(self):
        rule = _rule(pattern="modules/**", ignore_patterns=["modules/legacy/**"])
        assert svc.candidate_subdirectories(rule, PATHS) == ["modules/create", "modules/update"]


class TestNaming:
    def test_default_is_the_repositorys_module_name_then_the_leaf(self):
        rule = _rule()
        assert svc.derive_name(rule, "") == "management-groups"
        assert svc.derive_name(rule, "modules/create") == "management-groups-create"

    def test_a_template_renders_and_is_fitted_to_the_name_rule(self):
        rule = _rule(name_template="{repo}_{leaf}")
        assert svc.derive_name(rule, "modules/Create") == "management-groups-create"
        assert svc.derive_name(_rule(name_template="{path}"), "modules/create") == (
            "modules-create"
        )
        assert svc.derive_name(_rule(name_template="{root}"), "9lives") == "m-9lives"

    def test_a_template_is_substituted_not_formatted(self):
        # Only the four placeholders are replaced; anything else is literal text
        # (the API refuses it, but a stored template must never be evaluated).
        assert svc.derive_name(_rule(name_template="{repo.__class__}"), "") == "repo-class"
        rule = _rule(name_template="{leaf!r}-{leaf}")
        assert svc.derive_name(rule, "modules/x") == "leaf-r-x"

    @pytest.mark.parametrize(
        "template,ok",
        [
            ("", True),
            ("platform", True),
            ("{repo}-{leaf}", True),
            ("x{path}y{root}", True),
            ("{nope}", False),
            ("{leaf:>10}", False),
            ("{repo.__class__}", False),
            ("{leaf!r}", False),
            ("{}", False),
            ("{0}", False),
            ("{{repo}}", False),
            ("a{", False),
            ("a}", False),
        ],
    )
    def test_the_template_grammar(self, template, ok):
        assert bool(svc.TEMPLATE_RE.match(template)) is ok

    def test_provider_explicit_else_from_the_repository_name(self):
        assert svc.derive_provider(_rule(provider="aws")) == "aws"
        assert svc.derive_provider(_rule()) == "azurerm"
        assert svc.derive_provider(_rule(repo_url="https://github.com/org/platform")) == ""


class TestPreview:
    async def test_flags_registered_taken_and_missing_provider(self):
        db = _FakeDB(
            registered=[("mg-create-existing", "azurerm", "modules/create")],
            taken={"management-groups-update"},
        )
        entries = {e["subdirectory"]: e for e in await svc.preview(db, _rule(), PATHS)}
        assert entries["modules/create"]["registered-as"] == {
            "name": "mg-create-existing",
            "provider": "azurerm",
        }
        assert entries["modules/create"]["collision"] is False
        assert entries["modules/update"]["collision"] is True
        assert entries[""]["collision"] is False
        assert not any(e["missing-provider"] for e in entries.values())

    async def test_candidates_deriving_the_same_name_collide_with_each_other(self):
        # `{leaf}` gives `x` for both directories: neither is clean.
        rule = _rule(name_template="{leaf}")
        paths = ["a/x/main.tf", "b/x/main.tf", "c/y/main.tf"]
        entries = {e["subdirectory"]: e for e in await svc.preview(_FakeDB(), rule, paths)}
        assert entries["a/x"]["collision"] and entries["b/x"]["collision"]
        assert entries["c/y"]["collision"] is False

    async def test_a_registered_directory_does_not_collide_with_its_own_name(self):
        rule = _rule(name_template="{leaf}")
        paths = ["a/x/main.tf", "b/x/main.tf"]
        db = _FakeDB(registered=[("x-a", "azurerm", "a/x")])
        entries = {e["subdirectory"]: e for e in await svc.preview(db, rule, paths)}
        assert entries["b/x"]["collision"] is False

    async def test_no_provider_is_reported_not_guessed(self):
        rule = _rule(repo_url="https://github.com/org/platform")
        entries = await svc.preview(_FakeDB(), rule, PATHS)
        assert entries and all(e["missing-provider"] and e["provider"] == "" for e in entries)


class TestRegister:
    async def test_registers_each_candidate_as_a_vcs_module_of_the_rule(self):
        rule = _rule(labels={"team": "platform"}, owner_email="o@example.com", branch="main")
        db = _FakeDB()
        result = await svc.register_candidates(db, rule, PATHS)

        by_sub = {m.subdirectory: m for m in result.created}
        assert set(by_sub) == {"", "modules/create", "modules/legacy", "modules/update"}
        m = by_sub["modules/create"]
        assert (m.namespace, m.name, m.provider) == (
            "default",
            "management-groups-create",
            "azurerm",
        )
        assert m.source == "vcs"
        assert m.vcs_connection_id == rule.vcs_connection_id
        assert (m.vcs_repo_url, m.vcs_branch, m.vcs_tag_pattern) == (REPO, "main", "v*")
        assert m.module_autodiscovery_rule_id == rule.id
        assert m.labels == {"team": "platform"} and m.owner_email == "o@example.com"
        assert result.skipped == []

    async def test_only_the_chosen_subset(self):
        db = _FakeDB()
        result = await svc.register_candidates(db, _rule(), PATHS, only=["modules/update"])
        assert [m.subdirectory for m in result.created] == ["modules/update"]

    async def test_an_unknown_subdirectory_is_refused_before_registering_anything(self):
        db = _FakeDB()
        with pytest.raises(svc.UnknownSubdirectoryError, match="examples/basic"):
            await svc.register_candidates(
                db, _rule(), PATHS, only=["modules/create", "examples/basic"]
            )
        assert db.added == []

    async def test_registered_taken_and_providerless_candidates_are_skipped(self):
        db = _FakeDB(
            registered=[("x", "azurerm", "modules/create")], taken={"management-groups-update"}
        )
        result = await svc.register_candidates(db, _rule(), PATHS)
        assert dict(result.skipped) == {
            "modules/create": "already-registered",
            "modules/update": "name-taken",
        }
        nameless = await svc.register_candidates(
            _FakeDB(), _rule(repo_url="https://github.com/org/platform"), PATHS
        )
        assert nameless.created == [] and {r for _, r in nameless.skipped} == {"missing-provider"}

    async def test_a_race_on_one_module_skips_it_and_keeps_the_rest(self):
        db = _FakeDB(fail_add_for={"modules/create"})
        result = await svc.register_candidates(db, _rule(), PATHS)
        assert ("modules/create", "already-registered") in result.skipped
        assert "modules/update" in {m.subdirectory for m in result.created}

    async def test_a_race_on_a_name_is_reported_as_name_taken(self):
        db = _FakeDB(fail_name_for={"modules/create"})
        result = await svc.register_candidates(db, _rule(), PATHS)
        assert ("modules/create", "name-taken") in result.skipped
        assert "modules/create" not in {m.subdirectory for m in db.added}

    async def test_candidates_clashing_with_each_other_are_both_skipped_as_name_taken(self):
        rule = _rule(name_template="{leaf}")
        db = _FakeDB()
        result = await svc.register_candidates(db, rule, ["a/x/main.tf", "b/x/main.tf"])
        assert result.created == [] and db.added == []
        assert dict(result.skipped) == {"a/x": "name-taken", "b/x": "name-taken"}

    async def test_reserved_labels_are_dropped_not_copied(self):
        result = await svc.register_candidates(
            _FakeDB(), _rule(labels={"owner": "x", "team": "t"}), PATHS, only=[""]
        )
        assert result.created[0].labels == {"team": "t"}


class TestRecordScan:
    def test_accumulates_what_was_seen(self):
        rule = _rule(seen_subdirectories=["gone"])
        svc.record_scan(rule, PATHS, "sha9")
        assert rule.seen_subdirectories == [
            "",
            "gone",
            "modules/create",
            "modules/legacy",
            "modules/update",
        ]
        assert rule.last_scanned_sha == "sha9"
        first = rule.first_scan_at
        assert first is not None
        svc.record_scan(rule, PATHS, "sha10")
        assert rule.first_scan_at == first


class TestPoll:
    async def test_the_first_poll_records_a_baseline_and_registers_nothing(self):
        rule = _rule()
        db = _FakeDB(rules=[rule])
        with _github(sha="s1"):
            assert await svc.poll_rules(db) == 0
        assert db.added == []
        assert rule.first_scan_at is not None and rule.last_scanned_sha == "s1"
        assert "modules/create" in rule.seen_subdirectories

    async def test_an_unmoved_branch_is_not_walked(self):
        rule = _rule(
            first_scan_at=datetime.now(UTC),
            last_scanned_sha="s1",
            seen_subdirectories=["", "modules/create"],
        )
        with _github(sha="s1") as tree:
            await svc.poll_rules(_FakeDB(rules=[rule]))
        tree.assert_not_awaited()

    async def test_a_new_directory_is_registered_and_unregistered_old_ones_are_left(self):
        # modules/update and modules/legacy were seen (an operator left them
        # out); modules/new appears on the branch.
        rule = _rule(
            first_scan_at=datetime.now(UTC),
            last_scanned_sha="s1",
            seen_subdirectories=["", "modules/create", "modules/legacy", "modules/update"],
        )
        db = _FakeDB(rules=[rule])
        with _github([*PATHS, "modules/new/main.tf"], sha="s2"):
            assert await svc.poll_rules(db) == 1
        assert [m.subdirectory for m in db.added] == ["modules/new"]
        assert "modules/new" in rule.seen_subdirectories and rule.last_scanned_sha == "s2"

    async def test_a_truncated_tree_leaves_the_rule_to_retry(self):
        rule = _rule()
        with _github(tree=AsyncMock(return_value=None)):
            await svc.poll_rules(_FakeDB(rules=[rule]))
        assert rule.first_scan_at is None and rule.last_scanned_sha == ""

    async def test_one_failing_rule_does_not_stop_the_next(self):
        broken = _rule(name="broken", repo_url="not a url")
        good = _rule(name="good")
        with _github(sha="s1"):
            await svc.poll_rules(_FakeDB(rules=[broken, good]))
        assert broken.first_scan_at is None
        assert good.first_scan_at is not None

    async def test_an_inactive_connection_is_skipped_with_a_warning(self):
        rule = _rule()
        rule.vcs_connection.status = "suspended"
        with _github() as tree, patch.object(svc.logger, "warning") as warn:
            await svc.poll_rules(_FakeDB(rules=[rule]))
        tree.assert_not_awaited()
        assert rule.first_scan_at is None
        warn.assert_called_once()
        assert "not active" in warn.call_args.args[0]
        assert warn.call_args.kwargs["rule_id"] == str(rule.id)
        assert warn.call_args.kwargs["connection_status"] == "suspended"

    async def test_a_database_error_in_one_rule_does_not_undo_another(self):
        # Both rules find a new directory. The first rule's flush — after its
        # registration and record_scan — fails; its work is rolled back in its
        # own savepoint, and the second rule's registration stands.
        def baselined(name):
            return _rule(
                name=name,
                first_scan_at=datetime.now(UTC),
                last_scanned_sha="s1",
                seen_subdirectories=["", "modules/create", "modules/legacy", "modules/update"],
            )

        first, second = baselined("first"), baselined("second")
        db = _FakeDB(rules=[first, second])
        db.flush.side_effect = [RuntimeError("value too long"), None]
        with _github([*PATHS, "modules/new/main.tf"], sha="s2"):
            assert await svc.poll_rules(db) == 1
        assert [m.module_autodiscovery_rule_id for m in db.added] == [second.id]
        assert second.last_scanned_sha == "s2"

    async def test_each_rule_is_polled_in_its_own_savepoint(self):
        rules = [_rule(name="a"), _rule(name="b")]
        db = _FakeDB(rules=rules)
        opened = []
        real = db.begin_nested

        def counting():
            opened.append(1)
            return real()

        db.begin_nested = counting
        with _github(sha="s1"):
            await svc.poll_rules(db)
        # One per rule: a first poll registers nothing, so no inner savepoints.
        assert len(opened) == 2

    async def test_the_default_branch_is_used_when_the_rule_names_none(self):
        rule = _rule()
        with _github(default_branch="trunk") as tree:
            await svc.poll_rules(_FakeDB(rules=[rule]))
        assert tree.await_args.args[-1] == "trunk"


class TestRepositoryErrors:
    async def test_an_unknown_provider_is_422(self):
        conn = SimpleNamespace(provider="bitbucket")
        with pytest.raises(svc.RepositoryError) as e:
            await svc.resolve_head(conn, REPO, "main")
        assert e.value.status == 422

    async def test_a_gitlab_listing_error_is_502_with_the_cause_not_413(self):
        conn = SimpleNamespace(provider="gitlab", server_url="https://gitlab.example.com")
        head = svc.RepositoryHead(owner="org", repo="r", branch="gone", sha=None)
        req = httpx.Request("GET", "https://gitlab.example.com/api/v4/x")
        resp = httpx.Response(404, request=req, json={"message": "404 Tree Not Found"})
        with patch(f"{_GL}._gitlab_request", new=AsyncMock(return_value=resp)):
            with pytest.raises(svc.RepositoryError) as e:
                await svc.list_files(conn, head)
        assert e.value.status == 502
        assert "404" in e.value.detail

    async def test_gitlab_still_returns_none_on_error_for_best_effort_callers(self):
        from terrapod.services import gitlab_service

        conn = SimpleNamespace(provider="gitlab", server_url="https://gitlab.example.com")
        req = httpx.Request("GET", "https://gitlab.example.com/api/v4/x")
        resp = httpx.Response(404, request=req)
        with patch(f"{_GL}._gitlab_request", new=AsyncMock(return_value=resp)):
            assert await gitlab_service.list_repo_tree(conn, "org", "r", "gone") is None

    async def test_a_truncated_tree_is_413(self):
        conn = SimpleNamespace(provider="github")
        head = svc.RepositoryHead(owner="org", repo="r", branch="main", sha=None)
        with patch(f"{_GH}.list_repo_tree", new=AsyncMock(return_value=None)):
            with pytest.raises(svc.RepositoryError) as e:
                await svc.list_files(conn, head)
        assert e.value.status == 413


class TestGitHubTreeRef:
    async def test_the_ref_is_url_encoded_in_the_tree_path(self):
        from terrapod.services import github_service

        conn = SimpleNamespace(server_url="")
        resp = httpx.Response(
            200,
            request=httpx.Request("GET", "https://api.github.com/x"),
            json={"truncated": False, "tree": [{"path": "main.tf", "type": "blob"}]},
        )
        request = AsyncMock(return_value=resp)
        with (
            patch(f"{_GH}.get_installation_token", new=AsyncMock(return_value="t")),
            patch(f"{_GH}._github_request", new=request),
        ):
            paths = await github_service.list_repo_tree(conn, "org", "r", "feature/x#1?y")
        assert paths == ["main.tf"]
        url = request.await_args.args[1]
        assert url.endswith("/repos/org/r/git/trees/feature%2Fx%231%3Fy?recursive=1")
