#!/usr/bin/env python3
"""Config-channel contract check (#617).

Parses the **rendered** Helm output and binds it to the real Pydantic config
models. Run once per values profile (a fresh process each, so the model
construction below validates that profile's rendered config). Asserts:

  1. **Validates** — the rendered config.yaml / runners.yaml actually
     construct `Settings()` / `RunnerConfig()` (full pydantic validation, like
     the API/listener do at startup). Catches type/null errors a key-walk
     misses (e.g. a bare `cors:` → null).
  2. **No drift** — every key the chart renders is a real field on the model.
  3. **Coverage** — contract-spine keys present (rate_limit,
     registry.module_interface, the migrated listener settings).
  4. **Env channel** — rendered Deployments carry no non-sensitive TERRAPOD_*
     env (secrets via secretKeyRef + runtime values only).

Usage:
    check_helm_config_contract.py <rendered-manifests.yaml> [profile-label]
"""

from __future__ import annotations

import os
import sys
import typing

import yaml

# Stub the required secret env so Settings() construction gets past the
# secret fields to the config we actually want to validate. These are NOT read
# from the ConfigMap (they're secretKeyRef) — values here are throwaway.
os.environ.setdefault("TERRAPOD_DATABASE_URL", "postgresql+asyncpg://t:t@db:5432/t")
os.environ.setdefault("TERRAPOD_REDIS_URL", "redis://r:6379")
os.environ.setdefault("TERRAPOD_TOKEN_SIGNING_KEY", "contract-check-stub")

# TERRAPOD_* env vars allowed on a Deployment: secrets (delivered via
# secretKeyRef; some carry a documented dev-only literal `value:` fallback) plus
# build/runtime values that can't be config-file driven.
_ENV_ALLOWLIST = {
    "TERRAPOD_VERSION",
    "TERRAPOD_DATABASE_URL",
    "TERRAPOD_REDIS_URL",
    "TERRAPOD_TOKEN_SIGNING_KEY",
    "TERRAPOD_STORAGE__FILESYSTEM__HMAC_SECRET",
    "TERRAPOD_REGISTRY__SIGNING_KEY",
    "TERRAPOD_NOTIFICATIONS__SMTP__PASSWORD",
    "TERRAPOD_VCS__GITHUB__WEBHOOK_SECRET",
    "TERRAPOD_VCS__GITLAB__WEBHOOK_SECRET",
    "TERRAPOD_AI_SUMMARY__AUTH__API_KEY",
    "TERRAPOD_JOIN_TOKEN",
}
_ENV_ALLOWLIST_SUFFIX = ("_CLIENT_SECRET",)

_API_REQUIRED = ["rate_limit", "registry.module_interface"]
_RUNNER_REQUIRED = [
    "listener_name",
    "runner_namespace",
    "max_concurrent",
    "sse_read_timeout",
    "listener_cert_ttl_seconds",
]


def _docs(stream: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(stream) if isinstance(d, dict)]


def _configmap_raw(docs: list[dict], name_suffix: str, file_key: str) -> str | None:
    for d in docs:
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith(name_suffix):
            return d.get("data", {}).get(file_key)
    return None


def _has_path(data: dict, dotted: str) -> bool:
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def _model_in(annotation):
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Union:
        non_none = [a for a in args if a is not type(None)]
        return _model_in(non_none[0]) if len(non_none) == 1 else (None, False)
    if origin in (list, set, tuple):
        from pydantic import BaseModel

        if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            return (args[0], True)
        return (None, False)
    from pydantic import BaseModel

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return (annotation, False)
    return (None, False)


def _walk(data: dict, model, path: str = "") -> list[str]:
    errs: list[str] = []
    if not isinstance(data, dict):
        return errs
    fields = model.model_fields
    for key, val in data.items():
        if key not in fields:
            errs.append(f"{path}{key}")
            continue
        sub, is_list = _model_in(fields[key].annotation)
        if sub and isinstance(val, dict):
            errs += _walk(val, sub, f"{path}{key}.")
        elif sub and is_list and isinstance(val, list):
            for item in val:
                errs += _walk(item, sub, f"{path}{key}[].")
    return errs


def _deployment_env_offenders(docs: list[dict]) -> list[str]:
    offenders: list[str] = []
    for d in docs:
        if d.get("kind") != "Deployment":
            continue
        dep = d["metadata"]["name"]
        for c in d["spec"]["template"]["spec"].get("containers", []):
            for e in c.get("env", []) or []:
                name = e.get("name", "")
                if not name.startswith("TERRAPOD_"):
                    continue
                if name in _ENV_ALLOWLIST or name.endswith(_ENV_ALLOWLIST_SUFFIX):
                    continue
                if "value" in e:
                    offenders.append(f"{dep}: {name}")
    return offenders


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: check_helm_config_contract.py <rendered.yaml> [profile]", file=sys.stderr)
        return 2
    profile = sys.argv[2] if len(sys.argv) > 2 else sys.argv[1]
    with open(sys.argv[1]) as f:
        docs = _docs(f.read())
    problems: list[str] = []

    api_raw = _configmap_raw(docs, "-api-config", "config.yaml")
    runner_raw = _configmap_raw(docs, "-runner-config", "runners.yaml")

    # 1) VALIDATE: write the rendered config to where the models read it, then
    #    construct them (full pydantic validation, exactly like pod startup).
    #    Importing terrapod.config constructs `settings = Settings()` against the
    #    written config.yaml; RunnerConfig() reads the written runners.yaml.
    os.makedirs("/etc/terrapod", exist_ok=True)
    if api_raw is not None:
        with open("/etc/terrapod/config.yaml", "w") as f:
            f.write(api_raw)
    if runner_raw is not None:
        with open("/etc/terrapod/runners.yaml", "w") as f:
            f.write(runner_raw)

    Settings = RunnerConfig = None
    try:
        import terrapod.config as cfg  # triggers settings = Settings()

        Settings, RunnerConfig = cfg.Settings, cfg.RunnerConfig
        if runner_raw is not None:
            RunnerConfig()  # validate runners.yaml
    except Exception as e:  # pydantic ValidationError or import-time settings build
        problems.append(f"rendered config fails model validation: {type(e).__name__}: {str(e)[:600]}")

    # 2) DRIFT + 3) COVERAGE (only if the models imported cleanly).
    if Settings is not None and api_raw is not None:
        api = yaml.safe_load(api_raw) or {}
        drift = _walk(api, Settings)
        if drift:
            problems.append(f"config.yaml keys not on Settings: {sorted(drift)}")
        for k in _API_REQUIRED:
            if not _has_path(api, k):
                problems.append(f"config.yaml missing required key: {k}")
    if RunnerConfig is not None and runner_raw is not None:
        runner = yaml.safe_load(runner_raw) or {}
        drift = _walk(runner, RunnerConfig)
        if drift:
            problems.append(f"runners.yaml keys not on RunnerConfig: {sorted(drift)}")
        for k in _RUNNER_REQUIRED:
            if k not in runner:
                problems.append(f"runners.yaml missing required key: {k}")

    # 4) ENV CHANNEL
    env_offenders = _deployment_env_offenders(docs)
    if env_offenders:
        problems.append(f"non-sensitive TERRAPOD_* Deployment env: {env_offenders}")

    if problems:
        print(f"[{profile}] config-channel contract FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"[{profile}] config-channel contract OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
