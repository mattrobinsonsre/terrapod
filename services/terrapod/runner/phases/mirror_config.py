"""Write the terraform CLI config (credentials + network mirror + host
redirect for split-networking deployments).

Port of the `# --- Configure provider mirror + credentials ---` and
`# --- Public hostname redirect (split-networking deployments) ---`
blocks of docker/runner-entrypoint.sh.

Output: a `.terraformrc`-style file at /tmp/terraform.rc (configurable
for tests). Exports TF_CLI_CONFIG_FILE pointing at it so terraform /
tofu pick it up.

Three behavioural rules:

  1. Mirror only configured for HTTPS API URLs — terraform / tofu
     refuse network mirrors over plain HTTP.
  2. Terrapod-native registry hosts (the mirror's own host, and the
     public hostname in split-networking deployments) are EXCLUDED
     from the mirror and included in `direct`. The mirror serves the
     network-mirror protocol at /v1/providers/ for third-party
     providers; native hosts serve the registry protocol at
     /api/v2/registry/providers/ and must take the direct path.
  3. When TP_PUBLIC_API_URL differs from TP_API_URL (the listener's
     `publicApiUrl` Helm setting), a `host{}` block redirects the
     public hostname's service-discovery to the internal host. Both
     hostnames get credentials blocks because terraform matches
     credentials by the source-URL hostname, not the discovery target.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class MirrorConfigResult:
    config_path: Path
    mirror_configured: bool
    public_host_redirect: bool


def _host_of(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    return parsed.hostname or ""


def _build_native_patterns(internal_host: str, public_host: str) -> str:
    """Build the comma-separated glob list for the exclude/include
    lists. Both hosts appear; public host is added only when it
    differs from the internal one."""
    patterns = [f'"{internal_host}/*/*"']
    if public_host and public_host != internal_host:
        patterns.append(f'"{public_host}/*/*"')
    return ", ".join(patterns)


def write_terraform_rc(
    *,
    api_url: str,
    auth_token: str,
    public_api_url: str = "",
    config_path: Path = Path("/tmp/terraform.rc"),
) -> MirrorConfigResult:
    """Write /tmp/terraform.rc and return the path + flags describing
    what was written.

    No-op (returns config_path with mirror_configured=False) when
    api_url is empty — the rare degenerate dev invocation.
    """
    internal_host = _host_of(api_url)
    if not internal_host:
        return MirrorConfigResult(
            config_path=config_path,
            mirror_configured=False,
            public_host_redirect=False,
        )

    public_host = _host_of(public_api_url)
    native_patterns = _build_native_patterns(internal_host, public_host)

    parts: list[str] = []

    # Credentials block for the internal mirror host. Every API path
    # this runner hits uses Bearer auth.
    parts.append(f'credentials "{internal_host}" {{\n  token = "{auth_token}"\n}}\n')

    if api_url.startswith("https://"):
        # Provider installation block — network_mirror for third-party
        # providers, direct for Terrapod-native registry hosts.
        parts.append(
            "provider_installation {\n"
            "  network_mirror {\n"
            f'    url = "{api_url}/v1/providers/"\n'
            f"    exclude = [{native_patterns}]\n"
            "  }\n"
            "  direct {\n"
            f"    include = [{native_patterns}]\n"
            "  }\n"
            "}\n"
        )

    # Public-host redirect for split-networking deployments. terraform
    # matches credentials by source-URL hostname, so we have to write
    # a credentials block for the public host even though the discovery
    # services point back at the internal one.
    public_host_redirect = bool(
        public_host and public_host != internal_host and api_url.startswith("https://")
    )
    if public_host_redirect:
        parts.append(
            f'credentials "{public_host}" {{\n  token = "{auth_token}"\n}}\n'
            f'host "{public_host}" {{\n'
            "  services = {\n"
            f'    "modules.v1"   = "{api_url}/api/v2/registry/modules/"\n'
            f'    "providers.v1" = "{api_url}/api/v2/registry/providers/"\n'
            # tfe.v2(.1|.2) point at the same /api/v2/ base; without
            # these, `data "terraform_remote_state" { backend = "remote" }`
            # against the public host gives up with "Host X does not
            # provide a tfe service".
            f'    "tfe.v2"       = "{api_url}/api/v2/"\n'
            f'    "tfe.v2.1"     = "{api_url}/api/v2/"\n'
            f'    "tfe.v2.2"     = "{api_url}/api/v2/"\n'
            "  }\n"
            "}\n"
        )

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("".join(parts))
    return MirrorConfigResult(
        config_path=config_path,
        mirror_configured=api_url.startswith("https://"),
        public_host_redirect=public_host_redirect,
    )


def export_env(config_path: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """Return the env overrides the caller should apply (or pass to
    subprocess) so terraform / tofu use the config + adjusted
    timeouts. Pure: doesn't mutate os.environ.
    """
    out = dict(env) if env is not None else dict(os.environ)
    out["TF_CLI_CONFIG_FILE"] = str(config_path)
    # Increase registry client timeout (default 10s) and enable retries
    # for provider downloads. Covers first-request latency when the
    # provider mirror is caching a binary on-demand from upstream.
    out["TF_REGISTRY_CLIENT_TIMEOUT"] = "30"
    out["TF_PROVIDER_DOWNLOAD_RETRY"] = "3"
    return out
