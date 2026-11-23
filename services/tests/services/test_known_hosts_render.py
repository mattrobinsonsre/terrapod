"""Rendering approved host keys as a `known_hosts` file (#2592).

The matching class drives REAL `ssh-keygen -F`, which performs OpenSSH's own
known_hosts lookup, because the claim this module makes is about what OpenSSH
does with the bytes it produces. A shape assertion on the text would pass on a
file OpenSSH matches nothing in: a line keyed on the inventory hostname is
perfectly well-formed and never matched, because ansible hands ssh
`ansible_host`. That is the defect this module exists to prevent and only the
real matcher can see it.

`openssh-client` is installed by `docker/Dockerfile.test-base`, so the skip
below is for a laptop and never for CI.
"""

from __future__ import annotations

import ast
import pathlib
import random
import shutil
import subprocess

import pytest

from terrapod.db.models import SSH_HOST_KEY_MARKERS, SSH_HOST_KEY_TYPES
from terrapod.services import known_hosts_render
from terrapod.services.known_hosts_render import (
    KEY_TYPES,
    MARKERS,
    KnownHostsRenderError,
    known_hosts_patterns,
    render_known_hosts,
)

needs_ssh_keygen = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None,
    reason="no ssh-keygen, so OpenSSH's own known_hosts matcher cannot be driven",
)

#: Real, published host keys -- github.com's, which the runner image already
#: bakes as `saas_known_hosts`. Real material rather than invented blobs so the
#: `ssh-keygen -F` class is matching something OpenSSH will parse as a key.
ED25519 = "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
ECDSA = (
    "AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9"
    "nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg="
)
ED25519_OTHER = "AAAAC3NzaC1lZDI1NTE5AAAAIAfuCHKVTjquxvt6CM6tdG4SLp1Btn/nOeHHE5UOzRdf"


def key(key_type: str = "ssh-ed25519", public_key: str = ED25519, marker: str | None = None):
    """One key in the shape the API serves it, kebab-case attributes and all.

    Carrying `created-at` too, deliberately: the renderer is handed the JSON:API
    attributes untranslated, so a test whose rows hold only the three fields it
    reads would not notice a renderer that choked on the rest.
    """
    return {
        "key-type": key_type,
        "public-key": public_key,
        "marker": marker,
        "created-at": "2026-11-23T10:00:00Z",
        "updated-at": "2026-11-23T10:00:00Z",
    }


class TestTheDuplicatedConstants:
    """The renderer imports nothing, so these are copies. Pinned here.

    Not a style point: the renderer refuses a type or marker outside its own
    set, so a set that drifted BEHIND the model would refuse a key an operator
    successfully declared -- and one that drifted ahead would emit a line
    OpenSSH silently ignores, which is a declared trust anchor matching nothing.
    """

    def test_the_markers_match_the_model(self):
        assert MARKERS == SSH_HOST_KEY_MARKERS

    def test_the_key_types_match_the_model(self):
        assert KEY_TYPES == SSH_HOST_KEY_TYPES

    def test_the_policy_map_matches_the_model(self):
        """The third duplicate, and its drift is the worst of the three.

        The renderer's copy decides what `StrictHostKeyChecking` the run's anchor
        carries. A copy that disagreed with the model would make the UI, the API
        and the provider all report one policy while the run enforced another --
        and the direction that matters is `accept-new` drifting to `yes`, which
        turns every unpinned host unreachable under a setting that says it should
        not.
        """
        from terrapod.db.models import SSH_HOST_KEY_POLICY_TO_OPENSSH as MODEL_MAP
        from terrapod.services.known_hosts_render import SSH_HOST_KEY_POLICY_TO_OPENSSH

        assert SSH_HOST_KEY_POLICY_TO_OPENSSH == MODEL_MAP

    def test_the_policy_default_matches_the_model(self):
        from terrapod.db.models import DEFAULT_SSH_HOST_KEY_POLICY as MODEL_DEFAULT
        from terrapod.services.known_hosts_render import DEFAULT_SSH_HOST_KEY_POLICY

        assert DEFAULT_SSH_HOST_KEY_POLICY == MODEL_DEFAULT

    def test_the_map_covers_every_policy_the_model_offers(self):
        """A floor the equality above cannot give on its own: if both copies lost
        the same entry they would still be equal, and a policy with no OpenSSH
        value raises `KeyError` inside the anchor builder at run time."""
        from terrapod.db.models import SSH_HOST_KEY_POLICIES
        from terrapod.services.known_hosts_render import SSH_HOST_KEY_POLICY_TO_OPENSSH

        assert set(SSH_HOST_KEY_POLICY_TO_OPENSSH) == set(SSH_HOST_KEY_POLICIES)
        assert len(SSH_HOST_KEY_POLICIES) == 3, (
            "three OpenSSH behaviours is the whole reason this is not a boolean"
        )


