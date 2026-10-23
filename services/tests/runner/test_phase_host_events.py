"""Delivering the callback and composing its environment (#1928).

The plugin itself is driven through real ansible in
`test_host_events_callback.py`. This file is the orchestrator's half: putting the
asset where the EE can load it, and building the one environment variable that
makes ansible load it WITHOUT replacing the operator's own configuration.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from terrapod.runner import ee_client
from terrapod.runner.phases import host_events

_PLUGIN_SRC = (
    pathlib.Path(host_events.__file__).resolve().parents[1]
    / "ansible_callback"
    / "terrapod_events.py"
)


@pytest.fixture(autouse=True)
def _callback_dir(tmp_path, monkeypatch):
    """Point the shared-volume directory at a writable path.

    Without it every test here fails `OSError: [Errno 30] Read-only file
    system: '/tp'`, which reads as a defect in the module rather than as the
    absence of a Job -- the same fixture the EE install dirs needed.
    """
    monkeypatch.setenv("TP_EE_CALLBACK_DIR", str(tmp_path / "callback"))
    return tmp_path / "callback"


def _cfg(**kw):
    cfg = MagicMock()
    cfg.config_directory = kw.get("config_directory", "ansible")
    return cfg


def _dump(path_entries: list[str], *, source: str = "/etc/ansible/ansible.cfg") -> bytes:
    rendered = ", ".join(f"'{p}'" for p in path_entries)
    return (
        b"CALLBACKS_ENABLED(default) = []\n"
        + f"DEFAULT_CALLBACK_PLUGIN_PATH({source}) = [{rendered}]\n".encode()
        + b"DEFAULT_STDOUT_CALLBACK(default) = default\n"
    )


class TestDeliveringThePlugin:
    def test_it_copies_the_asset_onto_the_shared_volume(self, _callback_dir):
        target = host_events.deliver()
        assert target == _callback_dir
        placed = _callback_dir / "terrapod_events.py"
        assert placed.is_file()
        assert placed.read_text() == _PLUGIN_SRC.read_text()

    def test_it_is_owner_only_because_the_ee_shares_our_uid(self, _callback_dir):
        host_events.deliver()
        mode = (_callback_dir / "terrapod_events.py").stat().st_mode & 0o777
        assert mode == 0o600, (
            "the EE runs as the same pinned uid (#1928), so it reads this as the "
            "owner and the group bit would grant more than the contract needs"
        )

    def test_a_missing_asset_degrades_rather_than_refusing(self):
        """A packaging defect of ours must not stop an operator's fleet."""
        with patch.object(host_events, "_ASSET_CANDIDATES", ("/nonexistent",)):
            assert host_events.deliver() is None

    def test_an_unwritable_directory_degrades_rather_than_refusing(self, tmp_path, monkeypatch):
        # A regular FILE as the parent: ENOTDIR is a type error the kernel makes
        # structurally, so no capability bypasses it and the test means the same
        # thing whatever uid runs it.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        monkeypatch.setenv("TP_EE_CALLBACK_DIR", str(blocker / "callback"))
        assert host_events.deliver() is None


