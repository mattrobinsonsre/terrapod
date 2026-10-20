"""What the API tells a configure Job to do (#1972).

The runner reads these three `TP_*` variables and nothing else tells it which
playbook to run, so an omission here is a configure that refuses (no playbook) or
one that runs in the wrong directory -- neither of which any runner-tier test can
see, because they are handed a `RunnerConfig` that already has the values.

There is deliberately NO inventory variable. The inventory a configure targets
is the run's own resolved artifact (#2021), fetched by `configure_inventory`;
the workspace's `inventory_paths` are sources the API already merged into it, so
sending them would invite the runner to re-read them and merge the same hosts
twice. The last test here is what keeps that absence deliberate.
"""

from __future__ import annotations

from types import SimpleNamespace

from terrapod.engines.terraform import TerraformStrategy

#: The two deployment defaults `container_env` reads. Named rather than a
#: MagicMock: a mock answers every attribute, so a variable built from one it
#: should not touch would still look like a string.
_DEPLOYMENT = SimpleNamespace(
    default_terraform_version="1.13.0",
    default_execution_backend="tofu",
)


def _env(**attrs) -> dict[str, str]:
    strategy = TerraformStrategy()
    options = strategy.options_from_attrs(attrs, attrs.get("phase", "configure"))
    entries = strategy.container_env(options, _DEPLOYMENT)  # type: ignore[arg-type]
    return {e["name"]: e["value"] for e in entries}


class TestTheConfigureInstructions:
    def test_all_three_reach_the_job(self):
        env = _env(
            **{
                "config-directory": "ansible",
                "default-playbook": "site.yml",
                "default-limit": "web",
            }
        )
        assert env["TP_CONFIG_DIRECTORY"] == "ansible"
        assert env["TP_PLAYBOOK"] == "site.yml"
        assert env["TP_ANSIBLE_LIMIT"] == "web"

    def test_no_inventory_variable_is_sent(self):
        """The absence is the design, so it is asserted rather than assumed.

        A configure's `-i` is its resolved artifact and nothing else. The
        workspace's `inventory_paths` ARE sent to the API's own resolver, so a
        later change that plumbed them through to the Job as well would look
        like consistency and would quietly double the merge -- same hosts from
        two sources, precedence decided by argument order.
        """
        env = _env(
            **{
                "config-directory": "ansible",
                "default-playbook": "site.yml",
                "inventory-paths": ["inventory/production"],
            }
        )
        assert not [k for k in env if "INVENTORY" in k], sorted(env)

    def test_the_runs_own_playbook_wins_over_the_workspace_default(self):
        """#1974 adds the per-run attribute; the precedence is expressed here so
        that lands as a value rather than as a new decision."""
        env = _env(
            **{
                "config-directory": "ansible",
                "playbook": "patch-only.yml",
                "default-playbook": "site.yml",
                "ansible-limit": "canary",
                "default-limit": "web",
            }
        )
        assert env["TP_PLAYBOOK"] == "patch-only.yml"
        assert env["TP_ANSIBLE_LIMIT"] == "canary"

    def test_an_empty_config_directory_still_reaches_the_job(self):
        """`""` is the repository root, which is a real answer.

        It is falsey, so the only thing stopping it being dropped is that the
        API does not send the key at all for a run with no configure -- the
        distinction lives there rather than here.
        """
        env = _env(**{"config-directory": "", "default-playbook": "site.yml"})
        assert "TP_CONFIG_DIRECTORY" not in env
        assert env["TP_PLAYBOOK"] == "site.yml"


class TestATerraformOnlyDeploymentPaysNothing:
    def test_no_configure_variable_is_emitted_without_one(self):
        """The rendered Job spec stays byte-identical for a workspace that has no
        configure, which the golden spec matrix pins. A deployment running only
        terraform must pay nothing for this (#1972, and #1986's withdrawal of the
        engine gate means runtime adaptation is what delivers that)."""
        env = _env(**{"engine-version": "1.13.0"})
        for key in (
            "TP_CONFIG_DIRECTORY",
            "TP_PLAYBOOK",
            "TP_ANSIBLE_LIMIT",
            "TP_INVENTORY_PATHS",
        ):
            assert key not in env, f"{key} was emitted for a run with no configure"
