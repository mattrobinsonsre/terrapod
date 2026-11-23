"""Raising a configure's host-key anchor (#2592).

Three claims, and the one worth naming is the second: the file must land on the
volume the EE SHARES, because `ssh` runs in the EE and the orchestrator's own
`/tmp` is a separate `emptyDir` that the EE does not mount. A trust anchor
written there is rendered perfectly and invisible to the only process that
enforces it, so every host is refused for a reason nothing in the log names.

The others are that the anchor reaches `ssh` as `-o UserKnownHostsFile=` ahead
of ansible's own default -- OpenSSH takes the FIRST value for each parameter, so
position is the mechanism -- and that the whole thing is gated on the workspace
having declared keys, because an anchor pointed at an empty file refuses
everything.
"""

from __future__ import annotations

import json
import os
import stat

import httpx
import pytest

from terrapod.runner.phases import configure_known_hosts
from terrapod.runner.phases.configure_inventory import RENDERED_NAME as INVENTORY_NAME
from terrapod.runner.phases.configure_inventory import document_to_source
from terrapod.runner.phases.configure_known_hosts import (
    ANSIBLE_DEFAULT_SSH_ARGS,
    RENDERED_NAME,
    HostKeyAnchorUnavailable,
    materialise,
)

ED25519 = "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
ED25519_OTHER = "AAAAC3NzaC1lZDI1NTE5AAAAIAfuCHKVTjquxvt6CM6tdG4SLp1Btn/nOeHHE5UOzRdf"


@pytest.fixture(autouse=True)
def _shared_known_hosts_dir(monkeypatch, tmp_path):
    """Point the shared-volume directory somewhere a test can write.

    The default is `/tp/known-hosts`, which exists only inside a runner Job --
    so without this every test that materialises fails
    `OSError: Read-only file system: '/tp'`, which reads as a defect in the
    module rather than as the absence of a Job. The test that asserts WHERE the
    file goes sets this itself.
    """
    monkeypatch.setenv("TP_EE_KNOWN_HOSTS_DIR", str(tmp_path / "tp" / "known-hosts"))


@pytest.fixture
def cfg():
    from terrapod.runner.runner_config import RunnerConfig

    return RunnerConfig.from_env(
        {
            "TP_RUN_ID": "run-1",
            "TP_PHASE": "configure",
            "TP_WORKSPACE_ID": "ws-abc",
            "TP_API_URL": "http://api",
            "TP_AUTH_TOKEN": "t",
        }
    )


def keys_body(hosts: dict[str, list[dict]]) -> dict:
    """The route's JSON:API collection, in the shape it actually serves.

    `meta.pagination` included because the phase CHECKS it -- a body without it
    would exercise a different branch from the one production takes.
    """
    data = [
        {
            "type": "inventory-host-keys-by-host",
            "id": name,
            "attributes": {"keys": rows},
        }
        for name, rows in sorted(hosts.items())
    ]
    return {
        "data": data,
        "meta": {
            "pagination": {
                "current-page": 1,
                "page-size": len(data),
                "total-count": len(data),
                "total-pages": 1,
            }
        },
    }


def key(key_type: str = "ssh-ed25519", public_key: str = ED25519, marker: str | None = None):
    return {"key-type": key_type, "public-key": public_key, "marker": marker}


def client_for(*, status: int = 200, body: dict | None = None, boom: bool = False):
    """An `httpx.Client` answering the host-keys route from a transport stub.

    A real client over a stub transport rather than a mock, so the phase's own
    `client.get`, header handling and JSON decoding all run. A mock would let a
    change to any of those pass.
    """
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if boom:
            raise httpx.ConnectError("no route to host", request=request)
        return httpx.Response(status, json=body if body is not None else {})

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=1.0)
    client.terrapod_calls = calls  # type: ignore[attr-defined]
    return client


def inventory_at(tmp_path, document: dict):
    """The rendered `-i` list for `document`, written as the real phase writes it.

    Rendered through `document_to_source` rather than hand-written JSON: the
    anchor reads that file back, so a hand-written fixture would pin the anchor
    to a shape the renderer might no longer produce.
    """
    path = tmp_path / INVENTORY_NAME
    path.write_text(document_to_source(document))
    return [path]