class TestThePurityBar:
    def test_it_imports_nothing_that_the_runner_image_lacks(self):
        """The reason the duplicates above exist, asserted rather than trusted.

        `job_entrypoint` imports the configure phase at module level, and this
        module with it, so an import the minimal runner image cannot satisfy
        makes every run of every kind fail at startup with
        `ModuleNotFoundError`. It shipped exactly that once, for PyYAML.

        An allowlist rather than a check against what happens to be installed:
        the test runs in the TEST image, which has everything, so asking "does
        this import resolve" would pass on an import the runner image lacks.
        """
        source = pathlib.Path(known_hosts_render.__file__).read_text()
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        assert imported <= {"collections", "typing"}, (
            f"known_hosts_render imports {sorted(imported - {'collections', 'typing'})}. "
            f"It is COPYed into the minimal runner image as one file, so anything "
            f"beyond the standard typing helpers makes job_entrypoint unimportable "
            f"and fails every run of every kind at startup."
        )


class TestThePatterns:
    def test_ansible_host_is_what_openssh_will_be_asked_for(self):
        """The whole reason this function exists -- see its docstring."""
        assert known_hosts_patterns("web1", {"ansible_host": "10.0.0.5"})[0] == "10.0.0.5"

    def test_the_inventory_name_is_kept_beside_it(self):
        """Because it is what ansible hands ssh for a host with no
        `ansible_host`, and naming this host by another of its own names trusts
        nothing further."""
        assert set(known_hosts_patterns("web1", {"ansible_host": "10.0.0.5"})) == {
            "10.0.0.5",
            "web1",
        }

    def test_a_host_with_no_variables_is_its_own_name(self):
        assert known_hosts_patterns("web1") == ["web1"]
        assert known_hosts_patterns("web1", {}) == ["web1"]

    def test_a_non_default_port_adds_the_bracketed_form(self):
        """Measured against a real sshd: a connection to port 2222 is looked up
        as `[host]:2222` and a bare `host` line does not match it."""
        patterns = known_hosts_patterns("web1", {"ansible_host": "10.0.0.5", "ansible_port": 2222})
        assert "[10.0.0.5]:2222" in patterns

    def test_the_bare_form_survives_a_non_default_port(self):
        """Kept so a port set where this cannot see it -- `remote_port` in the
        operator's own `ansible.cfg` -- still matches in the default-port
        direction. Removing it would narrow the file for no gain."""
        patterns = known_hosts_patterns("web1", {"ansible_host": "10.0.0.5", "ansible_port": 2222})
        assert "10.0.0.5" in patterns

    def test_port_22_is_not_bracketed(self):
        """OpenSSH brackets only a non-default port, so a `[host]:22` line would
        match nothing."""
        patterns = known_hosts_patterns("web1", {"ansible_host": "10.0.0.5", "ansible_port": 22})
        assert patterns == ["10.0.0.5", "web1"]
        assert not any("]" in p for p in patterns)

    def test_a_string_port_22_is_not_bracketed_either(self):
        """Inventory variables arrive as strings often enough that an `== 22`
        on the raw value would bracket half the estates that set it."""
        assert known_hosts_patterns("h", {"ansible_port": "22"}) == ["h"]

    def test_there_are_no_duplicates(self):
        """`ansible_host` equal to the inventory name is ordinary, and a
        repeated pattern on a line is noise an operator has to read past."""
        assert known_hosts_patterns("web1", {"ansible_host": "web1"}) == ["web1"]


