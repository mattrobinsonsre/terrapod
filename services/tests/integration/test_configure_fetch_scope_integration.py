"""The poll cycle's fetch union includes the Ansible config root (#2117).

Integration tier, and not for show. The structural gate in
`tests/services/test_configure_fetch_scope.py` proves that
`_compute_paths_unions`' SELECT order and its row unpacking agree *as
written*; only a real SELECT proves they agree in fact. A services-tier test
with a mocked `db.execute` would hand the function tuples built in the order I
believe the select asks for, so it could not catch a transposition at all --
and transposing `configure_enabled` with `config_directory` swaps a boolean
for a path, which is falsy-but-present, so the config root would drop out of
every workspace's scope with no error raised anywhere.

The function is also the cross-workspace union, which is row-level behaviour:
one workspace wanting the whole repo poisons the key for every other
workspace sharing it. That cannot be asserted against a mock either.
"""

import uuid

from terrapod.db.models import VCSConnection, Workspace
from terrapod.db.session import get_db_session
from terrapod.services.vcs_poller import _compute_paths_unions

REPO = "https://github.com/acme/monorepo"


async def _seed(*workspaces: dict) -> tuple[list[uuid.UUID], uuid.UUID]:
    """Seed one connection and N workspaces on the same repo. Returns ids.

    Built through the ORM rather than `text("INSERT INTO workspaces ...")`:
    that table carries ~70 columns, so a hand-written INSERT breaks on the
    next NOT NULL anyone adds, with a driver error naming a column that has
    nothing to do with the test.
    """
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
        ids = []
        for i, fields in enumerate(workspaces):
            ws = Workspace(
                name=f"ws-{tag}-{i}",
                vcs_connection_id=conn.id,
                vcs_repo_url=REPO,
                **fields,
            )
            db.add(ws)
            await db.flush()
            ids.append(ws.id)
        await db.commit()
        return ids, conn.id


async def _union(ids: list[uuid.UUID], conn_id: uuid.UUID) -> list[str]:
    async with get_db_session() as db:
        unions = await _compute_paths_unions(db, ids)
    return unions[(conn_id, "acme", "monorepo")]


class TestTheConfigRootReachesTheFetchScope:
    async def test_a_configure_capable_workspace_contributes_its_root(self, app):
        """The defect, stated as a contrast.

        Both workspaces are identical but for `configure_enabled`, so the
        difference in the union is attributable to the declaration and
        nothing else. With a single fixture this passes however the union is
        written -- including the pre-#2117 way, which omitted the root.
        """
        narrow = {"working_directory": "terraform", "config_directory": "ansible"}

        off_ids, off_conn = await _seed({**narrow, "configure_enabled": False})
        assert await _union(off_ids, off_conn) == ["terraform"]

        on_ids, on_conn = await _seed({**narrow, "configure_enabled": True})
        assert await _union(on_ids, on_conn) == ["ansible", "terraform"], (
            "the configuration tarball for this workspace would contain no playbook, "
            "no roles/, no group_vars/ and no host_vars/ (#2117)"
        )

    async def test_a_config_root_at_the_repository_root_takes_the_whole_repo(self, app):
        # `""` is the root, so the narrowing cannot survive: the empty list is
        # this map's whole-repo sentinel.
        ids, conn = await _seed(
            {
                "working_directory": "terraform",
                "configure_enabled": True,
                "config_directory": "",
            }
        )
        assert await _union(ids, conn) == []

    async def test_a_configure_only_workspace_is_still_narrowed(self, app):
        # No Terraform root to fetch, so the monorepo narrowing holds. The
        # feature is not "a configure means fetch everything".
        ids, conn = await _seed(
            {
                "working_directory": "",
                "plan_apply_enabled": False,
                "configure_enabled": True,
                "config_directory": "ansible",
            }
        )
        assert await _union(ids, conn) == ["ansible"]


class TestTheUnionAcrossWorkspacesSharingARepository:
    async def test_two_workspaces_union_their_terraform_and_ansible_roots(self, app):
        # The coalescing this map exists for: one cache entry serves both, so
        # it must be the union of what each needs -- a plain-Terraform
        # workspace must not narrow the configure-capable one's root away.
        ids, conn = await _seed(
            {"working_directory": "terraform", "configure_enabled": False},
            {
                "working_directory": "terraform",
                "configure_enabled": True,
                "config_directory": "ansible",
            },
        )
        assert await _union(ids, conn) == ["ansible", "terraform"]

    async def test_one_root_level_configure_poisons_the_key_for_the_others(self, app):
        # The whole-repo sentinel has to win across the key, or the workspace
        # that needs everything gets a tarball narrowed by its neighbour.
        ids, conn = await _seed(
            {"working_directory": "terraform", "configure_enabled": False},
            {
                "working_directory": "terraform",
                "configure_enabled": True,
                "config_directory": "",
            },
        )
        assert await _union(ids, conn) == []