DOCUMENT = {
    "hosts": {
        "web1": {"ansible_host": "10.0.0.11"},
        "web2": {},
        "db1": {"ansible_host": "10.0.0.21", "ansible_port": 2222},
    },
    "groups": {"webservers": ["web1", "web2"]},
    "children": {},
}


class TestTheGate:
    def test_a_workspace_with_no_keys_changes_nothing(self, cfg, tmp_path):
        """No file, no environment, and therefore host-key checking exactly as
        it was before this phase existed.

        This is the load-bearing negative: anchoring a workspace that declared
        nothing would point ssh at an empty known_hosts under
        StrictHostKeyChecking=yes, which refuses every host in every configure.
        """
        client = client_for(body=keys_body({}))
        env = materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)
        assert env == {}
        assert not (configure_known_hosts.known_hosts_dir() / RENDERED_NAME).exists()

    def test_a_host_listed_with_an_empty_key_list_is_not_an_anchor(self, cfg, tmp_path):
        """Belt and braces against a future route shape: a truthy map of empty
        lists would take the anchored branch and render no lines, which is the
        empty-file failure by a longer route."""
        client = client_for(body=keys_body({"web1": []}))
        assert materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client) == {}

    def test_an_api_that_does_not_serve_the_route_changes_nothing(self, cfg, tmp_path):
        """A 404 is an API with no such table, so it has no approved keys and
        there is nothing to enforce. Refusing would break a legitimate skew --
        a runner image newer than the control plane, which the support policy
        allows to N-2."""
        client = client_for(status=404)
        assert materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client) == {}


class TestTheRequest:
    """How the keys are read, which is the one thing a stub can hide.

    `runs/next` serves `workspace-id` as `ws-<uuid>` -- the typed form -- so a
    URL built by prefixing again would ask for `ws-ws-<uuid>` and get a 404,
    which this phase reads as "this API does not serve approved keys" and
    answers by raising no anchor at all. Silently unverified, from one string.
    """

    @pytest.mark.parametrize("given", ["ws-abc", "abc"])
    def test_the_workspace_id_is_normalised_either_way(self, tmp_path, given):
        from dataclasses import replace

        from terrapod.runner.runner_config import RunnerConfig

        base = RunnerConfig.from_env(
            {
                "TP_RUN_ID": "run-1",
                "TP_PHASE": "configure",
                "TP_WORKSPACE_ID": given,
                "TP_API_URL": "http://api",
                "TP_AUTH_TOKEN": "t",
            }
        )
        client = client_for(body=keys_body({"web1": [key()]}))
        materialise(replace(base), inventory=inventory_at(tmp_path, DOCUMENT), client=client)
        url = str(client.terrapod_calls[0].url)
        assert url == "http://api/api/terrapod/v1/workspaces/ws-abc/inventory/host-keys", url

    def test_it_is_read_unpaged(self, cfg, tmp_path):
        """The route returns the whole list when no paging params are given, and
        the count is checked against `meta` so a future bounded default fails
        rather than anchoring only the first page."""
        client = client_for(body=keys_body({"web1": [key()]}))
        materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)
        assert client.terrapod_calls[0].url.params == httpx.QueryParams()

    def test_it_carries_the_runner_token(self, cfg, tmp_path):
        """The route requires `inventory:read`, which a runner token holds
        implicitly on its own run's workspace and unphased -- so this works on a
        plan as well as an apply-then-configure."""
        client = client_for(body=keys_body({"web1": [key()]}))
        materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)
        assert client.terrapod_calls[0].headers["Authorization"] == "Bearer t"


