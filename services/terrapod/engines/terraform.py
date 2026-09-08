"""The Terraform/OpenTofu strategy — today's behaviour, byte for byte.

#1407 phase 1. Every method here delegates to the code that already runs, so
routing a call site through the strategy changes nothing about what executes.
Phases 2 and 3 move the bodies in; this file is where they land.
"""

from __future__ import annotations

from typing import Any


class TerraformStrategy:
    """Terraform and OpenTofu, which share one engine and differ only in binary.

    `execution_backend` picks between them per workspace — that is a choice
    *within* this engine, not a different engine, which is why both are served
    here rather than by two strategies.
    """

    name = "terraform"

    #: `terraform plan` then `terraform apply`. The reconciler and the UI already
    #: speak these words; naming them here is what lets a later engine say
    #: `preview`/`update` without the platform hard-coding Terraform's vocabulary.
    phases = ("plan", "apply")

    #: OpenTofu, matching the column default on workspaces.
    default_execution_backend = "tofu"

    def build_job_spec(self, **kwargs: Any) -> dict:
        """Delegate to the existing builder, unchanged.

        Imported here rather than at module scope so this package stays free of
        `runner` at import time — the API imports the strategy and has no use for
        the Job template, and keeping the edge lazy means neither image pays for
        the other's code.

        #1488 replaces this `**kwargs` pass-through with a structured argument;
        the 34-parameter signature is exactly what that issue exists to retire.
        Until then this is a pure forward so nothing about the spec can change.
        """
        from terrapod.runner.job_template import build_job_spec

        return build_job_spec(**kwargs)
