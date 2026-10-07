"""Exporting the Terrapod provider's credentials inside a run (#1968).

The security property is the all-or-nothing rule, and it is worth stating why
it is not two independent decisions.

`reserved_env.py` reserves the **`TP_` prefix only**, and `job_template`
appends workspace `category=env` variables **after** the platform block --
where Kubernetes gives the last duplicate precedence. So if these two variables
were set from the Job spec, a workspace variable setting only
`TERRAPOD_HOSTNAME` would leave the real token in place and send it to a host
the variable chose. Exporting from the entrypoint closes that, but the same
asymmetry reappears if this module ever defers one variable and supplies the
other -- which is why the two are decided together.
"""

from __future__ import annotations

from types import SimpleNamespace

from terrapod.runner.phases import provider_credentials as pc


def _cfg(api_url="http://terrapod-api:8000", auth_token="runtok:abc"):
    return SimpleNamespace(api_url=api_url, auth_token=auth_token)


class TestTheHappyPath:
    def test_it_exports_both_from_the_run_s_own_credentials(self):
        out = pc.export_env(_cfg(), env={})

        assert out == {
            pc.HOSTNAME_VAR: "http://terrapod-api:8000",
            pc.TOKEN_VAR: "runtok:abc",
        }

    def test_it_exports_the_internal_url_not_a_bare_hostname(self):
        """The provider prepends `https://` only when there is no scheme, so a
        full URL passes through -- which is what we need, because the public
        hostname may not resolve from inside the cluster at all."""
        out = pc.export_env(_cfg(api_url="http://terrapod-api:8000"), env={})

        assert out[pc.HOSTNAME_VAR].startswith("http://")

    def test_a_trailing_slash_is_trimmed(self):
        out = pc.export_env(_cfg(api_url="http://terrapod-api:8000/"), env={})

        assert out[pc.HOSTNAME_VAR] == "http://terrapod-api:8000"

    def test_it_does_not_mutate_the_environment_it_was_given(self):
        """Pure, like `mirror_config.export_env`: the caller applies it, so a
        test needs no monkeypatching and a reader can see what is about to
        change."""
        env: dict[str, str] = {}
        pc.export_env(_cfg(), env=env)

        assert env == {}


class TestAllOrNothingDeference:
    def test_a_workspace_setting_BOTH_keeps_control(self):
        """The workaround an operator may already be using: configuring the
        provider with workspace variables, which is the only way to do it before
        this exists."""
        out = pc.export_env(
            _cfg(),
            env={pc.HOSTNAME_VAR: "https://terrapod.example.com", pc.TOKEN_VAR: "theirs"},
        )

        assert out == {}

    def test_a_workspace_setting_ONLY_THE_HOST_gets_no_token(self):
        """The case that would be exfiltration.

        Supplying the run's real token beside a host the workspace chose is how
        a credential leaves the cluster. So the host being set means neither is
        exported.
        """
        out = pc.export_env(_cfg(), env={pc.HOSTNAME_VAR: "https://attacker.example"})

        assert out == {}, "a workspace-chosen host must never be paired with our token"

    def test_a_workspace_setting_ONLY_THE_TOKEN_gets_no_host(self):
        """The mirror case. Less dangerous, but decided the same way so the rule
        has no asymmetry to reason about."""
        out = pc.export_env(_cfg(), env={pc.TOKEN_VAR: "theirs"})

        assert out == {}

    def test_an_empty_value_does_not_count_as_set(self):
        """An env var present but empty is how a cleared variable arrives, and
        the provider would reject it anyway -- so it is an absence, not a
        declaration."""
        out = pc.export_env(_cfg(), env={pc.HOSTNAME_VAR: "", pc.TOKEN_VAR: ""})

        assert out == {
            pc.HOSTNAME_VAR: "http://terrapod-api:8000",
            pc.TOKEN_VAR: "runtok:abc",
        }


class TestNothingToDo:
    def test_no_api_url_exports_nothing(self):
        """Local-mode and unit-test shape. A configure that genuinely needs the
        provider fails on its own "Missing hostname", which names the variable."""
        assert pc.export_env(_cfg(api_url=""), env={}) == {}

    def test_no_token_exports_nothing(self):
        assert pc.export_env(_cfg(auth_token=""), env={}) == {}

    def test_a_config_missing_the_attributes_entirely_is_not_an_error(self):
        assert pc.export_env(SimpleNamespace(), env={}) == {}

    def test_env_defaults_to_empty_rather_than_reading_the_process(self):
        """The caller passes `os.environ` explicitly; this must not reach for it
        itself, or a test's own environment would change the answer."""
        assert pc.export_env(_cfg()) != {}


class TestTheNamesAreTheProvidersOwn:
    def test_the_exported_names_are_what_the_provider_reads(self):
        """Deliberately unprefixed: these are consumed by a third-party binary
        whose flag names we do not control. Safe here precisely because this runs
        after the container environment is in place -- see the module docstring
        for why the same names would not be safe in the Job spec.
        """
        assert pc.HOSTNAME_VAR == "TERRAPOD_HOSTNAME"
        assert pc.TOKEN_VAR == "TERRAPOD_TOKEN"

    def test_neither_name_is_reserved_which_is_why_this_module_exists(self):
        """If `reserved_env` covered them, a workspace variable could not shadow
        them and this could have lived in the Job spec. It does not, so the
        export has to happen later than the spec."""
        from terrapod.runner.reserved_env import is_reserved_env_key

        assert not is_reserved_env_key(pc.HOSTNAME_VAR)
        assert not is_reserved_env_key(pc.TOKEN_VAR)
