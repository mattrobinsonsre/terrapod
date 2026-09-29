"""The workspace's Pulumi config reaches the stack, and the value never hits argv (#1565).

These drive a **fake `pulumi`** — a script that records its argv and its stdin —
rather than mocking `subprocess.run`, because the two properties worth pinning
here are properties of the process boundary itself: that the value travels on
stdin, and that it is absent from the command line. A mock asserting on the
arguments we passed to a patched function would pass just as happily if the code
had put the secret in argv, which is the exact failure being guarded.

The behaviour of the real CLI these rest on was measured against pulumi v3.208.0,
not inferred from its documentation:

  - `pulumi config set KEY --non-interactive` with the value on stdin works, and
    stores it; the help text calls it a prompt, and a prompt is what a
    non-interactive run does not get, so this needed checking.
  - `--secret` writes `secure:` ciphertext into `Pulumi.<stack>.yaml`.
  - `--path outer.inner` nests, and composes with `--secret`.
  - `aws:region` survives verbatim; an unqualified key is namespaced to the
    project by the CLI, so nothing here prefixes anything.
  - stdin strips exactly one trailing newline: `abc` and `abc\\n` both store
    `abc`, and `abc\\n\\n` stores `abc\\n`. That is why one is appended.
  - `config set` merges per key into the stack's config file: a key set here
    overwrites a committed one of the same name, and committed keys left alone
    survive.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from terrapod.runner.phases import pulumi_config

#: Records argv (one per line) into `argv.txt` and stdin verbatim into
#: `stdin.bin`, appending so several invocations can be read back in order.
_FAKE = """#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a" >> "$OUT/argv.txt"; done
printf -- '---\\n' >> "$OUT/argv.txt"
cat >> "$OUT/stdin.bin"
exit ${FAKE_RC:-0}
"""


@pytest.fixture
def fake_pulumi(tmp_path, monkeypatch):
    binary = tmp_path / "pulumi"
    binary.write_text(_FAKE, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("OUT", str(tmp_path))
    return binary


def _write(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "terraform.tfvars.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _argv_calls(tmp_path: Path) -> list[list[str]]:
    raw = (tmp_path / "argv.txt").read_text(encoding="utf-8")
    return [block.splitlines() for block in raw.split("---\n") if block.strip()]


def _stdin(tmp_path: Path) -> bytes:
    return (tmp_path / "stdin.bin").read_bytes()


class TestTheValueNeverReachesTheCommandLine:
    """The runner streams its output to the API and the UI, so log-safety is
    mechanism rather than redaction — the same guarantee `git_auth` holds."""

    def test_a_secret_value_is_absent_from_argv_and_present_on_stdin(
        self, tmp_path, fake_pulumi
    ) -> None:
        path = _write(tmp_path, [{"key": "dbpass", "value": "sup3rs3cret", "sensitive": True}])
        assert pulumi_config.apply(str(fake_pulumi), stack="proj/dev", path=path) == 1

        (argv,) = _argv_calls(tmp_path)
        assert "sup3rs3cret" not in " ".join(argv)
        assert b"sup3rs3cret" in _stdin(tmp_path)

    def test_an_ordinary_value_is_absent_from_argv_too(self, tmp_path, fake_pulumi) -> None:
        """Not only secrets. A non-sensitive value on the command line would
        still be one an operator has to think about before pasting a log."""
        path = _write(tmp_path, [{"key": "region", "value": "eu-west-1"}])
        pulumi_config.apply(str(fake_pulumi), stack="proj/dev", path=path)

        (argv,) = _argv_calls(tmp_path)
        assert "eu-west-1" not in " ".join(argv)
        assert b"eu-west-1" in _stdin(tmp_path)

    def test_a_value_shaped_like_a_flag_is_delivered_unharmed(self, tmp_path, fake_pulumi) -> None:
        """Nothing has to be escaped, because nothing is parsed — which is the
        second reason stdin is the right channel, after log-safety."""
        path = _write(tmp_path, [{"key": "k", "value": "--secret"}])
        pulumi_config.apply(str(fake_pulumi), stack="proj/dev", path=path)

        assert _stdin(tmp_path) == b"--secret\n"


class TestExactlyOneTrailingNewlineIsAppended:
    """pulumi strips one; appending one means the operator's value survives
    byte-for-byte, including when it genuinely ends in a newline."""

    def test_a_plain_value_gets_one(self, tmp_path, fake_pulumi) -> None:
        path = _write(tmp_path, [{"key": "k", "value": "abc"}])
        pulumi_config.apply(str(fake_pulumi), stack="s", path=path)
        assert _stdin(tmp_path) == b"abc\n"

    def test_a_value_that_ends_in_a_newline_gets_one_more(self, tmp_path, fake_pulumi) -> None:
        """Without this the final newline of a PEM key is silently eaten."""
        path = _write(tmp_path, [{"key": "k", "value": "-----BEGIN-----\nbody\n"}])
        pulumi_config.apply(str(fake_pulumi), stack="s", path=path)
        assert _stdin(tmp_path) == b"-----BEGIN-----\nbody\n\n"

    def test_an_empty_value_is_still_a_single_newline(self, tmp_path, fake_pulumi) -> None:
        path = _write(tmp_path, [{"key": "k", "value": ""}])
        pulumi_config.apply(str(fake_pulumi), stack="s", path=path)
        assert _stdin(tmp_path) == b"\n"


class TestTheFlagsMeanWhatTheVariableSaid:
    def test_sensitive_becomes_secret(self, tmp_path, fake_pulumi) -> None:
        """Pulumi's own encryption, so its engine renders `[secret]` in the
        preview, the event log and the state — not merely careful delivery."""
        path = _write(tmp_path, [{"key": "k", "value": "v", "sensitive": True}])
        pulumi_config.apply(str(fake_pulumi), stack="s", path=path)
        assert "--secret" in _argv_calls(tmp_path)[0]

    def test_not_sensitive_passes_no_secret_flag(self, tmp_path, fake_pulumi) -> None:
        path = _write(tmp_path, [{"key": "k", "value": "v"}])
        pulumi_config.apply(str(fake_pulumi), stack="s", path=path)
        assert "--secret" not in _argv_calls(tmp_path)[0]

    def test_structured_becomes_path(self, tmp_path, fake_pulumi) -> None:
        path = _write(tmp_path, [{"key": "outer.inner", "value": "v", "structured": True}])
        pulumi_config.apply(str(fake_pulumi), stack="s", path=path)
        argv = _argv_calls(tmp_path)[0]
        assert "--path" in argv
        assert "outer.inner" in argv

    def test_not_structured_passes_no_path_flag(self, tmp_path, fake_pulumi) -> None:
        """A dotted key without `structured` is a literal key, not a nested
        one — the same distinction the flag already draws for terraform."""
        path = _write(tmp_path, [{"key": "outer.inner", "value": "v"}])
        pulumi_config.apply(str(fake_pulumi), stack="s", path=path)
        assert "--path" not in _argv_calls(tmp_path)[0]

    def test_the_run_is_always_non_interactive(self, tmp_path, fake_pulumi) -> None:
        path = _write(tmp_path, [{"key": "k", "value": "v"}])
        pulumi_config.apply(str(fake_pulumi), stack="s", path=path)
        assert "--non-interactive" in _argv_calls(tmp_path)[0]

    def test_it_is_pinned_to_the_runs_own_stack(self, tmp_path, fake_pulumi) -> None:
        path = _write(tmp_path, [{"key": "k", "value": "v"}])
        pulumi_config.apply(str(fake_pulumi), stack="proj/dev", path=path)
        argv = _argv_calls(tmp_path)[0]
        assert argv[argv.index("--stack") + 1] == "proj/dev"


class TestKeysPassThroughVerbatim:
    """#1407 §6 makes this a negative rule: never prefix, because a
    transformation is a thing that can be wrong."""

    @pytest.mark.parametrize(
        "key", ["region", "aws:region", "myproject:region", "kubernetes:context"]
    )
    def test_the_key_is_exactly_what_was_configured(self, tmp_path, fake_pulumi, key) -> None:
        path = _write(tmp_path, [{"key": key, "value": "v"}])
        pulumi_config.apply(str(fake_pulumi), stack="s", path=path)
        assert key in _argv_calls(tmp_path)[0]

    def test_an_unqualified_key_is_not_given_a_namespace(self, tmp_path, fake_pulumi) -> None:
        """The CLI namespaces it to the project in `Pulumi.yaml`, and the runner
        already runs in the project directory, so doing it here would double it."""
        path = _write(tmp_path, [{"key": "region", "value": "v"}])
        pulumi_config.apply(str(fake_pulumi), stack="proj/dev", path=path)
        argv = _argv_calls(tmp_path)[0]
        assert "region" in argv
        assert not any(a.endswith(":region") for a in argv)


class TestEveryValueIsSetAndNoneIsSkipped:
    def test_all_of_them_in_order(self, tmp_path, fake_pulumi) -> None:
        path = _write(
            tmp_path,
            [{"key": "a", "value": "1"}, {"key": "b", "value": "2"}, {"key": "c", "value": "3"}],
        )
        assert pulumi_config.apply(str(fake_pulumi), stack="s", path=path) == 3
        calls = _argv_calls(tmp_path)
        assert [c[-1] for c in calls] == ["a", "b", "c"]
        assert _stdin(tmp_path) == b"1\n2\n3\n"

    def test_no_file_is_not_an_error(self, tmp_path, fake_pulumi) -> None:
        """A workspace with no Pulumi config mounts none, and the stack simply
        runs on whatever its repository committed."""
        assert pulumi_config.apply(str(fake_pulumi), stack="s", path=tmp_path / "absent") == 0
        assert not (tmp_path / "argv.txt").exists()

    def test_an_empty_list_runs_nothing(self, tmp_path, fake_pulumi) -> None:
        path = _write(tmp_path, [])
        assert pulumi_config.apply(str(fake_pulumi), stack="s", path=path) == 0
        assert not (tmp_path / "argv.txt").exists()


class TestAFailureStopsTheRun:
    """Dropping an entry with a warning would be the silent-drop this issue
    exists to remove: `config.get` with a default would take the default, and
    only `config.require` would complain."""

    def test_a_nonzero_exit_raises(self, tmp_path, fake_pulumi, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_RC", "1")
        path = _write(tmp_path, [{"key": "k", "value": "v"}])
        with pytest.raises(pulumi_config.ConfigError) as exc:
            pulumi_config.apply(str(fake_pulumi), stack="s", path=path)
        assert "k" in str(exc.value)

    def test_the_failure_message_does_not_carry_the_value(
        self, tmp_path, fake_pulumi, monkeypatch
    ) -> None:
        """The error is logged and reported, so it is one more surface the
        value must not reach."""
        monkeypatch.setenv("FAKE_RC", "1")
        path = _write(tmp_path, [{"key": "k", "value": "sup3rs3cret", "sensitive": True}])
        with pytest.raises(pulumi_config.ConfigError) as exc:
            pulumi_config.apply(str(fake_pulumi), stack="s", path=path)
        assert "sup3rs3cret" not in str(exc.value)

    def test_an_entry_with_no_key_raises(self, tmp_path, fake_pulumi) -> None:
        path = _write(tmp_path, [{"key": "", "value": "v"}])
        with pytest.raises(pulumi_config.ConfigError):
            pulumi_config.apply(str(fake_pulumi), stack="s", path=path)

    def test_unreadable_json_raises(self, tmp_path, fake_pulumi) -> None:
        path = tmp_path / "terraform.tfvars.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(pulumi_config.ConfigError):
            pulumi_config.apply(str(fake_pulumi), stack="s", path=path)

    def test_a_later_failure_does_not_pretend_the_earlier_ones_did_not_happen(
        self, tmp_path, fake_pulumi
    ) -> None:
        """It raises rather than returning a count, so the caller fails the run
        instead of proceeding with a partially-configured stack."""
        os.environ.pop("FAKE_RC", None)
        path = _write(tmp_path, [{"key": "a", "value": "1"}, {"key": "", "value": "2"}])
        with pytest.raises(pulumi_config.ConfigError):
            pulumi_config.apply(str(fake_pulumi), stack="s", path=path)
        assert len(_argv_calls(tmp_path)) == 1


class TestNothingSensitiveIsLogged:
    def test_the_log_line_carries_keys_but_no_values(self, tmp_path, fake_pulumi) -> None:
        import inspect

        src = inspect.getsource(pulumi_config.apply)
        assert 'e.get("key")' in src
        assert 'e.get("value")' not in src
        assert "entry.get('value')" not in src


class TestTheMergeWithACommittedStackFileIsPerKey:
    """The decided rule is that Terrapod's config overrides a committed
    `Pulumi.<stack>.yaml` key of the same name and leaves every other key in
    that file alone (#1565).

    That is not something this code implements — it is what `pulumi config set`
    does, verified against the CLI: setting a key that already exists in the
    file replaces just that entry, and keys never set survive. What this code
    must do is keep *using* that mechanism, so the guard is on the mechanism.
    """

    def test_it_never_replaces_the_stack_config_file(self, tmp_path, fake_pulumi) -> None:
        """`--config-file` does not merge — it tells pulumi to use the named file
        *instead of* the detected one. Collapsing these calls into one write of a
        generated file would look like a tidy-up and would silently discard every
        committed key the workspace does not set, which is the opposite of the
        rule. `set-all` is the same trap in a smaller package.
        """
        path = _write(tmp_path, [{"key": "a", "value": "1"}, {"key": "b", "value": "2"}])
        pulumi_config.apply(str(fake_pulumi), stack="s", path=path)
        for argv in _argv_calls(tmp_path):
            assert "--config-file" not in argv
            assert "set-all" not in argv

    def test_each_key_is_set_on_its_own(self, tmp_path, fake_pulumi) -> None:
        """One invocation per key is what keeps the write per-key. It also keeps
        the values on separate stdin writes, which is what lets each one carry a
        value containing anything at all."""
        path = _write(tmp_path, [{"key": "a", "value": "1"}, {"key": "b", "value": "2"}])
        pulumi_config.apply(str(fake_pulumi), stack="s", path=path)
        calls = _argv_calls(tmp_path)
        assert len(calls) == 2
        for argv in calls:
            # The fake records "$@", so argv[0] (the binary) is not in it.
            assert argv[:2] == ["config", "set"]


class TestItReadsTheOneDeliveredBlob:
    """The workspace's native variables are one list for every engine (#1898),
    and this phase is Pulumi's delivery of it. So the file it reads is the same
    file a Terraform run renders into `terrapod.auto.tfvars` — not a second
    Pulumi-only channel, which is what #1565 first built and what proved to be
    the same four fields under another name.
    """

    def test_the_default_path_is_the_shared_vars_blob(self) -> None:
        """Pinned by name, because a drift here is silent: the phase would find
        no file, set nothing, and the run would proceed on whatever the
        repository committed — a wrong answer that looks like a workspace with
        no config."""
        assert pulumi_config._CONFIG_FILE == Path("/var/run/terrapod/vars/terraform.tfvars.json")

    def test_it_matches_the_key_the_listener_writes(self) -> None:
        """The listener writes this key and job_template mounts it at this path;
        the three have to agree or nothing arrives."""
        from terrapod.runner import job_template

        assert job_template._TFVARS_SECRET_KEY == "terraform.tfvars.json"
        assert pulumi_config._CONFIG_FILE.name == job_template._TFVARS_FILENAME

    def test_an_entry_from_a_lagging_listener_still_sets(self, tmp_path, fake_pulumi) -> None:
        """A listener that predates #1898 sends `structured`/`hcl` and no
        `sensitive`. That must degrade to a plain, non-nested `config set` — the
        behaviour every run had before secrets and paths existed — rather than
        failing or guessing."""
        path = _write(tmp_path, [{"key": "k", "value": "v", "hcl": False}])
        assert pulumi_config.apply(str(fake_pulumi), stack="s", path=path) == 1
        argv = _argv_calls(tmp_path)[0]
        assert "--secret" not in argv
        assert "--path" not in argv
        assert _stdin(tmp_path) == b"v\n"