class TestTheEnvironmentItComposes:
    def test_it_prepends_ours_to_the_path_ansible_reports(self, _callback_dir):
        their_dir = "/etc/ansible/callbacks"
        result = ee_client.EEResult(bin="ansible-config", rc=0, output=_dump([their_dir]))
        with patch.object(ee_client, "run", return_value=result):
            env = host_events.callback_env(_cfg(), cwd="/w", plugin_dir=_callback_dir)
        entries = env["ANSIBLE_CALLBACK_PLUGINS"].split(":")
        assert entries[0] == str(_callback_dir), "ours must be first"
        assert their_dir in entries, (
            "the operator's own callback path was dropped — measured, that silently "
            "disables every notification callback they run"
        )

    def test_it_never_writes_callbacks_enabled(self, _callback_dir):
        """Writing it would REPLACE their list, not extend it.

        The plugin carries `CALLBACK_NEEDS_ENABLED = False` precisely so this
        variable never has to be set.
        """
        result = ee_client.EEResult(bin="ansible-config", rc=0, output=_dump(["/theirs"]))
        with patch.object(ee_client, "run", return_value=result):
            env = host_events.callback_env(_cfg(), cwd="/w", plugin_dir=_callback_dir)
        assert "ANSIBLE_CALLBACKS_ENABLED" not in env

    def test_it_tells_the_plugin_where_to_write(self, _callback_dir):
        result = ee_client.EEResult(bin="ansible-config", rc=0, output=_dump(["/theirs"]))
        with patch.object(ee_client, "run", return_value=result):
            env = host_events.callback_env(_cfg(), cwd="/w", plugin_dir=_callback_dir)
        assert env["TP_HOST_EVENTS_PATH"] == str(host_events.events_path())

    def test_our_own_directory_is_not_duplicated(self, _callback_dir):
        result = ee_client.EEResult(
            bin="ansible-config", rc=0, output=_dump([str(_callback_dir), "/theirs"])
        )
        with patch.object(ee_client, "run", return_value=result):
            env = host_events.callback_env(_cfg(), cwd="/w", plugin_dir=_callback_dir)
        entries = env["ANSIBLE_CALLBACK_PLUGINS"].split(":")
        assert entries.count(str(_callback_dir)) == 1

    @pytest.mark.parametrize(
        "outcome",
        [
            pytest.param("unavailable", id="the agent could not be reached"),
            pytest.param("nonzero", id="ansible-config exited non-zero"),
            pytest.param("empty", id="it answered nothing"),
            pytest.param("no-path-line", id="it answered without a callback path"),
        ],
    )
    def test_it_falls_back_to_ansibles_default_rather_than_to_ours_alone(
        self, _callback_dir, outcome
    ):
        """Ours alone would capture AND break theirs; the default keeps both.

        Prepending to ansible's documented default still finds an operator's
        `~/.ansible/plugins/callback` plugins, so the common case survives a
        failed dump. Only a CUSTOM path is lost, which is why the module logs a
        warning naming that.
        """
        if outcome == "unavailable":
            side: dict = {"side_effect": ee_client.ExecutionEnvironmentUnavailable("no agent")}
        elif outcome == "nonzero":
            side = {"return_value": ee_client.EEResult(bin="x", rc=2, output=b"boom")}
        elif outcome == "empty":
            side = {"return_value": ee_client.EEResult(bin="x", rc=0, output=b"")}
        else:
            side = {
                "return_value": ee_client.EEResult(
                    bin="x", rc=0, output=b"DEFAULT_STDOUT_CALLBACK(default) = default\n"
                )
            }
        with patch.object(ee_client, "run", **side):
            env = host_events.callback_env(_cfg(), cwd="/w", plugin_dir=_callback_dir)
        entries = env["ANSIBLE_CALLBACK_PLUGINS"].split(":")
        assert entries[0] == str(_callback_dir)
        assert "~/.ansible/plugins/callback" in entries, (
            "a failed dump must not collapse to our directory alone — that is the "
            "silent-disable regression, taken on the one path least able to report it"
        )

    def test_the_dump_runs_with_no_forced_settings(self, _callback_dir):
        """Forcing anything would change the answer being read.

        The question is what the OPERATOR's configuration says, so the dump must
        not run under the environment we are about to impose.
        """
        result = ee_client.EEResult(bin="ansible-config", rc=0, output=_dump(["/theirs"]))
        with patch.object(ee_client, "run", return_value=result) as run:
            host_events.callback_env(_cfg(), cwd="/w", plugin_dir=_callback_dir)
        assert run.call_count == 1
        args, kwargs = run.call_args
        assert args[0] == "ansible-config"
        assert args[1] == ["dump"]
        assert kwargs["env"] == {}
        assert kwargs["capture"] is True, (
            "without capture the dump is streamed into the operator's run log and "
            "cannot be read back"
        )


class TestTheSidecarIsOnePerRun:
    def test_the_two_paths_sit_in_the_shared_directory(self, _callback_dir):
        assert host_events.events_path().parent == _callback_dir
        assert host_events.large_path().parent == _callback_dir
        assert host_events.events_path() != host_events.large_path()

    def test_neither_path_is_per_host(self):
        """The retention sweep deletes a FIXED key list per run.

        An object per host -- the shape the issue body proposed -- would need
        prefix enumeration the sweep does not have, so every configure would
        leak objects for the life of the deployment.
        """
        for path in (host_events.events_path(), host_events.large_path()):
            assert "{host" not in str(path) and "%s" not in str(path)


class TestThePluginStaysShippableIntoAnyExecutionEnvironment:
    def test_it_imports_only_the_standard_library_and_ansible(self):
        """It runs under the EE's interpreter, which has no Terrapod at all.

        Ten published EE lines span Python 3.10 to 3.14 (#2154), and an
        operator's own image is theirs. So an import of ours here is a
        `ModuleNotFoundError` inside somebody else's container, discovered when
        the configure has already started.
        """
        tree = ast.parse(_PLUGIN_SRC.read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        assert "terrapod" not in imported, (
            f"the callback plugin imports Terrapod ({sorted(imported)}) — it runs "
            "inside the execution environment, which ships none of it"
        )
        assert imported <= {"json", "os", "time", "ansible", "__future__"}, sorted(imported)

    def test_nothing_in_terrapod_imports_it(self):
        """It is a data asset, not a module of ours.

        It imports `ansible`, which the orchestrator does not have, so an import
        anywhere in the runner would crash the Job -- and the runner-image gates
        are keyed on its COPY destination being outside `terrapod/`, which only
        holds while nothing treats it as a module.
        """
        root = pathlib.Path(host_events.__file__).resolve().parents[2]
        offenders = []
        for source in root.rglob("*.py"):
            if source == _PLUGIN_SRC or "ansible_callback" in source.parts:
                continue
            text = source.read_text(errors="replace")
            if "ansible_callback" in text and "import" in text:
                for node in ast.walk(ast.parse(text)):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        names = [node.module]
                    if any("ansible_callback" in n for n in names):
                        offenders.append(str(source.relative_to(root)))
        assert not offenders, f"these import the plugin as a module: {offenders}"