class TestWhereTheFileGoes:
    def test_it_is_written_to_the_shared_volume_and_not_tmp(self, cfg, tmp_path, monkeypatch):
        """Asserted through `TP_EE_KNOWN_HOSTS_DIR`, which is what the Job sets,
        so the assertion is about the contract rather than about a constant.

        The negative half matters as much: `/tmp` is an `emptyDir` mounted into
        the ORCHESTRATOR alone, so a file there is unreachable by the ssh that
        has to read it.
        """
        shared = tmp_path / "tp" / "known-hosts"
        monkeypatch.setenv("TP_EE_KNOWN_HOSTS_DIR", str(shared))
        client = client_for(body=keys_body({"web1": [key()]}))
        env = materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)

        written = shared / RENDERED_NAME
        assert written.exists()
        assert str(written) in env["ANSIBLE_SSH_ARGS"]
        assert "/tmp/" not in env["ANSIBLE_SSH_ARGS"]

    def test_it_is_owner_only(self, cfg, tmp_path):
        """`0600`, which is correct because the EE's `runAsUser` is pinned to the
        orchestrator's own uid (#1928) -- so group-readable would be a wider
        grant than the work needs.

        Set at CREATION rather than chmod-ed after, so the assertion is also
        that there was no `0644` window.
        """
        client = client_for(body=keys_body({"web1": [key()]}))
        materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)
        path = configure_known_hosts.known_hosts_dir() / RENDERED_NAME
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_the_directory_is_created_when_no_job_made_it(self, cfg, tmp_path, monkeypatch):
        """In a Job the init container has; in a runner-tier test nothing has."""
        monkeypatch.setenv("TP_EE_KNOWN_HOSTS_DIR", str(tmp_path / "deep" / "nested" / "kh"))
        client = client_for(body=keys_body({"web1": [key()]}))
        materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)
        assert (tmp_path / "deep" / "nested" / "kh" / RENDERED_NAME).exists()


class TestTheSshArguments:
    def test_the_anchor_comes_first(self, cfg, tmp_path):
        """The whole mechanism. OpenSSH takes the FIRST value for each
        parameter -- measured with controls both ways on two OpenSSH versions --
        so an operator's later `UserKnownHostsFile`, from wherever it arrives,
        is read and discarded. Emitting ours last would silently invert that.
        """
        client = client_for(body=keys_body({"web1": [key()]}))
        env = materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)
        args = env["ANSIBLE_SSH_ARGS"]
        assert args.index("UserKnownHostsFile") < args.index("ControlMaster")

    def test_strict_host_key_checking_is_stated(self, cfg, tmp_path):
        """Rather than left to OpenSSH's own `ask`, which refuses
        non-interactively only by accident of there being no tty -- and which
        loses to an `ansible_host_key_checking: false` that `yes` beats on
        position."""
        client = client_for(body=keys_body({"web1": [key()]}))
        env = materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)
        args = env["ANSIBLE_SSH_ARGS"]
        assert "-o StrictHostKeyChecking=yes" in args
        assert args.index("StrictHostKeyChecking") < args.index("ControlMaster")

    def test_the_anchor_keeps_ansibles_own_default(self, cfg, tmp_path):
        """Forcing `ANSIBLE_SSH_ARGS` REPLACES the option rather than adding to
        it, so dropping ansible's default would silently disable SSH
        multiplexing and compression -- one connection per task instead of one
        per host, which on a large fleet is the difference between a configure
        that finishes and one that does not."""
        client = client_for(body=keys_body({"web1": [key()]}))
        env = materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)
        assert env["ANSIBLE_SSH_ARGS"].endswith(ANSIBLE_DEFAULT_SSH_ARGS)
        assert ANSIBLE_DEFAULT_SSH_ARGS == "-C -o ControlMaster=auto -o ControlPersist=60s"


