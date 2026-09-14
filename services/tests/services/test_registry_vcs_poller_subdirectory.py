"""Publishing a submodule from its subdirectory (#1583).

The registry poller downloads a tag's whole repository archive. For a module
with a `subdirectory` it must store only that subdirectory, re-rooted, so the
tarball — and the interface extracted from it — is the submodule's. A tag with
nothing there (typically cut before the submodule existed) is not published,
and is noted so the next poll does not download it again.
"""

import io
import tarfile
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import settings
from terrapod.services import registry_vcs_poller

_P = "terrapod.services.registry_vcs_poller"


def _repo_archive(files: dict[str, bytes]) -> bytes:
    """A VCS-style archive: everything under one wrapper directory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(f"org-repo-abc123/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


WITH_SUBMODULE = _repo_archive(
    {
        "main.tf": b'variable "root_only" {}\n',
        "modules/create/main.tf": b'variable "name" {\n  type = string\n}\n',
        "modules/create/outputs.tf": b'output "id" {\n  value = "x"\n}\n',
    }
)
BEFORE_SUBMODULE = _repo_archive({"main.tf": b'variable "root_only" {}\n'})


class _FakeRedis:
    def __init__(self):
        self.keys: dict[str, str] = {}

    async def exists(self, key):
        return int(key in self.keys)

    async def set(self, key, value, ex=None):  # noqa: ARG002
        self.keys[key] = value


def _module(subdirectory="modules/create"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="create",
        namespace="default",
        provider="azurerm",
        vcs_connection_id=uuid.uuid4(),
        vcs_repo_url="https://github.com/org/repo",
        vcs_tag_pattern="v*",
        vcs_last_tag="",
        versions=[],
        status="pending",
        subdirectory=subdirectory,
    )


def _db():
    conn = MagicMock()
    conn.provider = "github"
    result = MagicMock()
    result.scalars.return_value.first.return_value = conn
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


def _members(archive: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        return {m.name for m in tf.getmembers() if not m.isdir()}


@pytest.fixture
def vcs(monkeypatch):
    """Two tags: v1.0.0 has the submodule, v0.1.0 predates it."""
    downloads: list[str] = []
    archives = {"v1.0.0": WITH_SUBMODULE, "v0.1.0": BEFORE_SUBMODULE}

    async def download(conn, owner, repo, tag):  # noqa: ARG001
        downloads.append(tag)
        return archives[tag]

    monkeypatch.setattr(
        registry_vcs_poller, "_dispatch_parse_repo_url", lambda _p: lambda _u: ("org", "repo")
    )
    monkeypatch.setattr(
        registry_vcs_poller,
        "_dispatch_list_tags",
        lambda _p: AsyncMock(
            return_value=[
                {"name": "v1.0.0", "sha": "sha-new"},
                {"name": "v0.1.0", "sha": "sha-old"},
            ]
        ),
    )
    monkeypatch.setattr(registry_vcs_poller, "_dispatch_download_archive", lambda _p: download)
    monkeypatch.setattr(settings.registry.module_interface, "enabled", True)
    return downloads


def _stored(storage) -> dict[str, bytes]:
    return {call.args[0]: call.args[1] for call in storage.put.await_args_list}


@patch(
    "terrapod.services.module_impact_service.trigger_linked_workspace_runs", new_callable=AsyncMock
)
async def test_the_submodule_is_published_re_rooted_with_its_own_interface(_trigger, vcs):
    redis = _FakeRedis()
    storage = MagicMock(put=AsyncMock())
    db = _db()

    with patch(f"{_P}.get_redis_client", return_value=redis):
        await registry_vcs_poller._poll_module(db, storage, _module())

    stored = _stored(storage)
    assert list(stored) == ["registry/modules/default/create/azurerm/1.0.0.tar.gz"], stored.keys()
    assert _members(next(iter(stored.values()))) == {"main.tf", "outputs.tf"}

    versions = [c.args[0] for c in db.add.call_args_list]
    assert [v.version for v in versions] == ["1.0.0"]
    assert [i["name"] for i in versions[0].inputs] == ["name"], "the interface is the submodule's"
    assert [o["name"] for o in versions[0].outputs] == ["id"]


@patch(
    "terrapod.services.module_impact_service.trigger_linked_workspace_runs", new_callable=AsyncMock
)
async def test_a_tag_without_the_subdirectory_is_skipped_and_not_downloaded_again(_trigger, vcs):
    redis = _FakeRedis()
    module = _module()

    with patch(f"{_P}.get_redis_client", return_value=redis):
        await registry_vcs_poller._poll_module(_db(), MagicMock(put=AsyncMock()), module)
        await registry_vcs_poller._poll_module(_db(), MagicMock(put=AsyncMock()), module)

    assert vcs.count("v0.1.0") == 1, f"the old tag was downloaded again: {vcs}"
    assert len(redis.keys) == 1


@patch(
    "terrapod.services.module_impact_service.trigger_linked_workspace_runs", new_callable=AsyncMock
)
async def test_without_redis_the_tag_is_still_skipped(_trigger, vcs):
    broken = MagicMock()
    broken.exists = AsyncMock(side_effect=ConnectionError("redis down"))
    broken.set = AsyncMock(side_effect=ConnectionError("redis down"))
    storage = MagicMock(put=AsyncMock())

    with patch(f"{_P}.get_redis_client", return_value=broken):
        await registry_vcs_poller._poll_module(_db(), storage, _module())

    assert list(_stored(storage)) == ["registry/modules/default/create/azurerm/1.0.0.tar.gz"]


@patch(
    "terrapod.services.module_impact_service.trigger_linked_workspace_runs", new_callable=AsyncMock
)
async def test_a_root_module_still_publishes_the_whole_repository(_trigger, vcs):
    redis = _FakeRedis()
    storage = MagicMock(put=AsyncMock())

    with patch(f"{_P}.get_redis_client", return_value=redis):
        await registry_vcs_poller._poll_module(_db(), storage, _module(subdirectory=""))

    stored = _stored(storage)
    assert len(stored) == 2, "both tags publish when there is no subdirectory"
    assert "modules/create/main.tf" in _members(
        stored["registry/modules/default/create/azurerm/1.0.0.tar.gz"]
    )
    assert redis.keys == {}
