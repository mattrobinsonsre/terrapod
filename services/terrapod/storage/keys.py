"""
Key path helpers for object storage.

Provides consistent key naming for all stored artifacts.
All keys are relative to the storage backend's configured prefix.
"""


def state_key(workspace_id: str, version_id: str) -> str:
    """Key for a workspace state version."""
    return f"state/{workspace_id}/{version_id}.tfstate"


def state_backup_key(workspace_id: str, version_id: str) -> str:
    """Key for a backup of a workspace state version."""
    return f"state/{workspace_id}/{version_id}.backup.tfstate"


def plan_log_key(workspace_id: str, run_id: str) -> str:
    """Key for a run's plan log output."""
    return f"logs/{workspace_id}/plans/{run_id}.log"


def apply_log_key(workspace_id: str, run_id: str) -> str:
    """Key for a run's apply log output."""
    return f"logs/{workspace_id}/applies/{run_id}.log"


def plan_output_key(workspace_id: str, run_id: str) -> str:
    """Key for a run's binary plan output (tfplan file)."""
    return f"plans/{workspace_id}/{run_id}.tfplan"


def config_version_key(workspace_id: str, config_version_id: str) -> str:
    """Key for a configuration version archive."""
    return f"config/{workspace_id}/{config_version_id}.tar.gz"


def run_tfvars_key(workspace_id: str, run_id: str) -> str:
    """Key for a generated .tfvars file for a run."""
    return f"runs/{workspace_id}/{run_id}.auto.tfvars"


def policy_set_key(policy_set_id: str, version_id: str) -> str:
    """Key for a policy set bundle."""
    return f"policies/{policy_set_id}/{version_id}.tar.gz"


# --- Module Registry ---


def module_tarball_key(
    org: str, namespace: str, name: str, provider: str, version: str
) -> str:
    """Key for a module version tarball."""
    return f"registry/modules/{org}/{namespace}/{name}/{provider}/{version}.tar.gz"


# --- Provider Registry ---


def provider_binary_key(
    org: str, namespace: str, name: str, version: str, os_: str, arch: str
) -> str:
    """Key for a provider binary zip."""
    return f"registry/providers/{org}/{namespace}/{name}/{version}/{name}_{version}_{os_}_{arch}.zip"


def provider_shasums_key(org: str, namespace: str, name: str, version: str) -> str:
    """Key for a provider version's SHA256SUMS file."""
    return f"registry/providers/{org}/{namespace}/{name}/{version}/SHA256SUMS"


def provider_shasums_sig_key(org: str, namespace: str, name: str, version: str) -> str:
    """Key for a provider version's SHA256SUMS.sig file."""
    return f"registry/providers/{org}/{namespace}/{name}/{version}/SHA256SUMS.sig"


# --- Module Cache ---


def module_cache_key(
    hostname: str, namespace: str, name: str, provider: str, version: str
) -> str:
    """Key for a cached upstream module tarball."""
    return f"cache/modules/{hostname}/{namespace}/{name}/{provider}/{version}.tar.gz"


# --- Provider Cache ---


def provider_cache_key(
    hostname: str, namespace: str, type_: str, version: str, filename: str
) -> str:
    """Key for a cached upstream provider binary."""
    return f"cache/providers/{hostname}/{namespace}/{type_}/{version}/{filename}"


# --- Binary Cache ---


def binary_cache_key(tool: str, version: str, os_: str, arch: str) -> str:
    """Key for a cached terraform/tofu CLI binary."""
    return f"cache/binaries/{tool}/{version}/{os_}_{arch}"