class TestTheRenderedAnchor:
    def test_a_host_is_keyed_on_its_connect_address(self, cfg, tmp_path):
        """Not its inventory name. ansible hands ssh `ansible_host`, so a file
        keyed on `web1` while ssh asks about `10.0.0.11` matches nothing and the
        host is refused as though its key had changed."""
        client = client_for(body=keys_body({"web1": [key()]}))
        materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)
        content = (configure_known_hosts.known_hosts_dir() / RENDERED_NAME).read_text()
        assert content.split(" ")[0].split(",")[0] == "10.0.0.11"
        assert "web1" in content.split(" ")[0]

    def test_a_non_default_port_is_bracketed(self, cfg, tmp_path):
        client = client_for(body=keys_body({"db1": [key()]}))
        materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)
        content = (configure_known_hosts.known_hosts_dir() / RENDERED_NAME).read_text()
        assert "[10.0.0.21]:2222" in content

    def test_a_host_with_no_ansible_host_is_keyed_on_its_name(self, cfg, tmp_path):
        client = client_for(body=keys_body({"web2": [key()]}))
        materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)
        content = (configure_known_hosts.known_hosts_dir() / RENDERED_NAME).read_text()
        assert content.startswith("web2 ssh-ed25519 ")

    def test_every_key_of_every_host_is_a_line(self, cfg, tmp_path):
        client = client_for(
            body=keys_body(
                {
                    "web1": [key(), key(public_key=ED25519_OTHER, marker="revoked")],
                    "web2": [key()],
                }
            )
        )
        materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)
        content = (configure_known_hosts.known_hosts_dir() / RENDERED_NAME).read_text()
        assert len(content.strip().split("\n")) == 3
        assert sum(line.startswith("@revoked ") for line in content.strip().split("\n")) == 1

    def test_a_key_that_cannot_be_rendered_refuses_the_run(self, cfg, tmp_path):
        """Rather than being skipped. A skipped key leaves a host an operator
        can SEE pinned and which OpenSSH refuses, which reads as a key
        mismatch; and a field carrying a newline would write lines for hosts
        nobody declared."""
        forged = f"{ED25519}\nevil.example.com ssh-ed25519 {ED25519_OTHER}"
        client = client_for(body=keys_body({"web1": [key(public_key=forged)]}))
        with pytest.raises(HostKeyAnchorUnavailable, match="known_hosts"):
            materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)


