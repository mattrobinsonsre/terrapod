"""Configuration model for the runner Job entrypoint.

Parses the env vars the listener injects on Job spec — see
runner/job_template.py for the producer side. Each migrated phase
reads the same model so the contract is in one place and unit-test-
able without spinning up a Job pod.

This module is intentionally Pydantic-light. We only need a typed
view over os.environ; no .env file loading, no nested models.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RunnerConfig:
    """Runtime contract between the listener and the Job pod.

    Field names match the TP_* env-var names exactly, lowercased and
    de-prefixed where unambiguous. Adding a field here is a contract
    change with the listener; mirror in runner/job_template.py.
    """

    # Identity + auth — present on every Job
    api_url: str
    public_api_url: str
    auth_token: str
    run_id: str
    phase: Literal["plan", "apply"]

    # Backend selection
    backend: Literal["terraform", "tofu"]
    version: str

    # Workspace context
    workspace_id: str
    working_dir: str

    # Plan/apply options
    target_addrs: list[str]
    replace_addrs: list[str]
    var_files: list[str]
    refresh: bool
    refresh_only: bool
    allow_empty_apply: bool
    destroy: bool
    plan_only: bool

    # Misc behaviours
    setup_script: str
    termination_grace_period_seconds: int
    upload_timeout_seconds: int
    download_retries: int
    download_retry_delay_seconds: int

    # Platform — derived locally, exposed here so phases can branch
    # without re-shelling out to uname.
    os: str
    arch: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> RunnerConfig:
        e = env if env is not None else os.environ

        def _bool(name: str, default: bool = False) -> bool:
            v = e.get(name, "")
            if not v:
                return default
            return v.lower() in ("1", "true", "yes", "on")

        def _int(name: str, default: int) -> int:
            v = e.get(name)
            if not v:
                return default
            try:
                return int(v)
            except ValueError:
                return default

        def _json_list(name: str) -> list[str]:
            import json

            raw = e.get(name, "")
            if not raw or raw == "[]":
                return []
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return []
            return [str(x) for x in parsed if isinstance(x, str | int | float)]

        # uname → terraform release-naming convention
        os_name = platform.system().lower()  # "linux", "darwin"
        arch_raw = platform.machine()
        arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(arch_raw, arch_raw)

        return cls(
            api_url=e.get("TP_API_URL", "").rstrip("/"),
            public_api_url=e.get("TP_PUBLIC_API_URL", "").rstrip("/"),
            auth_token=e.get("TP_AUTH_TOKEN", ""),
            run_id=e.get("TP_RUN_ID", ""),
            phase=e.get("TP_PHASE", "plan"),  # type: ignore[arg-type]
            backend=e.get("TP_BACKEND", "tofu"),  # type: ignore[arg-type]
            version=e.get("TP_VERSION", ""),
            workspace_id=e.get("TP_WORKSPACE_ID", ""),
            working_dir=e.get("TP_WORKING_DIR", ""),
            target_addrs=_json_list("TP_TARGET_ADDRS"),
            replace_addrs=_json_list("TP_REPLACE_ADDRS"),
            var_files=_json_list("TP_VAR_FILES"),
            refresh=_bool("TP_REFRESH", default=True),
            refresh_only=_bool("TP_REFRESH_ONLY"),
            allow_empty_apply=_bool("TP_ALLOW_EMPTY_APPLY"),
            destroy=_bool("TP_DESTROY"),
            plan_only=_bool("TP_PLAN_ONLY"),
            setup_script=e.get("TP_SETUP_SCRIPT", ""),
            termination_grace_period_seconds=_int("TP_TERMINATION_GRACE", 120),
            upload_timeout_seconds=_int("TP_UPLOAD_TIMEOUT", 60),
            download_retries=_int("TP_DOWNLOAD_RETRIES", 3),
            download_retry_delay_seconds=_int("TP_DOWNLOAD_RETRY_DELAY", 5),
            os=os_name,
            arch=arch,
        )

    @property
    def has_api(self) -> bool:
        """Whether API-backed phases (binary cache, artifact uploads,
        state download) are reachable. False in degenerate test/dev
        invocations that run the entrypoint with no listener context."""
        return bool(self.api_url) and bool(self.run_id)

    @property
    def auth_header(self) -> str:
        return f"Authorization: Bearer {self.auth_token}"