class TestTheRenderedFile:
    def test_a_line_is_patterns_type_and_key(self):
        out = render_known_hosts({"web1": [key()]})
        assert out == f"web1 ssh-ed25519 {ED25519}\n"

    def test_the_patterns_are_comma_joined(self):
        out = render_known_hosts(
            {"web1": [key()]}, patterns={"web1": ["10.0.0.5", "[10.0.0.5]:2222", "web1"]}
        )
        assert out.split(" ")[0] == "10.0.0.5,[10.0.0.5]:2222,web1"

    def test_a_host_absent_from_patterns_falls_back_to_its_name(self):
        """The only answer available to a caller with no inventory to read, and
        the right one for a host with no `ansible_host`."""
        assert render_known_hosts({"web1": [key()]}, patterns={"other": ["x"]}).startswith("web1 ")

    def test_every_key_type_renders(self):
        """All seven, so a type the model accepts can never be one the renderer
        refuses -- which would refuse a key an operator successfully declared."""
        for key_type in sorted(SSH_HOST_KEY_TYPES):
            out = render_known_hosts({"h": [key(key_type=key_type, public_key=ED25519)]})
            assert out == f"h {key_type} {ED25519}\n"

    def test_a_marker_is_a_line_prefix(self):
        out = render_known_hosts({"h": [key(marker="cert-authority")]})
        assert out == f"@cert-authority h ssh-ed25519 {ED25519}\n"

    def test_a_marker_marks_only_its_own_line(self):
        """The shape the whole model rests on: a marker belongs to a KEY, not to
        an algorithm or a host, so a rotation can revoke the outgoing key while
        the incoming one stays plainly trusted."""
        out = render_known_hosts(
            {
                "h": [
                    key(public_key=ED25519),
                    key(public_key=ED25519_OTHER, marker="revoked"),
                    key(key_type="ecdsa-sha2-nistp256", public_key=ECDSA),
                ]
            }
        )
        lines = out.strip().split("\n")
        assert len(lines) == 3
        marked = [line for line in lines if line.startswith("@")]
        assert len(marked) == 1
        assert marked[0] == f"@revoked h ssh-ed25519 {ED25519_OTHER}"

    def test_every_approved_key_gets_its_own_line(self):
        """Never one per algorithm family. A host holds two keys of one type
        while it is being rotated, and the table's unique constraint is
        `(host, type, key)` precisely so it can."""
        out = render_known_hosts({"h": [key(public_key=ED25519), key(public_key=ED25519_OTHER)]})
        lines = out.strip().split("\n")
        assert len(lines) == 2
        assert {line.split(" ")[2] for line in lines} == {ED25519, ED25519_OTHER}

    def test_several_hosts_are_grouped_and_sorted(self):
        out = render_known_hosts({"web2": [key()], "db1": [key()], "web1": [key()]})
        assert [line.split(" ")[0] for line in out.strip().split("\n")] == ["db1", "web1", "web2"]

    def test_the_order_of_the_input_does_not_change_the_bytes(self):
        """Byte-stability, so the file does not churn between runs of a
        workspace nobody changed."""
        rows = [
            key(public_key=ED25519),
            key(public_key=ED25519_OTHER, marker="revoked"),
            key(key_type="ecdsa-sha2-nistp256", public_key=ECDSA),
        ]
        expected = render_known_hosts({"h": list(rows)})
        for seed in range(8):
            shuffled = list(rows)
            random.Random(seed).shuffle(shuffled)
            assert render_known_hosts({"h": shuffled}) == expected

    def test_empty_input_is_the_empty_string(self):
        """Distinguishable from "this workspace has keys" by truthiness alone,
        which is what the caller's gate keys on: pointing ssh at an EMPTY
        known_hosts under StrictHostKeyChecking=yes refuses every host."""
        assert render_known_hosts({}) == ""

    def test_a_file_ends_with_a_newline(self):
        """A `known_hosts` line is newline-terminated, and OpenSSH ignores a
        final line with no terminator on some versions."""
        assert render_known_hosts({"h": [key()]}).endswith("\n")


class TestTheRefusals:
    def test_a_newline_in_a_public_key_is_refused(self):
        """The one that matters. A field carrying a newline writes a line of the
        caller's choosing into the file that decides what is trusted -- for
        hosts the caller does not own."""
        forged = f"{ED25519}\nevil.example.com ssh-ed25519 {ED25519_OTHER}"
        with pytest.raises(KnownHostsRenderError, match="whitespace"):
            render_known_hosts({"h": [key(public_key=forged)]})

    def test_a_space_in_a_public_key_is_refused(self):
        with pytest.raises(KnownHostsRenderError, match="whitespace"):
            render_known_hosts({"h": [key(public_key="AAAA BBBB")]})

    def test_a_newline_in_a_host_pattern_is_refused(self):
        with pytest.raises(KnownHostsRenderError, match="whitespace"):
            render_known_hosts({"h": [key()]}, patterns={"h": ["good", "bad\nevil.example.com"]})

    def test_a_key_type_openssh_does_not_know_is_refused(self):
        """Rather than emitted: OpenSSH ignores a line whose algorithm it does
        not recognise, so the host would be refused as though it had presented
        an unknown key."""
        with pytest.raises(KnownHostsRenderError, match="does not recognise"):
            render_known_hosts({"h": [key(key_type="ssh-dss")]})

    def test_a_marker_openssh_does_not_know_is_refused(self):
        with pytest.raises(KnownHostsRenderError, match="recognises only"):
            render_known_hosts({"h": [key(marker="quarantined")]})

    def test_a_marker_with_a_leading_at_is_refused(self):
        """`@@revoked` is not a marker OpenSSH knows, and the API stores the
        marker without its `@` precisely so the renderer adds exactly one."""
        with pytest.raises(KnownHostsRenderError, match="recognises only"):
            render_known_hosts({"h": [key(marker="@revoked")]})

    def test_a_missing_public_key_is_refused(self):
        with pytest.raises(KnownHostsRenderError, match="no usable public key"):
            render_known_hosts({"h": [{"key-type": "ssh-ed25519", "public-key": None}]})

    def test_an_empty_marker_is_no_marker(self):
        """`None` and `""` both mean "an ordinary line". The column is nullable
        and a JSON round trip can turn either into the other."""
        assert not render_known_hosts({"h": [key(marker="")]}).startswith("@")
        assert not render_known_hosts({"h": [key(marker=None)]}).startswith("@")