class TestTheConflictingVariable:
    def test_ansible_ssh_args_refuses_the_run(self, cfg, tmp_path):
        """The one channel no mechanism beats: it sets the same option from a
        higher-precedence source, so it REPLACES the anchor rather than
        arriving after it -- measured, that host's argv carried the operator's
        path and no trace of ours.

        So the choice is between refusing and running a configure that reports
        success having verified nothing, which is what this phase exists to make
        impossible.
        """
        document = {
            "hosts": {"web1": {"ansible_host": "10.0.0.11", "ansible_ssh_args": "-o X=y"}},
            "groups": {},
            "children": {},
        }
        client = client_for(body=keys_body({"web1": [key()]}))
        with pytest.raises(HostKeyAnchorUnavailable, match="ansible_ssh_args"):
            materialise(cfg, inventory=inventory_at(tmp_path, document), client=client)

    def test_the_refusal_names_the_hosts(self, cfg, tmp_path):
        """A count tells an operator nothing: the fix is to edit a particular
        host's or group's variables."""
        document = {
            "hosts": {
                "web1": {"ansible_ssh_args": "-o X=y"},
                "web2": {"ansible_ssh_args": "-o X=y"},
                "db1": {},
            },
            "groups": {},
            "children": {},
        }
        client = client_for(body=keys_body({"web1": [key()]}))
        with pytest.raises(HostKeyAnchorUnavailable) as caught:
            materialise(cfg, inventory=inventory_at(tmp_path, document), client=client)
        assert "web1" in str(caught.value)
        assert "web2" in str(caught.value)
        assert "db1" not in str(caught.value)

    def test_it_is_not_refused_when_the_workspace_pins_nothing(self, cfg, tmp_path):
        """Narrow on purpose. A workspace with no approved keys has no anchor to
        displace, so `ansible_ssh_args` stays its own business -- refusing it
        would be a gratuitous break for every estate already using it."""
        document = {
            "hosts": {"web1": {"ansible_ssh_args": "-o X=y"}},
            "groups": {},
            "children": {},
        }
        client = client_for(body=keys_body({}))
        assert materialise(cfg, inventory=inventory_at(tmp_path, document), client=client) == {}

    def test_ssh_arguments_that_are_not_about_host_keys_are_untouched(self, cfg, tmp_path):
        """Scoped to the two OPTIONS, not to the variables.

        A `ProxyCommand`, an `IdentityFile` and a `ServerAliveInterval` are the
        overwhelmingly common contents of these variables, they all still reach
        ssh, and refusing them would be the noisy-gate failure -- a gate most of
        whose findings are correct configurations teaches people to silence it.
        """
        document = {
            "hosts": {
                "web1": {
                    "ansible_ssh_common_args": "-o ProxyCommand=nc %h %p",
                    "ansible_ssh_extra_args": "-o ServerAliveInterval=30",
                }
            },
            "groups": {},
            "children": {},
        }
        client = client_for(body=keys_body({"web1": [key()]}))
        env = materialise(cfg, inventory=inventory_at(tmp_path, document), client=client)
        assert "UserKnownHostsFile" in env["ANSIBLE_SSH_ARGS"]

    @pytest.mark.parametrize(
        "variables",
        [
            {"ansible_ssh_common_args": "-o StrictHostKeyChecking=no"},
            {"ansible_ssh_common_args": "-o UserKnownHostsFile=/dev/null"},
            {"ansible_ssh_extra_args": "-o StrictHostKeyChecking=no"},
            {"ansible_host_key_checking": False},
            {"ansible_ssh_host_key_checking": False},
        ],
    )
    def test_any_other_host_key_statement_is_also_refused(self, cfg, tmp_path, variables):
        """Even though the anchor BEATS all of these on position.

        The question is not whether the run would be secure -- it would -- but
        whether a setting an operator can see is quietly doing nothing. Letting
        these through would leave an inventory that says verification is off and
        runs that enforce it.
        """
        document = {"hosts": {"web1": variables}, "groups": {}, "children": {}}
        client = client_for(body=keys_body({"web1": [key()]}))
        with pytest.raises(HostKeyAnchorUnavailable, match="contradict"):
            materialise(cfg, inventory=inventory_at(tmp_path, document), client=client)

    @pytest.mark.parametrize("spelling", ["false", "False", "no", "off", "0", " false "])
    def test_a_stringified_false_is_caught_too(self, cfg, tmp_path, spelling):
        """An inventory variable reaches the resolved document UNCOERCED --
        ansible's config layer coerces at `get_option` time, not at inventory
        parse time -- so `ansible_host_key_checking: "false"` arrives as a
        string. An `is False` check alone lets through the spelling an operator
        is most likely to write, leaving their setting silently inert."""
        document = {
            "hosts": {"web1": {"ansible_host_key_checking": spelling}},
            "groups": {},
            "children": {},
        }
        client = client_for(body=keys_body({"web1": [key()]}))
        with pytest.raises(HostKeyAnchorUnavailable, match="contradict"):
            materialise(cfg, inventory=inventory_at(tmp_path, document), client=client)

    @pytest.mark.parametrize("spelling", ["true", "True", "yes", "1", "on"])
    def test_a_stringified_true_is_not_a_contradiction(self, cfg, tmp_path, spelling):
        """The other direction, which the false-spelling list must not swallow:
        a host that states the default explicitly agrees with the anchor."""
        document = {
            "hosts": {"web1": {"ansible_host_key_checking": spelling}},
            "groups": {},
            "children": {},
        }
        client = client_for(body=keys_body({"web1": [key()]}))
        env = materialise(cfg, inventory=inventory_at(tmp_path, document), client=client)
        assert "UserKnownHostsFile" in env["ANSIBLE_SSH_ARGS"]

    def test_host_key_checking_left_ON_is_not_a_contradiction(self, cfg, tmp_path):
        """Only `False` contradicts. `True` agrees with the anchor, and an
        inventory that states the default explicitly is ordinary."""
        document = {
            "hosts": {"web1": {"ansible_host_key_checking": True}},
            "groups": {},
            "children": {},
        }
        client = client_for(body=keys_body({"web1": [key()]}))
        env = materialise(cfg, inventory=inventory_at(tmp_path, document), client=client)
        assert "UserKnownHostsFile" in env["ANSIBLE_SSH_ARGS"]

    def test_the_platform_relaxation_is_refused_and_named(self, cfg, tmp_path):
        """The sharpest case, and Terrapod's own.

        `ansible_host_key_checking = False` on the workspace reaches the runner
        as `ansible_ssh_common_args` on the `all` group (#2318), which collapses
        into every host's effective variables. Overriding it silently would
        leave the workspace page saying verification is relaxed while every run
        enforced it -- a lie in the product's own interface, which is worse than
        a failed run. So the message points at the setting, not only at the
        variable, because the operator never wrote that variable.
        """
        from terrapod.services.inventory_resolve import (
            _HOST_KEY_VAR,
            _RELAXED_HOST_KEY_ARGS,
        )

        document = {
            "hosts": {"web1": {_HOST_KEY_VAR: _RELAXED_HOST_KEY_ARGS}},
            "groups": {},
            "children": {},
        }
        client = client_for(body=keys_body({"web1": [key()]}))
        with pytest.raises(HostKeyAnchorUnavailable) as caught:
            materialise(cfg, inventory=inventory_at(tmp_path, document), client=client)
        assert "ansible_host_key_checking" in str(caught.value), (
            "the refusal must name the workspace SETTING, because that is where the "
            "variable came from and the operator cannot find it in their inventory"
        )

    def test_the_two_anchored_options_are_the_ones_openssh_decides(self):
        """The constant is the subject of the rule, so it is pinned.

        Widening it would refuse ordinary ssh arguments; narrowing it would let
        a statement about one of the two options through silently.
        """
        assert configure_known_hosts.ANCHORED_SSH_OPTIONS == (
            "UserKnownHostsFile",
            "StrictHostKeyChecking",
        )
        # And both appear in the anchor, which is why they are the anchored ones.
        anchor = configure_known_hosts.ssh_args(__import__("pathlib").Path("/x"))
        assert all(option in anchor for option in configure_known_hosts.ANCHORED_SSH_OPTIONS)


