"""A configuration archive that cannot be written out fails the run (#1635).

`_safe_extract` used to swallow every OSError per member, so running out of
space (ENOSPC) or an I/O error (EIO) left a partly written tree that the run
accepted, and the run then failed later with an error that did not name the
cause. Only the cosmetic attribute failures — setting a file's mode, mtime or
owner as a non-root user — are tolerated now.
"""

from __future__ import annotations

import errno
import io
import os
import tarfile

import httpx
import pytest

from terrapod.runner.phases import configuration as cfg_phase
from terrapod.runner.runner_config import RunnerConfig

_FILES = [f"terraform/app/f{i}.tf" for i in range(5)]


def _tar_gz() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in _FILES:
            data = b'resource "null_resource" "x" {}\n'
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 1_700_000_000
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _cfg() -> RunnerConfig:
    return RunnerConfig.from_env(
        env={
            "TP_API_URL": "https://api.example.com",
            "TP_AUTH_TOKEN": "tok",
            "TP_RUN_ID": "run-1",
            "TP_BACKEND": "tofu",
            "TP_VERSION": "1.12.1",
            "TP_DOWNLOAD_RETRY_DELAY": "0",
            "TP_WORKING_DIR": "terraform/app",
        }
    )


def _client() -> tuple[httpx.Client, dict[str, int]]:
    calls = {"n": 0}
    body = _tar_gz()

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        calls["n"] += 1
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def _fail_writing(monkeypatch, err: int, on_member: str) -> None:
    """Make writing one member's data fail with `err`, as a full or failing
    disk would."""
    real = tarfile.TarFile.makefile

    def makefile(self, tarinfo, targetpath):
        if tarinfo.name == on_member:
            raise OSError(err, os.strerror(err), targetpath)
        return real(self, tarinfo, targetpath)

    monkeypatch.setattr(tarfile.TarFile, "makefile", makefile)


@pytest.mark.parametrize("err", [errno.ENOSPC, errno.EIO, errno.EDQUOT, errno.EROFS])
def test_a_failure_to_write_data_fails_the_run(tmp_path, monkeypatch, err):
    _fail_writing(monkeypatch, err, _FILES[2])
    client, calls = _client()

    with pytest.raises(cfg_phase.ConfigurationArchiveError) as exc:
        cfg_phase.download_configuration(_cfg(), work_dir=tmp_path / "ws", client=client)

    msg = str(exc.value)
    assert repr(_FILES[2]) in msg, "the message names the member that was not written"
    assert os.strerror(err) in msg
    assert "partly extracted" in msg
    # The archive itself is fine, so it is not called unreadable and not
    # downloaded again.
    assert "not a readable tar.gz" not in msg
    assert calls["n"] == 1
    assert isinstance(exc.value.__cause__, OSError)
    assert exc.value.__cause__.errno == err


def test_the_download_is_removed_when_writing_fails(tmp_path, monkeypatch):
    seen = []
    real = cfg_phase.download_to_file

    def recording(url, output_path, *args, **kwargs):
        seen.append(output_path)
        return real(url, output_path, *args, **kwargs)

    monkeypatch.setattr(cfg_phase, "download_to_file", recording)
    _fail_writing(monkeypatch, errno.ENOSPC, _FILES[0])
    client, _ = _client()

    with pytest.raises(cfg_phase.ConfigurationArchiveError):
        cfg_phase.download_configuration(_cfg(), work_dir=tmp_path / "ws", client=client)

    assert seen
    assert not any(p.exists() for p in seen), "the downloaded archive was left behind"


@pytest.mark.parametrize("setter", ["utime", "chmod"])
def test_attribute_failures_from_tarfile_are_tolerated(tmp_path, monkeypatch, setter):
    # A non-root runner may not be allowed to set a mode or mtime. tarfile
    # reports that as a non-fatal ExtractError; the files are all written.
    def denied(*args, **kwargs):
        raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(tarfile.os, setter, denied)
    client, _ = _client()

    result = cfg_phase.download_configuration(_cfg(), work_dir=tmp_path / "ws", client=client)

    assert result.downloaded
    for name in _FILES:
        assert (tmp_path / "ws" / name).is_file()


@pytest.mark.parametrize("err", [errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP])
def test_attribute_errnos_raised_after_the_data_is_written_are_tolerated(
    tmp_path, monkeypatch, err
):
    # The same failure surfacing as an OSError from extract itself, after the
    # member's data has landed.
    real = tarfile.TarFile.extract

    def extract(self, member, path="", set_attrs=True, **kwargs):
        real(self, member, path, set_attrs=set_attrs, **kwargs)
        raise OSError(err, os.strerror(err))

    monkeypatch.setattr(tarfile.TarFile, "extract", extract)
    client, _ = _client()

    result = cfg_phase.download_configuration(_cfg(), work_dir=tmp_path / "ws", client=client)

    assert result.downloaded
    for name in _FILES:
        assert (tmp_path / "ws" / name).is_file()


def test_a_member_that_cannot_be_created_fails_the_run(tmp_path, monkeypatch):
    # EACCES opening a file for writing is a failure to write, not a
    # cosmetic attribute error.
    _fail_writing(monkeypatch, errno.EACCES, _FILES[1])
    client, _ = _client()

    with pytest.raises(cfg_phase.ConfigurationArchiveError) as exc:
        cfg_phase.download_configuration(_cfg(), work_dir=tmp_path / "ws", client=client)

    assert repr(_FILES[1]) in str(exc.value)
