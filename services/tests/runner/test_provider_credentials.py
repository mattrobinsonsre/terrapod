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


class TestTheEngineGate:
    """A credential an engine cannot use is exposure with no benefit.

    `exec_subprocess` scrubs `TP_AUTH_TOKEN` out of the engine's environment --
    "a provider is third-party code ... it has no business also holding the token
    that writes this run's state" -- and the scrub covers the `TP_` prefix, so
    what this module exports is NOT scrubbed and carries the same token value.
    That is unavoidable for the feature: Terraform has no per-provider
    environment, so anything the Terrapod provider reads, every provider reads.

    What is avoidable is handing it to an engine with no resource to use it.
    `terrapod_inventory_item` is a Terraform resource; Pulumi has no equivalent
    (#1987), so a Pulumi run would get all of the exposure and none of the use.
    """

    def test_a_pulumi_run_gets_nothing(self):
        assert pc.export_env(_cfg(), env={}, engine="pulumi") == {}

    def test_terraform_and_opentofu_both_get_it(self):
        for engine in ("terraform", "tofu", "opentofu"):
            out = pc.export_env(_cfg(), env={}, engine=engine)
            assert out != {}, engine

    def test_an_absent_engine_is_terraform(self):
        """`TP_ENGINE` is unset on a Terraform run, so the empty string is the
        common case rather than an unknown."""
        assert pc.export_env(_cfg(), env={}, engine="") != {}

    def test_an_unrecognised_engine_gets_nothing(self):
        """Fails closed, so a new engine opts in here rather than inheriting the
        credential by default."""
        assert pc.export_env(_cfg(), env={}, engine="ansible") == {}
        assert pc.export_env(_cfg(), env={}, engine="chef") == {}

    def test_the_default_argument_still_exports(self):
        """Every pre-existing caller omits `engine`, so the default must be the
        exporting case -- a default that silently stopped exporting would turn
        this narrowing into a feature outage."""
        assert pc.export_env(_cfg(), env={}) != {}


class TestTheEntrypointActuallyEngagesTheGate:
    """A gate the call site does not pass the engine to is decoration.

    This exists because the obvious mutation proved it: dropping `engine=` from
    the entrypoint's call left the whole runner suite green -- 1061 passed --
    because every behavioural test calls `export_env` directly and the default
    is the exporting case. So the gate was present and never engaged, which is
    the weaker half of "a presence check is not an application check".

    Source-introspected rather than driven, because the call sits mid-way
    through `main()` after a chdir and a dozen side effects, and there is no seam
    to reach it through.
    """

    def _main_source(self) -> str:
        import inspect

        from terrapod.runner import job_entrypoint

        return inspect.getsource(job_entrypoint)

    def test_the_call_site_passes_the_engine(self):
        src = self._main_source()
        assert "provider_credentials.export_env(" in src, "the phase is not wired at all"
        assert "engine=engine" in src, (
            "the entrypoint must pass the run's engine, or the gate never fires and a "
            "Pulumi run is handed a credential it cannot use"
        )

    def test_the_call_comes_AFTER_the_engine_is_read(self):
        """Positional, because Python would raise on an unbound name but a
        future refactor could just as easily read the engine twice or default it
        -- and a call placed above the read would have to invent a value."""
        src = self._main_source()
        read_at = src.index('engine = os.environ.get("TP_ENGINE"')
        call_at = src.index("provider_credentials.export_env(")
        assert read_at < call_at, (
            "the export reads the engine, so it has to run after the engine is known"
        )


class TestTheScrubDoesNotCoverThese:
    """Why the exposure exists at all, pinned so it cannot be forgotten.

    If a future change reserved these names, `exec_subprocess` would scrub them
    and the feature would break silently -- an empty `provider "terrapod" {}`
    would start failing with a missing-hostname error and nothing would say why.
    So the relationship is asserted in both directions rather than assumed.
    """

    def test_the_exported_names_survive_the_engine_env_scrub(self):
        from terrapod.runner.reserved_env import is_reserved_env_key

        assert not is_reserved_env_key(pc.HOSTNAME_VAR)
        assert not is_reserved_env_key(pc.TOKEN_VAR)

    def test_the_scrub_really_is_that_predicate(self):
        """The two assertions above are only about the scrub if the scrub uses
        this predicate. Checked by reading the source, because the filtering is
        a comprehension inside `run()` and there is no seam to call."""
        import inspect

        from terrapod.runner import exec_subprocess

        src = inspect.getsource(exec_subprocess.run)
        assert "is_reserved_env_key" in src
        assert "env=child_env" in src, (
            "the scrubbed env has to actually be passed to the child, or the "
            "filtering above is decoration"
        )

    def test_the_platform_token_this_duplicates_IS_scrubbed(self):
        """`TP_AUTH_TOKEN` is removed from the child env; the value we export
        under a different name is the same token. That asymmetry is the reason a
        narrower credential is the real answer."""
        from terrapod.runner.reserved_env import is_reserved_env_key

        assert is_reserved_env_key("TP_AUTH_TOKEN")


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