class TestItFailsClosed:
    def test_an_unreachable_api_refuses_the_run(self, cfg, tmp_path):
        """An unanswered read is indistinguishable from a workspace that
        declared no keys, so continuing would run unverified on exactly the
        fleets somebody pinned."""
        client = client_for(boom=True)
        with pytest.raises(HostKeyAnchorUnavailable, match="approved SSH host keys"):
            materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)

    def test_a_403_refuses_the_run(self, cfg, tmp_path):
        """Unlike a 404. A route that exists and refused this token is a
        scoping defect, and we cannot tell from here whether keys exist."""
        client = client_for(status=403, body={"detail": "no"})
        with pytest.raises(HostKeyAnchorUnavailable):
            materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)

    def test_a_transient_5xx_is_retried(self, cfg, tmp_path, monkeypatch):
        """Bounded retry, per the client-operations rule: a 500 is not an
        answer, and failing the whole configure on one is worse than waiting."""
        monkeypatch.setattr(configure_known_hosts.time, "sleep", lambda _: None)
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(1)
            if len(seen) < 3:
                return httpx.Response(503)
            return httpx.Response(200, json=keys_body({"web1": [key()]}))

        client = httpx.Client(transport=httpx.MockTransport(handler), timeout=1.0)
        env = materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)
        assert len(seen) == 3
        assert "UserKnownHostsFile" in env["ANSIBLE_SSH_ARGS"]

    def test_a_short_page_refuses_the_run(self, cfg, tmp_path):
        """The route is read UNPAGED. If a future bounded default appeared, the
        anchor would hold the first page of hosts and leave the rest to be
        refused as unknown -- so the count is checked against `meta` and a
        mismatch refuses here, where the cause is nameable."""
        body = keys_body({"web1": [key()]})
        body["meta"]["pagination"]["total-count"] = 40
        client = client_for(body=body)
        with pytest.raises(HostKeyAnchorUnavailable, match="unpaged"):
            materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)

    def test_a_body_that_is_not_a_collection_refuses_the_run(self, cfg, tmp_path):
        client = client_for(body={"not": "jsonapi"})
        with pytest.raises(HostKeyAnchorUnavailable, match="JSON:API collection"):
            materialise(cfg, inventory=inventory_at(tmp_path, DOCUMENT), client=client)

    def test_an_unreadable_rendered_inventory_refuses_the_run(self, cfg, tmp_path):
        """Fatal because every host's connect address lives in that file: a
        silently empty map would key the anchor on inventory names and refuse a
        fleet addressed by `ansible_host`."""
        path = tmp_path / INVENTORY_NAME
        path.write_text("this is not json")
        client = client_for(body=keys_body({"web1": [key()]}))
        with pytest.raises(HostKeyAnchorUnavailable, match="could not be read back"):
            materialise(cfg, inventory=[path], client=client)


