"""An agent-mode Pulumi run keeps its stack in the Job, as Terraform keeps its state (#1576).

The runner creates the stack in a file backend with a passphrase of its own,
imports the deployment the API hands it — secrets in plaintext — runs, and after
an update exports the stack and hands it back once. It never uses Terrapod as a
live Pulumi backend.

The CLI facts these helpers encode were measured on v3.262.0 (the spike recorded
in #1576): importing without a provider block crashes the CLI; importing under
the salt the stack last used fails "incorrect passphrase"; a two-part
`project/stack` is refused by the file backend; and a saved plan opens only under
the stack key it was sealed with.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from terrapod.runner import exec_subprocess, job_entrypoint
from terrapod.runner.phases import pulumi_exec, uploads
from terrapod.runner.phases import state as state_phase
from terrapod.runner.runner_config import RunnerConfig

SALT = "v1:c2FsdA==:v1:bm9uY2U=:Y2lwaGVy"
DEPLOYMENT = {
    "manifest": {"time": "t1"},
    "resources": [{"urn": "urn:a", "outputs": {"pw": {"plaintext": '"x"'}}}],
}


def _cfg(**over) -> RunnerConfig:
    cfg = RunnerConfig.from_env(
        env={
            "TP_API_URL": "https://terrapod.test",
            "TP_AUTH_TOKEN": "runtok:abc",
            "TP_RUN_ID": "run-1",
            "TP_BACKEND": "tofu",
            "TP_VERSION": "1.12.1",
        }
    )
    return dataclasses.replace(cfg, **over)


class TestNames:
    @pytest.mark.parametrize(
        "given,want",
        [
            ("default/proj/dev", "organization/proj/dev"),
            ("dev", "dev"),
            ("", ""),
        ],
    )
    def test_the_stack_reference(self, given, want) -> None:
        assert pulumi_exec.local_stack_ref(given) == want


class TestTheCommittedStackFile:
    def test_the_provider_lines_are_dropped_and_the_rest_kept(self, tmp_path) -> None:
        f = tmp_path / "Pulumi.dev.yaml"
        f.write_text(
            "secretsprovider: awskms://alias/x\n"
            "encryptionsalt: v1:old\n"
            "encryptedkey: abc\n"
            "config:\n  proj:region: eu-west-1\n"
        )
        pulumi_exec.reset_secrets_config(f)
        assert f.read_text() == "config:\n  proj:region: eu-west-1\n"

    def test_a_salt_can_be_pinned(self, tmp_path) -> None:
        f = tmp_path / "Pulumi.dev.yaml"
        f.write_text("encryptionsalt: v1:old\nconfig: {}\n")
        pulumi_exec.reset_secrets_config(f, SALT)
        assert f.read_text() == f"encryptionsalt: {SALT}\nconfig: {{}}\n"
        assert pulumi_exec.read_salt(f) == SALT

    def test_no_file_is_invented_without_a_salt(self, tmp_path) -> None:
        f = tmp_path / "Pulumi.dev.yaml"
        pulumi_exec.reset_secrets_config(f)
        assert not f.exists()

    def test_a_pinned_salt_creates_the_file(self, tmp_path) -> None:
        f = tmp_path / "Pulumi.dev.yaml"
        pulumi_exec.reset_secrets_config(f, SALT)
        assert pulumi_exec.read_salt(f) == SALT

    @pytest.mark.parametrize(
        "body,secure",
        [
            ("config:\n  proj:pw:\n    secure: v1:abc\n", True),
            ("config:\n  proj:pw: {secure: v1:abc}\n", True),
            ("config:\n  # secure: v1:abc\n  proj:region: x\n", False),
            ("config:\n  proj:insecure: yes\n", False),
        ],
    )
    def test_secure_values_are_noticed(self, tmp_path, body, secure) -> None:
        f = tmp_path / "Pulumi.dev.yaml"
        f.write_text(body)
        assert pulumi_exec.has_secure_config(f) is secure

    def test_a_quoted_salt_reads_the_same(self, tmp_path) -> None:
        f = tmp_path / "Pulumi.dev.yaml"
        f.write_text(f'encryptionsalt: "{SALT}"\n')
        assert pulumi_exec.read_salt(f) == SALT


class TestTheImport:
    def test_the_seed_names_this_stacks_salt(self) -> None:
        doc = pulumi_exec.seed_document(DEPLOYMENT, SALT)
        assert doc["version"] == 3
        assert doc["deployment"]["secrets_providers"] == {
            "type": "passphrase",
            "state": {"salt": SALT},
        }
        assert "secrets_providers" not in DEPLOYMENT


class TestChangeDetection:
    def test_a_new_manifest_is_not_a_change(self) -> None:
        after = {**DEPLOYMENT, "manifest": {"time": "t2"}, "secrets_providers": {"type": "x"}}
        assert not pulumi_exec.deployment_changed(DEPLOYMENT, after)

    def test_an_empty_new_stack_is_not_a_change(self) -> None:
        assert not pulumi_exec.deployment_changed(None, {"manifest": {}, "resources": []})

    def test_a_resource_change_is(self) -> None:
        after = {**DEPLOYMENT, "resources": []}
        assert pulumi_exec.deployment_changed(DEPLOYMENT, after)


class TestThePlanCarriesItsKey:
    def test_round_trip(self, tmp_path) -> None:
        plan = tmp_path / "plan.json"
        plan.write_text('{"resourcePlans": {}}')
        keys = pulumi_exec.StackKeys(SALT, "pw")
        pulumi_exec.bundle_plan(plan, keys)
        assert "resourcePlans" not in json.loads(plan.read_text())
        assert pulumi_exec.unbundle_plan(plan) == keys
        assert plan.read_text() == '{"resourcePlans": {}}'

    def test_a_plan_from_before_bundling_is_left_as_it_is(self, tmp_path) -> None:
        plan = tmp_path / "plan.json"
        plan.write_text('{"resourcePlans": {}}')
        assert pulumi_exec.unbundle_plan(plan) is None
        assert plan.read_text() == '{"resourcePlans": {}}'


class _FakePulumi:
    """Stands in for the CLI: `stack init` records a salt, `import` and
    `export` read and write the files they are given."""

    def __init__(self, export: dict | None = None, fail: str = "") -> None:
        self.calls: list[list[str]] = []
        self.imported: dict | None = None
        self.import_passphrase = ""
        self.export = export
        self.fail = fail

    def __call__(self, argv, **kwargs):
        args = argv[1:]
        self.calls.append(args)
        verb = " ".join(args[:2])
        if verb == self.fail:
            return MagicMock(exit_code=255)
        if verb == "stack init":
            name = args[2].rsplit("/", 1)[-1]
            f = Path.cwd() / f"Pulumi.{name}.yaml"
            body = f.read_text() if f.exists() else ""
            if "encryptionsalt" not in body:
                f.write_text(f"encryptionsalt: {SALT}\n{body}")
        elif verb == "stack import":
            self.imported = json.loads(Path(args[args.index("--file") + 1]).read_text())
            self.import_passphrase = os.environ["PULUMI_CONFIG_PASSPHRASE"]
        elif verb == "stack export":
            Path(args[args.index("--file") + 1]).write_text(json.dumps(self.export))
        return MagicMock(exit_code=0)


@pytest.fixture
def job(monkeypatch, tmp_path):
    """A program directory and a state directory, and an environment restored
    afterwards — `prepare_local_stack` writes to os.environ directly."""
    program = tmp_path / "program"
    program.mkdir()
    monkeypatch.chdir(program)
    monkeypatch.setenv("TP_PULUMI_STACK", "default/proj/dev")
    monkeypatch.setenv("TP_PULUMI_STATE_DIR", str(tmp_path / "state"))
    for var in ("PULUMI_BACKEND_URL", "PULUMI_CONFIG_PASSPHRASE", "PULUMI_CONFIG_PASSPHRASE_FILE"):
        monkeypatch.setenv(var, "operator-set")
    return tmp_path


def _download(monkeypatch, serial=3, deployment=DEPLOYMENT):
    monkeypatch.setattr(state_phase, "download_pulumi_deployment", lambda cfg: (serial, deployment))


class TestPreparingTheStack:
    def test_the_stack_is_made_here_and_seeded(self, monkeypatch, job) -> None:
        fake = _FakePulumi()
        monkeypatch.setattr(exec_subprocess, "run", fake)
        _download(monkeypatch)

        stack = pulumi_exec.prepare_local_stack(_cfg(), "/bin/pulumi")

        assert fake.calls[0][:3] == ["stack", "init", "organization/proj/dev"]
        assert "passphrase" in fake.calls[0]
        assert os.environ["PULUMI_BACKEND_URL"] == (job / "state").resolve().as_uri()
        assert "PULUMI_CONFIG_PASSPHRASE_FILE" not in os.environ
        assert fake.import_passphrase == stack.keys.passphrase != "operator-set"
        assert fake.imported["deployment"]["secrets_providers"]["state"]["salt"] == SALT
        assert (stack.base_serial, stack.deployment) == (3, DEPLOYMENT)
        # The plaintext seed does not outlive the import.
        assert not (job / "state" / "import.json").exists()

    def test_a_new_stack_imports_nothing(self, monkeypatch, job) -> None:
        fake = _FakePulumi()
        monkeypatch.setattr(exec_subprocess, "run", fake)
        _download(monkeypatch, serial=0, deployment=None)

        stack = pulumi_exec.prepare_local_stack(_cfg(), "/bin/pulumi")
        assert [c[:2] for c in fake.calls] == [["stack", "init"]]
        assert stack.base_serial == 0

    def test_a_committed_provider_is_dropped_before_init(self, monkeypatch, job) -> None:
        (job / "program" / "Pulumi.dev.yaml").write_text(
            "secretsprovider: awskms://alias/x\nencryptionsalt: v1:theirs\n"
        )
        monkeypatch.setattr(exec_subprocess, "run", _FakePulumi())
        _download(monkeypatch)

        stack = pulumi_exec.prepare_local_stack(_cfg(), "/bin/pulumi")
        body = (job / "program" / "Pulumi.dev.yaml").read_text()
        assert "secretsprovider" not in body and "v1:theirs" not in body
        assert stack.keys.salt == SALT

    def test_a_bound_update_reuses_the_previews_key(self, monkeypatch, job) -> None:
        fake = _FakePulumi()
        monkeypatch.setattr(exec_subprocess, "run", fake)
        _download(monkeypatch)
        keys = pulumi_exec.StackKeys("v1:preview-salt", "preview-pw")

        stack = pulumi_exec.prepare_local_stack(_cfg(), "/bin/pulumi", keys=keys)
        assert stack.keys == keys
        assert fake.import_passphrase == "preview-pw"

    def test_secure_config_fails_early_and_says_why(self, monkeypatch, job) -> None:
        (job / "program" / "Pulumi.dev.yaml").write_text("config:\n  proj:pw:\n    secure: v1:x\n")
        fake = _FakePulumi()
        monkeypatch.setattr(exec_subprocess, "run", fake)
        with pytest.raises(pulumi_exec.LocalStackError, match="secure"):
            pulumi_exec.prepare_local_stack(_cfg(), "/bin/pulumi")
        assert fake.calls == []

    def test_a_failed_download_is_fatal(self, monkeypatch, job) -> None:
        monkeypatch.setattr(exec_subprocess, "run", _FakePulumi())

        def boom(cfg):
            raise state_phase.StateDownloadError("HTTP 500")

        monkeypatch.setattr(state_phase, "download_pulumi_deployment", boom)
        with pytest.raises(pulumi_exec.LocalStackError, match="500"):
            pulumi_exec.prepare_local_stack(_cfg(), "/bin/pulumi")

    @pytest.mark.parametrize("verb", ["stack init", "stack import"])
    def test_a_failed_cli_step_is_fatal(self, monkeypatch, job, verb) -> None:
        monkeypatch.setattr(exec_subprocess, "run", _FakePulumi(fail=verb))
        _download(monkeypatch)
        with pytest.raises(pulumi_exec.LocalStackError):
            pulumi_exec.prepare_local_stack(_cfg(), "/bin/pulumi")
        assert not (job / "state" / "import.json").exists()


def _stack(job: Path, deployment=DEPLOYMENT) -> pulumi_exec.LocalStack:
    state = job / "state"
    state.mkdir(exist_ok=True)
    return pulumi_exec.LocalStack(
        ref="organization/proj/dev",
        config_path=job / "Pulumi.dev.yaml",
        state_dir=state,
        keys=pulumi_exec.StackKeys(SALT, "pw"),
        base_serial=3,
        deployment=deployment,
    )


class TestHandingItBack:
    def _hand_back(self, monkeypatch, job, *, export, upload_ok=True, rc=0):
        fake = _FakePulumi(export=export)
        monkeypatch.setattr(exec_subprocess, "run", fake)
        seen: dict = {"uploads": [], "diverged": 0}

        def fake_upload(cfg, path, *, base_serial):
            seen["uploads"].append((json.loads(path.read_text()), base_serial))
            return upload_ok

        monkeypatch.setattr(uploads, "upload_pulumi_deployment", fake_upload)
        monkeypatch.setattr(
            uploads,
            "signal_state_diverged",
            lambda cfg: seen.__setitem__("diverged", seen["diverged"] + 1),
        )
        seen["rc"] = job_entrypoint._hand_back_pulumi_state(
            _cfg(), "/bin/pulumi", _stack(job), exit_code=rc, child_grace=1.0
        )
        seen["calls"] = fake.calls
        return seen

    def test_a_changed_stack_goes_back_once_with_its_base_serial(self, monkeypatch, job) -> None:
        after = {"version": 3, "deployment": {**DEPLOYMENT, "resources": []}}
        seen = self._hand_back(monkeypatch, job, export=after)
        assert seen["uploads"] == [(after, 3)]
        assert seen["rc"] == 0
        assert "--show-secrets" in seen["calls"][0]
        # The plaintext export does not outlive the upload.
        assert not (job / "state" / "export.json").exists()

    def test_an_unchanged_stack_goes_back_not_at_all(self, monkeypatch, job) -> None:
        after = {"version": 3, "deployment": {**DEPLOYMENT, "manifest": {"time": "t9"}}}
        seen = self._hand_back(monkeypatch, job, export=after)
        assert seen["uploads"] == []
        assert seen["rc"] == 0

    def test_a_failed_update_still_hands_back_what_it_did(self, monkeypatch, job) -> None:
        """A failed `up` can have created resources; dropping them would orphan them."""
        after = {"version": 3, "deployment": {**DEPLOYMENT, "resources": [{"urn": "urn:b"}]}}
        seen = self._hand_back(monkeypatch, job, export=after, rc=255)
        assert len(seen["uploads"]) == 1
        assert seen["rc"] == 255

    def test_a_failed_upload_is_fatal_and_flags_divergence(self, monkeypatch, job) -> None:
        after = {"version": 3, "deployment": {**DEPLOYMENT, "resources": []}}
        seen = self._hand_back(monkeypatch, job, export=after, upload_ok=False)
        assert seen["diverged"] == 1
        assert seen["rc"] == 1

    def test_a_stack_that_cannot_be_read_back_is_fatal(self, monkeypatch, job) -> None:
        fake = _FakePulumi(fail="stack export")
        monkeypatch.setattr(exec_subprocess, "run", fake)
        flagged = []
        monkeypatch.setattr(uploads, "signal_state_diverged", lambda cfg: flagged.append(1))
        rc = job_entrypoint._hand_back_pulumi_state(
            _cfg(), "/bin/pulumi", _stack(job), exit_code=0, child_grace=1.0
        )
        assert (rc, flagged) == (1, [1])


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestTheWire:
    def test_the_download_reads_the_serial_header(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/api/terrapod/v1/runs/run-1/artifacts/pulumi-deployment"
            return httpx.Response(
                200,
                json={"version": 3, "deployment": DEPLOYMENT},
                headers={"X-Terrapod-State-Serial": "9"},
            )

        serial, deployment = state_phase.download_pulumi_deployment(_cfg(), client=_client(handler))
        assert (serial, deployment) == (9, DEPLOYMENT)

    def test_an_empty_stack_is_none(self) -> None:
        handler = lambda req: httpx.Response(200, json={"version": 3, "deployment": None})  # noqa: E731
        assert state_phase.download_pulumi_deployment(_cfg(), client=_client(handler)) == (0, None)

    def test_anything_but_200_fails_closed(self) -> None:
        """A 409 here is a stack Terrapod cannot open; guessing "no state" would
        propose creating everything that already exists."""
        handler = lambda req: httpx.Response(409, json={"detail": "awskms"})  # noqa: E731
        with pytest.raises(state_phase.StateDownloadError, match="409"):
            state_phase.download_pulumi_deployment(_cfg(), client=_client(handler))

    def test_the_upload_quotes_its_base_serial(self, tmp_path) -> None:
        seen = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen.update(method=req.method, url=str(req.url), ctype=req.headers["content-type"])
            return httpx.Response(204)

        f = tmp_path / "export.json"
        f.write_text('{"version": 3, "deployment": {}}')
        assert uploads.upload_pulumi_deployment(_cfg(), f, base_serial=4, client=_client(handler))
        assert seen["method"] == "PUT"
        assert seen["url"].endswith("/artifacts/pulumi-deployment?base-serial=4")
        assert seen["ctype"] == "application/json"

    def test_a_refused_upload_reports_failure(self, tmp_path) -> None:
        handler = lambda req: httpx.Response(409)  # noqa: E731
        f = tmp_path / "export.json"
        f.write_text("{}")
        assert not uploads.upload_pulumi_deployment(
            _cfg(), f, base_serial=4, client=_client(handler)
        )
