#!/usr/bin/env python3
"""Config-channel contract check (#617).

Parses the **rendered** Helm output through the real Pydantic config models and
asserts the chart ↔ code ↔ chart-test contract:

  1. **No drift** — every key the chart renders into the API ConfigMap
     (config.yaml) is a real field on `Settings`, and every key in the runner
     ConfigMap (runners.yaml) is a real field on `RunnerConfig`. A chart key the
     code doesn't know (typo, stale rename) fails here. Because this reads the
     *rendered YAML* (concrete dicts), there is none of the brittleness of
     grepping Go-template text.
  2. **Coverage** — config-channel keys that were historically inert or are
     central to the contract are present (rate_limit, module_interface, the
     migrated listener settings).
  3. **Env channel** — the rendered API + listener Deployments carry no
     non-sensitive `TERRAPOD_*` env: Deployment env is for secrets
     (`secretKeyRef`) and unavoidable runtime values (Downward API) only.

Usage:
    check_helm_config_contract.py <rendered-manifests.yaml> [profile-label]

Reads a `helm template` multi-doc stream and exits non-zero with a precise
message on any violation. Run once per values profile.
"""

from __future__ import annotations

import sys
import typing

import yaml
from pydantic import BaseModel

from terrapod.config import RunnerConfig, Settings

# TERRAPOD_* env vars allowed on a Deployment: secrets (delivered via
# secretKeyRef; some carry a documented dev-only literal `value:` fallback) plus
# build/runtime values that can't be config-file driven. Everything else is a
# non-sensitive setting that must come from a ConfigMap, not env.
_ENV_ALLOWLIST = {
    "TERRAPOD_VERSION",  # Chart.AppVersion, deploy-time
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
# OIDC client secrets render as TERRAPOD_{NAME}_CLIENT_SECRET (per-provider) —
# always a secretKeyRef, allowed by suffix.
_ENV_ALLOWLIST_SUFFIX = ("_CLIENT_SECRET",)

# Dotted key paths that MUST appear in the rendered ConfigMaps (regression /
# contract spine). rate_limit was wholly inert before #617; registry's
# module_interface block was omitted from the render entirely.
_API_REQUIRED = ["rate_limit", "registry.module_interface"]
_RUNNER_REQUIRED = [
    "listener_name",
    "runner_namespace",
    "max_concurrent",
    "sse_read_timeout",
    "listener_cert_ttl_seconds",
]


def _model_in(annotation) -> tuple[type[BaseModel] | None, bool]:
    """Return (BaseModel subclass inside the annotation, is_list)."""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Union:
        non_none = [a for a in args if a is not type(None)]
        return _model_in(non_none[0]) if len(non_none) == 1 else (None, False)
    if origin in (list, set, tuple):
        if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            return (args[0], True)
        return (None, False)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return (annotation, False)
    return (None, False)


def _walk(data: dict, model: type[BaseModel], path: str = "") -> list[str]:
    """Return dotted paths of rendered keys that are NOT fields on `model`."""
    errs: list[str] = []
    fields = model.model_fields
    if not isinstance(data, dict):
        return errs
    for key, val in data.items():
        if key not in fields:
            errs.append(f"{path}{key}")
            continue
        sub, is_list = _model_in(fields[key].annotation)
        if sub and isinstance(val, dict):
            errs += _walk(val, sub, f"{path}{key}.")
        elif sub and is_list and isinstance(val, list):
            for i, item in enumerate(val):
                errs += _walk(item, sub, f"{path}{key}[{i}].")
    return errs


def _has_path(data: dict, dotted: str) -> bool:
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def _docs(stream: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(stream) if isinstance(d, dict)]


def _configmap_data(docs: list[dict], name_suffix: str, file_key: str) -> dict | None:
    for d in docs:
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith(name_suffix):
            raw = d.get("data", {}).get(file_key, "")
            return yaml.safe_load(raw) or {}
    return None


def _deployment_env_offenders(docs: list[dict]) -> list[str]:
    """Non-sensitive TERRAPOD_* env (literal `value:`) on any Deployment."""
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
                # secretKeyRef / fieldRef env are fine; only literal `value:` is a
                # potential non-sensitive-config-in-env violation.
                if "value" in e:
                    offenders.append(f"{dep}: {name}")
    return offenders


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: check_helm_config_contract.py <rendered.yaml> [profile]", file=sys.stderr)
        return 2
    profile = sys.argv[2] if len(sys.argv) > 2 else sys.argv[1]
    docs = _docs(open(sys.argv[1]).read())

    problems: list[str] = []

    api = _configmap_data(docs, "-api-config", "config.yaml")
    if api is None:
        problems.append("API ConfigMap (-api-config) not rendered")
    else:
        drift = _walk(api, Settings)
        if drift:
            problems.append(f"config.yaml keys not on Settings: {sorted(drift)}")
        for k in _API_REQUIRED:
            if not _has_path(api, k):
                problems.append(f"config.yaml missing required key: {k}")

    runner = _configmap_data(docs, "-runner-config", "runners.yaml")
    if runner is None:
        # runner ConfigMap only renders when listener.enabled — tolerate absence.
        pass
    else:
        drift = _walk(runner, RunnerConfig)
        if drift:
            problems.append(f"runners.yaml keys not on RunnerConfig: {sorted(drift)}")
        for k in _RUNNER_REQUIRED:
            if k not in runner:
                problems.append(f"runners.yaml missing required key: {k}")

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
