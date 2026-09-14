"""An unreadable configuration archive fails the run, naming the archive (#1600).

Before, a download that was not a readable tar.gz was logged as a warning and
the run carried on — so a workspace with a working directory then failed with
"working directory '…' not found in config", and a truncated gzip crashed the
orchestrator with a bare EOFError. Neither named the archive.
"""

from __future__ import annotations

import io
import os
import tarfile

import httpx
import pytest

from terrapod.runner.phases import configuration as cfg_phase
from terrapod.runner.runner_config import RunnerConfig


def _tar_gz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _cfg(**overrides) -> RunnerConfig:
    base = {
        "TP_API_URL": "https://api.example.com",
        "TP_AUTH_TOKEN": "tok",
        "TP_RUN_ID": "run-1",
        "TP_BACKEND": "tofu",
        "TP_VERSION": "1.12.1",
        "TP_DOWNLOAD_RETRY_DELAY": "0",
        "TP_WORKING_DIR": "terraform/app",
    }
    base.update(overrides)
    return RunnerConfig.from_env(env=base)


def _serving(*bodies: bytes) -> tuple[httpx.Client, dict[str, int]]:
    """A client that serves the bodies in turn (the last one repeats) and
    counts the downloads."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        body = bodies[min(calls["n"], len(bodies) - 1)]
        calls["n"] += 1
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def _fails(tmp_path, body: bytes, **cfg) -> tuple[str, int]:
    client, calls = _serving(body)
    with pytest.raises(cfg_phase.ConfigurationArchiveError) as exc:
        cfg_phase.download_configuration(_cfg(**cfg), work_dir=tmp_path / "ws", client=client)
    return str(exc.value), calls["n"]


def _real_archive() -> bytes:
    # Incompressible content, so half the archive is half the files.
    return _tar_gz({f"terraform/app/f{i}.tf": os.urandom(4000) for i in range(10)})


def test_a_body_that_is_not_gzip(tmp_path):
    msg, n = _fails(tmp_path, b"<html>Service Unavailable</html>")
    assert "run-1" in msg and "not a readable tar.gz" in msg
    # Size and leading bytes say what was actually stored.
    assert "32 bytes" in msg and "3c 68 74 6d" in msg
    assert "working directory" not in msg
    assert n == 3, "downloaded again before giving up"


def test_a_truncated_gzip(tmp_path):
    whole = _real_archive()
    msg, _ = _fails(tmp_path, whole[: len(whole) // 2])
    assert "not a readable tar.gz" in msg
    assert "starting 1f 8b" in msg, "a truncated gzip still starts with the gzip magic"


def test_an_empty_200(tmp_path):
    msg, _ = _fails(tmp_path, b"")
    assert "the download was empty" in msg


def test_the_message_says_retrying_the_run_will_not_help(tmp_path):
    msg, _ = _fails(tmp_path, b"not an archive")
    assert "retrying this run downloads it again" in msg
    assert "queue a new run" in msg


def test_a_bad_download_then_a_good_one_succeeds(tmp_path):
    good = _real_archive()
    client, calls = _serving(good[: len(good) // 2], good)

    result = cfg_phase.download_configuration(_cfg(), work_dir=tmp_path / "ws", client=client)

    assert result.downloaded
    assert (tmp_path / "ws" / "terraform" / "app" / "f0.tf").exists()
    assert calls["n"] == 2


def test_the_number_of_downloads_follows_the_retry_setting(tmp_path):
    _, n = _fails(tmp_path, b"junk", TP_DOWNLOAD_RETRIES="1")
    assert n == 1


def _recording_downloads(monkeypatch) -> list:
    """Record the path each download is written to."""
    seen = []
    real = cfg_phase.download_to_file

    def recording(url, output_path, *args, **kwargs):
        seen.append(output_path)
        return real(url, output_path, *args, **kwargs)

    monkeypatch.setattr(cfg_phase, "download_to_file", recording)
    return seen


def test_each_download_has_its_own_file_and_it_is_removed(tmp_path, monkeypatch):
    # #1609: every download used to go to one fixed /tmp path, so parallel
    # test workers overwrote and deleted each other's archives.
    seen = _recording_downloads(monkeypatch)
    good = _real_archive()
    for i in range(2):
        client, _ = _serving(good)
        cfg_phase.download_configuration(_cfg(), work_dir=tmp_path / f"ws{i}", client=client)

    assert len(seen) == 2
    assert seen[0] != seen[1], "two downloads shared a path"
    assert not any(p.exists() for p in seen), "a downloaded archive was left behind"


def test_the_download_is_removed_when_the_archive_is_unreadable(tmp_path, monkeypatch):
    seen = _recording_downloads(monkeypatch)
    _fails(tmp_path, b"not an archive")

    assert seen
    assert not any(p.exists() for p in seen), "the unreadable archive was left behind"
