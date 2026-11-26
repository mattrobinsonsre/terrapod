"""Environment variable names a workspace variable may not take.

The runner Job's container environment is built platform-block first, workspace
variables second, and Kubernetes gives the **last** duplicate name precedence.
So a workspace variable named `TP_API_URL` or `TP_AUTH_TOKEN` won, which let
anyone holding variable-write on a workspace point the runner at a host of their
choosing and collect the run's own token on the way (GHSA-7859-pwwx-vx4f).

**Scope is deliberately narrow: `TP_*` only.**

`TP_*` is platform plumbing. A workspace variable of that name is not a
configuration choice a user could reasonably mean — it is a collision with an
internal contract, and no legitimate use exists.

Terraform's own settings are **not** reserved, `TF_LOG` included. HCP Terraform
and Terraform Enterprise document setting `TF_LOG=TRACE` as a workspace
environment variable as the supported way to debug a run, and ship a per-run
"Enable Debug Logging" toggle besides; they disclose that it can put sensitive
values in logs and leave the choice to the operator. Terrapod is a replacement
for that platform, so refusing a variable HashiCorp's own runbook tells people
to set would break migrating users to enforce a policy the incumbent does not
have. The exposure `TF_LOG` creates is that run logs are readable — which is
fixed by making the log URL a capability, not by banning the variable.

Two consequences worth stating, because they are easy to get wrong later:

- This is enforced by **dropping the key at injection**, not by reordering the
  platform and workspace blocks. Reordering would fix only the keys the platform
  always emits; several `TP_*` names (`TP_DESTROY`, `TP_PLAN_ONLY`,
  `TP_VAR_FILES`) are emitted only for certain runs, so there is no collision
  for precedence to resolve and a stored value would still take effect.
- Dropping at injection also fixes variables **already stored**, which a
  write-time rejection alone cannot.
"""

from __future__ import annotations

#: The platform's own prefix. Everything the runner is told about itself — its
#: API address, its token, its run id, its phase — is namespaced under it.
RESERVED_ENV_PREFIX = "TP_"

#: Carries the env-category variable NAMES the API flagged sensitive, from the
#: listener to the runner, so the runner can register their values with the log
#: redactor (#2722).
#:
#: NAMES, never values. The values reach the container through `secretKeyRef`
#: precisely so they are absent from the Job spec, and the runner reads each
#: from its own environment -- so this carries nothing that `kubectl describe`
#: could not already see.
#:
#: A JSON array rather than a delimited list because nothing constrains an env
#: variable name to exclude a delimiter: `_validated_env_key` refuses the
#: reserved prefix and says nothing about the rest of the name.
#:
#: It lives here, with the prefix, so both ends import one name -- and so it is
#: reserved by construction: a workspace variable cannot shadow it, and
#: `exec_subprocess` scrubs the prefix, so it never reaches the engine.
SENSITIVE_ENV_KEYS_VAR = f"{RESERVED_ENV_PREFIX}SENSITIVE_ENV_KEYS"


def is_reserved_env_key(key: str) -> bool:
    """Whether `key` collides with platform plumbing.

    Case-insensitive: the environment is case-sensitive on Linux, but a variable
    named `tp_auth_token` is a transparent attempt at the same thing and has no
    legitimate meaning either.
    """
    return key.strip().upper().startswith(RESERVED_ENV_PREFIX)