class TestTheMixedInventoryList:
    def test_a_repository_source_is_not_parsed_as_ours(self, cfg, tmp_path):
        """In the dynamic handoff (#2033) the `-i` list carries the repository's
        own sources -- ini, yaml, or an executable script. Trying to read one as
        our rendered JSON would refuse every such run; they are filtered by
        name instead.
        """
        repo = tmp_path / "hosts.ini"
        repo.write_text("[web]\nweb1 ansible_host=10.0.0.11\n")
        ours = tmp_path / INVENTORY_NAME
        ours.write_text(document_to_source(DOCUMENT))
        client = client_for(body=keys_body({"web1": [key()]}))
        env = materialise(cfg, inventory=[repo, ours], client=client)
        assert "UserKnownHostsFile" in env["ANSIBLE_SSH_ARGS"]
        content = (configure_known_hosts.known_hosts_dir() / RENDERED_NAME).read_text()
        # Read from OUR file, which carries the ansible_host; the ini's copy of
        # the same fact is invisible here and is not needed.
        assert "10.0.0.11" in content

    def test_the_handoffs_declared_file_is_read_too(self, cfg, tmp_path):
        """In the handoff the merged artifact is deliberately NOT in the `-i`
        list -- the declared rows travel alone, under their own name. A host can
        only hold an approved key by being declared, so that file is where every
        pinnable host's variables are, and reading only `RENDERED_NAME` would
        key the anchor on bare inventory names for exactly the runs that have
        a dynamic source.
        """
        from terrapod.runner.phases.configure_inventory import DECLARED_NAME, built_to_source

        built = {"all": {"hosts": {"web1": {"ansible_host": "10.0.0.11"}}}}
        declared = tmp_path / DECLARED_NAME
        declared.write_text(built_to_source(built))
        repo = tmp_path / "hosts.ini"
        repo.write_text("[web]\nweb1\n")

        client = client_for(body=keys_body({"web1": [key()]}))
        materialise(cfg, inventory=[repo, declared], client=client)
        content = (configure_known_hosts.known_hosts_dir() / RENDERED_NAME).read_text()
        assert "10.0.0.11" in content

    def test_the_anchor_reads_what_the_renderer_writes(self, cfg, tmp_path):
        """The coupling, pinned. `document_to_source` serialises with
        `json.dumps` precisely so neither side needs PyYAML (the runner image
        ships none), and this phase reads it back. If that serialisation ever
        changed, every configure in an anchored workspace would refuse -- so the
        two are asserted together rather than assumed.
        """
        from terrapod.runner.phases import configure_inventory

        text = document_to_source(DOCUMENT)
        # It is JSON, which is what the reader relies on...
        assert json.loads(text)
        # ...and the reader recovers every host and its variables from it.
        hosts = configure_inventory.hosts_from_rendered(text)
        assert set(hosts) == {"web1", "web2", "db1"}
        assert hosts["web1"]["ansible_host"] == "10.0.0.11"
        assert hosts["db1"]["ansible_port"] == 2222
        assert hosts["web2"] == {}
