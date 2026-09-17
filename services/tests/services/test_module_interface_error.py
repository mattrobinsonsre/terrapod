"""Every writer of a module version's interface records why it failed (#1707).

A failed parse used to store `inputs: []`, which reads exactly like a module that
declares no variables — and the service catalog builds its provision form from
those inputs, so the item offered nothing and no error surfaced anywhere. Each
writer now sets `interface_error` on failure and clears it on success.
"""

import io
import tarfile
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import settings
from terrapod.services import registry_module_service, registry_vcs_poller

_P = "terrapod.services.registry_vcs_poller"
_TRIGGER = "terrapod.services.module_impact_service.trigger_linked_workspace_runs"


def _tarball(files: dict[str, str], wrapper: str = "") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{wrapper}{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


GOOD = {"variables.tf": 'variable "region" {}\n'}
BROKEN = {"variables.tf": 'variable "region" {\n  type = \n}\n'}


@pytest.fixture(autouse=True)
def _interface_enabled(monkeypatch):
    monkeypatch.setattr(settings.registry.module_interface, "enabled", True)


# --- Direct upload ----------------------------------------------------------


class TestDirectUpload:
    async def _upload(self, tmp_path, content: bytes, version):
        path = tmp_path / "m.tar.gz"
        path.write_bytes(content)
        module = SimpleNamespace(id=uuid.uuid4(), status="pending")
        db = AsyncMock()
        existing = MagicMock()
        existing.scalars.return_value.first.return_value = version
        db.execute = AsyncMock(return_value=existing)
        storage = MagicMock(put_stream=AsyncMock())
        with (
            patch.object(registry_module_service, "get_module", AsyncMock(return_value=module)),
            patch.object(
                registry_module_service,
                "upsert_module_version",
                AsyncMock(return_value=version),
            ),
            patch(_TRIGGER, new_callable=AsyncMock),
        ):
            return await registry_module_service.upload_module_tarball(
                db, storage, "default", "vpc", "aws", "1.0.0", str(path)
            )

    async def test_an_unparseable_file_sets_the_reason(self, tmp_path):
        version = SimpleNamespace(inputs=None, outputs=None, interface_error=None)
        await self._upload(tmp_path, _tarball(BROKEN), version)
        assert version.inputs == []
        assert version.interface_error == "variables.tf: invalid HCL at line 2, column 10"

    async def test_a_corrupt_archive_sets_the_reason(self, tmp_path):
        version = SimpleNamespace(inputs=None, outputs=None, interface_error=None)
        await self._upload(tmp_path, b"definitely not a tarball", version)
        assert version.interface_error == (
            "The module archive could not be read as a gzip-compressed tar file."
        )

    async def test_a_good_re_upload_clears_it(self, tmp_path):
        version = SimpleNamespace(
            inputs=[], outputs=[], interface_error="variables.tf: invalid HCL"
        )
        await self._upload(tmp_path, _tarball(GOOD), version)
        assert [i["name"] for i in version.inputs] == ["region"]
        assert version.interface_error is None

    async def test_an_unexpected_parser_crash_is_still_recorded(self, tmp_path):
        version = SimpleNamespace(inputs=None, outputs=None, interface_error=None)
        with patch(
            "terrapod.services.module_hcl_parser.extract_module_interface_result_from_file",
            side_effect=RuntimeError("boom at /var/lib/secret"),
        ):
            await self._upload(tmp_path, _tarball(GOOD), version)
        assert version.interface_error == "The module interface could not be read."


# --- VCS registry poller ------------------------------------------------------


def _module(versions=()):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="vpc",
        namespace="default",
        provider="aws",
        vcs_connection_id=uuid.uuid4(),
        vcs_repo_url="https://github.com/org/repo",
        vcs_tag_pattern="v*",
        vcs_last_tag="",
        versions=list(versions),
        status="pending",
        subdirectory="modules/vpc",
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


@pytest.fixture
def serve(monkeypatch):
    """Serve one tag, v1.0.0 at `sha-new`, whose archive the test chooses."""
    archive: dict[str, bytes] = {}

    async def download(conn, owner, repo, tag):  # noqa: ARG001
        return archive["v1.0.0"]

    monkeypatch.setattr(
        registry_vcs_poller, "_dispatch_parse_repo_url", lambda _p: lambda _u: ("org", "repo")
    )
    monkeypatch.setattr(
        registry_vcs_poller,
        "_dispatch_list_tags",
        lambda _p: AsyncMock(return_value=[{"name": "v1.0.0", "sha": "sha-new"}]),
    )
    monkeypatch.setattr(registry_vcs_poller, "_dispatch_download_archive", lambda _p: download)

    def _set(files: dict[str, str]) -> None:
        # A VCS archive wraps the repository in one directory; the module lives
        # in a subdirectory, which the poller re-roots before parsing.
        archive["v1.0.0"] = _tarball(files, wrapper="org-repo-abc/modules/vpc/")

    return _set


async def _poll(module, db):
    storage = MagicMock(put=AsyncMock())
    with (
        patch(f"{_P}.get_redis_client", return_value=None),
        patch(_TRIGGER, new_callable=AsyncMock),
    ):
        await registry_vcs_poller._poll_module(db, storage, module)


class TestVcsPoller:
    async def test_a_new_version_records_the_reason(self, serve):
        serve(BROKEN)
        db = _db()
        await _poll(_module(), db)
        [version] = [c.args[0] for c in db.add.call_args_list]
        assert version.inputs == []
        assert version.interface_error == "variables.tf: invalid HCL at line 2, column 10"

    async def test_a_new_version_that_parses_records_none(self, serve):
        serve(GOOD)
        db = _db()
        await _poll(_module(), db)
        [version] = [c.args[0] for c in db.add.call_args_list]
        assert [i["name"] for i in version.inputs] == ["region"]
        assert version.interface_error is None

    async def test_a_moved_tag_that_now_parses_clears_it(self, serve):
        serve(GOOD)
        existing = SimpleNamespace(
            version="1.0.0",
            vcs_commit_sha="sha-old",
            vcs_tag="v1.0.0",
            inputs=[],
            outputs=[],
            interface_error="variables.tf: invalid HCL",
        )
        await _poll(_module([existing]), _db())
        assert existing.vcs_commit_sha == "sha-new"
        assert [i["name"] for i in existing.inputs] == ["region"]
        assert existing.interface_error is None

    async def test_a_moved_tag_that_no_longer_parses_sets_it(self, serve):
        serve(BROKEN)
        existing = SimpleNamespace(
            version="1.0.0",
            vcs_commit_sha="sha-old",
            vcs_tag="v1.0.0",
            inputs=[{"name": "region"}],
            outputs=[],
            interface_error=None,
        )
        await _poll(_module([existing]), _db())
        assert existing.inputs == []
        assert existing.interface_error == "variables.tf: invalid HCL at line 2, column 10"
