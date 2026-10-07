"""Phase: let the Terrapod provider authenticate from inside a run (#1968).

A workspace declares its inventory with `terrapod_inventory_item`, which means
the Terrapod provider runs *inside* the run and has to reach the Terrapod API.
The operator writes `provider "terrapod" {}` with nothing in it, and this is
what makes that work.

The provider reads `TERRAPOD_HOSTNAME` and `TERRAPOD_TOKEN` from the
environment (`provider/internal/provider/provider.go`), so this exports both
from what the Job already carries: `TP_API_URL` and `TP_AUTH_TOKEN`. Nothing
new is minted -- the run's own token is the credential, and the implicit grant
that makes it sufficient is "a runner token may manage the inventory items of
its own run's workspace", enforced in `api/routers/inventory.py`.

**The internal URL, not the public one.** `TERRAPOD_HOSTNAME` is named for a
hostname and accepts a full URL: `client.NewClient` prepends `https://` only
when there is no scheme, so a complete URL passes through untouched. That is
what we want, because the public hostname may not resolve from inside the
cluster at all -- on a local stack `terrapod.local` resolves to 127.0.0.1, and a
provider pointed at it would dial the runner pod's own loopback.

## Why this is exported here and NOT in the Job spec

Putting `TERRAPOD_TOKEN` in the Job spec would be a credential-redirection
hole, and the mechanism is worth stating because it is not obvious:

* `reserved_env.py` reserves the **`TP_` prefix only** (GHSA-7859-pwwx-vx4f),
  so a workspace `category=env` variable may legitimately be called
  `TERRAPOD_HOSTNAME`;
* `job_template` appends workspace env **after** the platform block, and
  Kubernetes gives the **last** duplicate precedence.

So a workspace variable setting only the hostname, with the token still coming
from the spec, would send the run's real token to a host the variable chose.
Exporting here instead closes it: this runs inside the container, after the
environment is in place, so what it sets is what the engine inherits.

## All-or-nothing deference

If **either** variable is already set, this exports **neither** and says so.

That preserves the workaround an operator may already be using today -- setting
both as workspace variables to drive the provider from inside an agent run,
which is the only way to do it before this exists -- while refusing the
dangerous half of it. Deferring one and supplying the other is exactly the
exfiltration above, arrived at from the other direction, so the two decisions
cannot be taken separately.

**Local execution mode is untouched**, and that is correct rather than an
oversight: there is no runner token, nothing here runs, and the provider needs a
host and token configured as it does today. The implicit grant is a property of
running *inside* a run.
"""

from __future__ import annotations

import structlog

#: What the provider reads. Deliberately the provider's own names rather than
#: `TP_`-prefixed ones: these are consumed by a third-party binary we do not
#: control the flags of. See the module docstring for why that is safe here and
#: would not be in the Job spec.
HOSTNAME_VAR = "TERRAPOD_HOSTNAME"
TOKEN_VAR = "TERRAPOD_TOKEN"

# `structlog` directly rather than `terrapod.logging_config`: the runner image
# ships only the modules `Dockerfile.runner` names, and that one is not among
# them. Matches every sibling phase.
logger = structlog.get_logger("runner.phase.provider_credentials")


def export_env(cfg, env: dict[str, str] | None = None) -> dict[str, str]:
    """The provider credentials to merge into the engine's environment.

    Pure: returns the overrides rather than mutating `os.environ`, so a caller
    can see what it is about to apply and a test needs no monkeypatching. Same
    contract as `mirror_config.export_env`.

    `env` is the environment to inspect for an operator-supplied value; the
    caller passes `os.environ`. Returns `{}` when there is nothing to do, which
    is the no-op a workspace that declares no inventory gets.
    """
    present = env if env is not None else {}

    operator_set = [name for name in (HOSTNAME_VAR, TOKEN_VAR) if present.get(name)]
    if operator_set:
        # Not a warning: configuring the provider yourself is a legitimate
        # thing to do, and this is the message that explains why the run's own
        # token is not in play.
        logger.info(
            "provider credentials left to the workspace",
            set_by_workspace=operator_set,
            reason=(
                "a workspace variable already configures the Terrapod provider, so the "
                "run's own token is not exported. Both variables are left alone together: "
                "supplying one and deferring the other would send this run's token to a "
                "host the workspace chose."
            ),
        )
        return {}

    api_url = (getattr(cfg, "api_url", "") or "").rstrip("/")
    token = getattr(cfg, "auth_token", "") or ""
    if not api_url or not token:
        # Nothing to export and nothing to complain about. A Job always carries
        # both, so this is the local-mode and unit-test shape rather than a
        # failure -- and a configure that genuinely needs them will fail on the
        # provider's own "Missing hostname" with a message naming the variable.
        return {}

    logger.info("provider credentials exported", hostname=api_url)
    return {HOSTNAME_VAR: api_url, TOKEN_VAR: token}
