"""The workspace's own sensitive values are registered with the log redactor (#2722).

`exec_subprocess.run` has always wrapped plan and apply output in a
`StreamRedactor`, so the redactor was never the gap. Its secret set was: of the
four `register_secret` call sites, three were in `ansible_auth` and ran only on a
configure, and the fourth was the cloud identity token -- so #1934 made redaction
the platform's job for Ansible and nothing generalised it. A workspace variable
marked sensitive was delivered to a provider and scrubbed from nothing.

**Both directions are load-bearing here, and the second is the one with teeth.**
Registering too little leaves a secret in a log that `run:read` can fetch while
`var:read` masks the same value. Registering too much silently corrupts the log --
a region name or a username replaced by a mask -- and that failure is found by a
confused operator rather than by CI, which makes it the worse of the two.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from terrapod.runner import exec_subprocess, job_entrypoint

_RUNNER = pathlib.Path(exec_subprocess.__file__).parent
_ENTRYPOINT = pathlib.Path(job_entrypoint.__file__)


@pytest.fixture(autouse=True)
def _clean_registry():
    exec_subprocess._reset_secrets_for_tests()
    yield
    exec_subprocess._reset_secrets_for_tests()


def _arm(monkeypatch, tmp_path, variables, env=None):
    """Drive the real registration over a real vars blob."""
    blob = tmp_path / "terraform.tfvars.json"
    blob.write_text(json.dumps(variables), encoding="utf-8")
    monkeypatch.setattr(job_entrypoint, "_VARS_FILE", blob)
    return job_entrypoint._register_workspace_secrets(env or {})


def _redact(text: str) -> str:
    redactor = exec_subprocess.StreamRedactor()
    return (redactor.feed(text.encode()) + redactor.flush()).decode()


class TestADeclaredSecretIsRegistered:
    def test_a_sensitive_scalar_is_stripped_from_the_log(self, monkeypatch, tmp_path):
        _arm(
            monkeypatch,
            tmp_path,
            [{"key": "db", "value": "SEKRET-canary-abc123", "sensitive": True}],
        )
        assert "SEKRET-canary-abc123" not in _redact("password=SEKRET-canary-abc123 ok")

    def test_an_env_category_secret_is_found_by_name(self, monkeypatch, tmp_path):
        # The value is never on the wire -- it arrives through `secretKeyRef` -- so
        # the listener sends the NAME and the value is read from the environment.
        env = {
            job_entrypoint.SENSITIVE_ENV_KEYS_VAR: json.dumps(["API_TOKEN"]),
            "API_TOKEN": "ENVSEKRET-tok-7733aa",
        }
        _arm(monkeypatch, tmp_path, [], env)
        assert "ENVSEKRET-tok-7733aa" not in _redact("Bearer ENVSEKRET-tok-7733aa")

    def test_a_leaf_of_a_structured_secret_is_stripped(self, monkeypatch, tmp_path):
        # The whole HCL expression cannot match what a provider logs: a value of
        # `{"user":...,"password":...}` reaches a request body as `password=p`.
        value = json.dumps({"user": "admin", "password": "LEAFSEKRET-9988x"})
        _arm(
            monkeypatch,
            tmp_path,
            [{"key": "creds", "structured": True, "sensitive": True, "value": value}],
        )
        assert "LEAFSEKRET-9988x" not in _redact("body password=LEAFSEKRET-9988x&user=admin")


class TestNothingElseIsTouched:
    """The over-redaction direction. A mask in place of a legitimate value is a
    defect an operator meets while reading a log, not one CI would ever see."""

    def test_a_non_sensitive_variable_is_left_alone(self, monkeypatch, tmp_path):
        registered = _arm(
            monkeypatch,
            tmp_path,
            [{"key": "region", "value": "eu-west-1-not-a-secret", "sensitive": False}],
        )
        assert registered == 0
        assert _redact("region=eu-west-1-not-a-secret") == "region=eu-west-1-not-a-secret"

    def test_a_short_leaf_of_a_structured_secret_is_not_registered(self, monkeypatch, tmp_path):
        # A leaf is INFERRED where the whole value was DECLARED: the operator marked
        # the object sensitive, not every string in it. `admin` is a username, and
        # blanking it would strip the word from every line of the log.
        value = json.dumps({"user": "admin", "password": "LEAFSEKRET-9988x"})
        _arm(
            monkeypatch,
            tmp_path,
            [{"key": "creds", "structured": True, "sensitive": True, "value": value}],
        )
        assert _redact("user admin logged in") == "user admin logged in"

    def test_a_key_of_a_structured_secret_is_never_registered(self, monkeypatch, tmp_path):
        # A key is a field name. Registering `password` would blank the word out of
        # every line that mentions it, including a line reporting a real failure.
        value = json.dumps({"passwordphrase": "LEAFSEKRET-9988x"})
        _arm(
            monkeypatch,
            tmp_path,
            [{"key": "creds", "structured": True, "sensitive": True, "value": value}],
        )
        assert (
            _redact("the passwordphrase field is required")
            == "the passwordphrase field is required"
        )

    def test_a_malformed_env_key_list_registers_nothing_and_does_not_raise(
        self, monkeypatch, tmp_path
    ):
        env = {
            job_entrypoint.SENSITIVE_ENV_KEYS_VAR: "not json",
            "API_TOKEN": "ENVSEKRET-tok-7733aa",
        }
        assert _arm(monkeypatch, tmp_path, [], env) == 0


class TestTheRegistrationIsReachedAtAll:
    """A behavioural test cannot see the call site, and the call site is the half
    that was missing for every engine but Ansible."""

    def test_the_entrypoint_arms_redaction_before_it_delivers_the_variables(self):
        src = _ENTRYPOINT.read_text(encoding="utf-8")
        arm = src.index("_register_workspace_secrets(os.environ)")
        deliver = src.index("tfvars.write_auto_tfvars(")
        # Positional, because a perfect registration after the first exec protects
        # nothing. The delivery is itself above `init`, so being above the delivery
        # puts this above every engine exec.
        assert arm < deliver, "redaction must be armed before the variables are delivered"

    def test_arming_is_not_gated_on_the_engine(self):
        # The delivery below it IS engine-gated. One registry serves every
        # `StreamRedactor`, so arming must not be: a Pulumi update and a configure
        # hand the same values to the same kind of third-party code.
        src = _ENTRYPOINT.read_text(encoding="utf-8")
        tree = ast.parse(src)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_register_workspace_secrets"
        ]
        assert len(calls) == 1, "one call, or the engine-neutrality claim is untestable"
        line = src.split("\n")[calls[0].lineno - 1]
        assert "if " not in line, f"the call is conditional: {line.strip()!r}"


#: Every module that registers a secret, and why. A fourth is not forbidden -- it
#: is a decision, and this is what makes it one rather than a drift.
_REGISTRARS = {
    "phases/ansible_auth.py": "a configure's vault password, SSH key material and credential values",
    "phases/cloud_identity.py": "the federation token, handed to a provider that logs its STS body",
    "job_entrypoint.py": "the workspace's own sensitive variables, for every engine (#2722)",
}


class TestOneProducerPerSecretClass:
    def test_the_set_of_modules_registering_secrets_is_declared(self):
        found = {}
        for path in sorted(_RUNNER.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "register_secret"
                ):
                    found[str(path.relative_to(_RUNNER))] = True
        assert set(found) == set(_REGISTRARS), (
            "a module registers secrets without a declared reason, or a declared one "
            "stopped: add it to _REGISTRARS with why, or remove the stale entry"
        )

    def test_the_floor_is_not_satisfied_by_an_empty_scan(self):
        # A lower bound is what is meant here, and the other direction -- the set
        # GROWING -- is protected by the equality above: a fourth module that
        # registers a secret fails that assertion whatever this number says. This
        # exists only so an AST walk that stops matching fails loudly instead of
        # passing on an empty scan.
        assert len(_REGISTRARS) >= 3


def test_redaction_floors_agree():
    """The runner's substituted-value floor equals the API's literal floor.

    Two constants rather than one import because `docker/Dockerfile.runner` ships
    three `services/` modules and `summary_redaction` is not one of them -- so the
    runner genuinely cannot reach it, and one producer per image with this gate
    across the boundary is the closest thing to one producer available. The test
    tree has both, which is why the agreement is checkable here and nowhere else.
    """
    from terrapod.services import summary_redaction

    assert exec_subprocess.SUBSTITUTED_VALUE_MIN_LEN == summary_redaction.MIN_LITERAL_LENGTH