@needs_ssh_keygen
class TestOpenSSHMatchesIt:
    """Driven through `ssh-keygen -F`, which is OpenSSH's own lookup.

    Every case here has a negative control, because `ssh-keygen -F` exits
    non-zero both for "not in this file" and for "this file is unreadable" --
    so a positive result that is never contrasted says nothing about whether
    the matcher ran at all.
    """

    @staticmethod
    def _found(content: str, host: str, tmp_path: pathlib.Path) -> bool:
        path = tmp_path / "known_hosts"
        path.write_text(content)
        return (
            subprocess.run(
                ["ssh-keygen", "-F", host, "-f", str(path)],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )

    def test_a_rendered_host_is_matched_by_its_own_name(self, tmp_path):
        content = render_known_hosts({"web1": [key()]})
        assert self._found(content, "web1", tmp_path)
        assert not self._found(content, "web2", tmp_path)

    def test_a_host_addressed_by_ansible_host_is_matched(self, tmp_path):
        """The defect the patterns exist for: without `ansible_host` in the
        line, ssh asks about `10.0.0.5` and a file keyed on `web1` matches
        nothing."""
        variables = {"ansible_host": "10.0.0.5"}
        content = render_known_hosts(
            {"web1": [key()]}, patterns={"web1": known_hosts_patterns("web1", variables)}
        )
        assert self._found(content, "10.0.0.5", tmp_path)
        assert self._found(content, "web1", tmp_path)

    def test_a_non_default_port_is_matched_in_the_bracketed_form(self, tmp_path):
        variables = {"ansible_host": "10.0.0.9", "ansible_port": 2222}
        content = render_known_hosts(
            {"web1": [key()]}, patterns={"web1": known_hosts_patterns("web1", variables)}
        )
        assert self._found(content, "[10.0.0.9]:2222", tmp_path)

    def test_a_host_the_workspace_did_not_pin_is_not_matched(self, tmp_path):
        """The file is an ALLOW-LIST under StrictHostKeyChecking=yes: a host the
        configure targets and the workspace has not pinned is refused, not
        merely unverified. That is the correct direction and it is why the
        caller names the unpinned hosts in the run log."""
        content = render_known_hosts({"web1": [key()]})
        assert not self._found(content, "db1", tmp_path)

    def test_openssh_can_read_every_key_we_emit(self, tmp_path):
        """A line OpenSSH cannot parse is one it IGNORES, so a malformed render
        is a trust anchor that silently matches nothing rather than an error.

        Through `-l`, which parses each key and prints its fingerprint, because
        `-F` cannot answer this: measured, `ssh-keygen -F h` reports FOUND for
        `h ssh-ed25519 not-base64!!!`. It matches on the host PATTERN alone, so
        every `-F` assertion in this class -- correctly -- says something about
        names and nothing about keys.

        Unmarked lines only, also measured: `-l` lists no fingerprint for a
        `@cert-authority` or `@revoked` line and exits 255 on a file holding
        nothing else. Their semantics were measured end to end against a real
        `sshd` instead -- a `@revoked` key is refused in either line order --
        and the test below covers their matching.
        """
        content = render_known_hosts(
            {
                "h": [
                    key(public_key=ED25519),
                    key(key_type="ecdsa-sha2-nistp256", public_key=ECDSA),
                ]
            }
        )
        path = tmp_path / "known_hosts"
        path.write_text(content)
        result = subprocess.run(
            ["ssh-keygen", "-l", "-f", str(path)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        # Two lines in, two fingerprints out: a key OpenSSH could not read would
        # simply be absent, which a returncode check alone cannot see.
        assert len([line for line in result.stdout.strip().split("\n") if line]) == 2

    def test_a_marked_line_still_matches_its_host(self, tmp_path):
        """A marker must not break the host lookup -- a `@revoked` line that
        matched nothing would make a revoked key an UNKNOWN key, which is the
        one thing the marker exists to prevent."""
        for marker in sorted(MARKERS):
            content = render_known_hosts({"h": [key(marker=marker)]})
            assert self._found(content, "h", tmp_path), marker
