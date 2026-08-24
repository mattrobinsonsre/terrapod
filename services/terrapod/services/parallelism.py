"""The parallelism setting and its one rule (#1431).

How many operations the engine performs at once: `-parallelism` for
terraform/tofu, `--parallel` for Pulumi, `forks` for Ansible. The unit differs by
engine — the first two count concurrent resource operations, Ansible counts hosts
— but the question the operator is answering is the same one, so it is one
setting.

Lives here rather than in a router because four paths write it: workspace create,
workspace update, the autodiscovery rule template, and bulk update. A validator
only one caller uses is one the next caller forgets.
"""

from __future__ import annotations

#: Terraform's own default. Chosen so that adding this setting changed no
#: existing workspace's behaviour — every one of them was already running at 10.
DEFAULT_PARALLELISM = 10

#: Not a limit the engines impose; a guard against a value that is certainly a
#: mistake. Concurrency is bounded in practice by the workspace's CPU and memory,
#: and a four-figure setting will exhaust one or the other long before it helps.
MAX_PARALLELISM = 256


def validate_parallelism(raw: object) -> int:
    """Coerce and bounds-check, or raise ValueError with something actionable.

    Rejects `0` explicitly. Terraform reads `-parallelism=0` as "no limit" while
    Pulumi reads `--parallel 1` as "fully serial" and has no zero form, so a
    shared setting cannot honour zero without meaning different things per
    engine. Anyone wanting serial execution wants 1, and says so.

    Accepts a string because JSON:API clients and Helm-shaped input both hand
    numbers over as text often enough that refusing would be pedantry.
    """
    if isinstance(raw, bool):
        # bool is an int subclass, and `True` would otherwise quietly mean 1.
        raise ValueError("parallelism must be a whole number, not a boolean")
    if isinstance(raw, str):
        try:
            raw = int(raw.strip())
        except ValueError:
            raise ValueError(f"parallelism must be a whole number, got {raw!r}") from None
    if not isinstance(raw, int):
        raise ValueError(f"parallelism must be a whole number, got {type(raw).__name__}")
    if raw < 1:
        raise ValueError(
            "parallelism must be at least 1 (1 runs operations one at a time); "
            "0 is not accepted because it means 'unlimited' to terraform and "
            "nothing at all to the other engines"
        )
    if raw > MAX_PARALLELISM:
        raise ValueError(f"parallelism must be at most {MAX_PARALLELISM}, got {raw}")
    return raw
