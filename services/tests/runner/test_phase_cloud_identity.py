"""The runner's cloud-identity credential phase (#1901).

Three outcomes, and keeping them apart is the point of the phase: the workspace
mints nothing and the run proceeds on the agent pool's identity; it mints and
succeeds; or it mints and fails, which fails the run — because falling through
there does not mean no credentials, it means the pool's, which are broader than
the ones the workspace was deliberately moved off.
"""

import os
import stat

import httpx
import pytest

from terrapod.runner.phases import cloud_identity


def _cfg(**overrides):
    from terrapod.runner.runner_config import RunnerConfig as RC

    base = {
        "TP_API_URL": "https://api.example.com",
        "TP_AUTH_TOKEN": "tok",
        "TP_RUN_ID": "run-1",
        "TP_BACKEND": "tofu",
        "TP_VERSION": "1.12.1",
        "TP_PHASE": "plan",
    }
    base.update(overrides)
    return RC.from_env(env=base)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.example.com")


class TestTheWorkspaceMintsNothing:
    def test_204_returns_no_env_and_writes_no_file(self, tmp_path):
        """The normal posture for most workspaces. It must not be an error and
        it must not leave a file behind that a provider could then read."""
        target = tmp_path / "token"
        env = cloud_identity.run(
            _cfg(),
            token_path=target,
            client=_client(lambda req: httpx.Response(204)),
        )
        assert env == {}
        assert not target.exists()

    def test_no_api_configured_returns_no_env(self, tmp_path):
        env = cloud_identity.run(
            _cfg(TP_API_URL=""),
            token_path=tmp_path / "token",
            client=_client(lambda r: httpx.Response(500)),
        )
        assert env == {}


class TestTheWorkspaceMintsAToken:
    def _ok(self, token="header.body.sig", phase="plan", audiences=("sts.amazonaws.com",)):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/api/terrapod/v1/runs/run-1/cloud-identity-token"
            # The phase is NOT sent: the server takes it from the runner token,
            # which is phase-bound, so a plan-phase Job cannot ask for the apply
            # identity however it frames the request.
            body = request.content or b""
            assert b"phase" not in body, "the phase must not be sent in the request"
            assert request.headers["Authorization"] == "Bearer tok"
            return httpx.Response(
                200,
                json={
                    "token": token,
                    "expires_in": 900,
                    "phase": phase,
                    "audiences": list(audiences),
                },
            )

        return handler

    def test_the_token_is_written_and_the_path_exported(self, tmp_path):
        target = tmp_path / "oidc" / "token"
        env = cloud_identity.run(_cfg(), token_path=target, client=_client(self._ok()))

        assert target.read_text() == "header.body.sig"
        assert env[cloud_identity.TOKEN_FILE_ENV] == str(target)

    def test_the_file_is_private(self, tmp_path):
        """0600, and created that way rather than chmod'd afterwards — this file
        is a bearer credential for the workspace's whole cloud identity until it
        expires, so there must be no world-readable window."""
        target = tmp_path / "token"
        cloud_identity.run(_cfg(), token_path=target, client=_client(self._ok()))
        mode = stat.S_IMODE(os.stat(target).st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_the_phase_is_exported_both_ways(self, tmp_path):
        """`TF_VAR_` too, because HCL cannot otherwise see which phase it is in —
        which is the only way an operator can switch role by phase."""
        env = cloud_identity.run(
            _cfg(TP_PHASE="apply"),
            token_path=tmp_path / "token",
            client=_client(self._ok(phase="apply")),
        )
        assert env[cloud_identity.PHASE_ENV] == "apply"
        assert env[cloud_identity.PHASE_TFVAR_ENV] == "apply"

    def test_no_per_cloud_env_is_set(self, tmp_path):
        """The design, asserted. Terrapod knows no cloud: setting any of these
        would mean picking one, or setting all of them blindly, and the operator's
        own provider configuration is where that belongs."""
        env = cloud_identity.run(_cfg(), token_path=tmp_path / "token", client=_client(self._ok()))
        for forbidden in (
            "AWS_ROLE_ARN",
            "AWS_WEB_IDENTITY_TOKEN_FILE",
            "ARM_OIDC_TOKEN_FILE_PATH",
            "ARM_USE_OIDC",
            "ARM_CLIENT_ID",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ):
            assert forbidden not in env, f"{forbidden} must come from operator configuration"

    def test_the_token_never_reaches_a_log(self, tmp_path, capsys):
        """The runner streams stdout verbatim, so a JWT in a log line is a
        credential in a log line. The audiences are logged; the token is not.

        Asserts on `capsys`, not `caplog`: structlog writes to stdout rather than
        through the stdlib logging capture, so `caplog.text` is EMPTY here and
        "the secret is not in it" would be trivially true however the code
        behaved. The positive assertion below is what proves the stream was
        captured at all.
        """
        secret = "eyJhbGciOiJSUzI1NiJ9.SECRETPAYLOAD.SIGNATURE"
        cloud_identity.run(
            _cfg(),
            token_path=tmp_path / "token",
            client=_client(self._ok(token=secret)),
        )
        out = capsys.readouterr().out
        assert "cloud identity token delivered" in out, "the log line under test was not captured"
        assert "sts.amazonaws.com" in out, "the audiences are meant to be logged"
        assert secret not in out
        assert "SECRETPAYLOAD" not in out


class TestItMintsAndFails:
    """Raise, never fall through. The alternative is a run that succeeds against
    real infrastructure under the agent pool's broader permissions."""

    def test_a_500_raises_after_retrying(self, tmp_path):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(500, text="boom")

        with pytest.raises(cloud_identity.CloudIdentityUnavailable, match="could not be minted"):
            cloud_identity.run(_cfg(), token_path=tmp_path / "token", client=_client(handler))
        assert calls["n"] == 3, "a transient failure is worth retrying"

    def test_a_4xx_is_final_and_not_retried(self, tmp_path):
        """The run is gone, or this token is not scoped to it. Retrying cannot
        change either answer and only delays the failure."""
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(403, text="Runner token required")

        with pytest.raises(cloud_identity.CloudIdentityUnavailable):
            cloud_identity.run(_cfg(), token_path=tmp_path / "token", client=_client(handler))
        assert calls["n"] == 1

    def test_a_connection_error_raises(self, tmp_path):
        def handler(request):
            raise httpx.ConnectError("no route", request=request)

        with pytest.raises(cloud_identity.CloudIdentityUnavailable):
            cloud_identity.run(_cfg(), token_path=tmp_path / "token", client=_client(handler))

    def test_a_200_with_no_token_raises(self, tmp_path):
        """Rather than writing an empty file a provider would then fail on with
        an error naming neither the credential nor the cause."""
        with pytest.raises(cloud_identity.CloudIdentityUnavailable, match="no token"):
            cloud_identity.run(
                _cfg(),
                token_path=tmp_path / "token",
                client=_client(lambda r: httpx.Response(200, json={"expires_in": 900})),
            )

    def test_an_unwritable_path_raises_naming_the_path(self, tmp_path):
        unwritable = tmp_path / "ro"
        unwritable.mkdir()
        os.chmod(unwritable, 0o500)
        try:
            with pytest.raises(cloud_identity.CloudIdentityUnavailable, match="Could not write"):
                cloud_identity.run(
                    _cfg(),
                    token_path=unwritable / "sub" / "token",
                    client=_client(
                        lambda r: httpx.Response(
                            200, json={"token": "a.b.c", "expires_in": 900, "phase": "plan"}
                        )
                    ),
                )
        finally:
            os.chmod(unwritable, 0o700)
