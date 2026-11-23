"""Native workspace read, update and list against real Postgres (#1554).

The services-api tests pin the wiring with a mocked database; this pins the SQL.
The load-bearing assertion is the engine boundary, a property of the query
rather than of the code's control flow: the native surface returns a Pulumi
workspace while the TFE surface — which a `terraform` CLI talks to — still never
does.

There was a second boundary here, that gating an engine off hid its workspaces.
That switch is withdrawn (#1986), so there is nothing left to hide them and the
tests went with it; `test_engines_endpoint.py` now pins the inverse — that no
setting can shrink the engine list.
"""

import pytest

from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

NATIVE = "/api/v1/workspaces"
TFE_LIST = "/api/v2/organizations/default/workspaces"


async def _create(client, name: str, engine: str) -> str:
    r = await client.post(
        NATIVE,
        json={"data": {"type": "workspaces", "attributes": {"name": name, "engine": engine}}},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _names(resp) -> set[str]:
    assert resp.status_code == 200, resp.text
    return {d["attributes"]["name"] for d in resp.json()["data"]}


class TestTheNativeSurfaceServesEveryEngine:
    async def test_list_read_and_update_a_pulumi_workspace(self, app, client):
        set_auth(app, admin_user())
        pid = await _create(client, "proj::dev", "pulumi")
        await _create(client, "tf-native", "terraform")

        assert {"proj::dev", "tf-native"} <= _names(await client.get(NATIVE, headers=AUTH))
        assert _names(
            await client.get(NATIVE, params={"filter[engine]": "pulumi"}, headers=AUTH)
        ) == {"proj::dev"}

        by_id = await client.get(f"{NATIVE}/{pid}", headers=AUTH)
        by_name = await client.get(f"{NATIVE}/proj::dev", headers=AUTH)
        assert by_id.status_code == by_name.status_code == 200
        assert by_id.json()["data"]["id"] == by_name.json()["data"]["id"] == pid
        assert by_id.json()["data"]["attributes"]["engine"] == "pulumi"

        # A rename follows the Pulumi name rule, which the plain rule rejects.
        upd = await client.patch(
            f"{NATIVE}/{pid}",
            json={
                "data": {
                    "type": "workspaces",
                    "attributes": {"name": "proj::prod", "pulumi-bind-plan": True},
                }
            },
            headers=AUTH,
        )
        assert upd.status_code == 200, upd.text
        attrs = upd.json()["data"]["attributes"]
        assert attrs["name"] == "proj::prod"
        assert attrs["pulumi-bind-plan"] is True
        again = await client.get(f"{NATIVE}/{pid}", headers=AUTH)
        assert again.json()["data"]["attributes"]["pulumi-bind-plan"] is True

    #: Attributes the native surface serves and the compatibility surface does
    #: not, with the reason. Enumerated rather than tolerated: a difference that
    #: nobody had to write down is a difference nobody notices growing.
    NATIVE_ONLY_ATTRS = {
        "pulumi-bind-plan": "a Pulumi concept, and this surface serves Terraform alone (#1911)",
        # The Ansible roots and run defaults (#1929). Native-only for the same
        # reason as the entry above, and deliberately: `/api/tfe/v2` is the
        # frozen surface that `terraform`, `tofu` and `tfci` consume, no
        # invocation of any of them reads these, and
        # `docs/tfe-cli-surface.md` is what decides what belongs there.
        #
        # They are POPPED from the shared literal in `_workspace_json` rather
        # than built conditionally, because the attribute-contract gate
        # AST-reads that literal and cannot see through a `**spread` -- so
        # building it the other way would take the keys out of the snapshot
        # entirely and un-freeze them for the native consumers that DO read
        # them.
        #
        # Note the WRITE path is shared: create and PATCH accept all six on
        # either prefix. That asymmetry follows `pulumi-bind-plan` exactly --
        # the representation is gated on the door, the parsing is not.
        # Which operations the workspace has (#1966). Native-only for the
        # reason above AND for one of their own: the name `operations` was
        # never available on the compatibility surface, because that is TFE's
        # own boolean there for whether the workspace uses remote operations,
        # served from `execution_mode` and pinned by the attribute contract. So
        # these could not have gone on `/api/tfe/v2` under their natural name
        # even if the engine filter allowed it.
        "plan-apply-enabled": (
            "the workspace's operation declaration; `/api/tfe/v2` serves Terraform "
            "alone and already uses `operations` for TFE's own execution-mode "
            "boolean (#1966)"
        ),
        # The playbook allow-list (#2527). Native-only for the same reason as
        # the Ansible roots above: no invocation of `terraform`, `tofu` or
        # `tfci` reads it, so it is not on `docs/tfe-cli-surface.md` and has no
        # business on the frozen surface. It bounds a CONFIGURE, which is not a
        # concept that surface has at all.
        "playbooks": (
            "the per-workspace playbook allow-list; it bounds a configure, which "
            "`/api/tfe/v2` has no concept of, and no CLI invocation reads it (#2527)"
        ),
        # Which of the two operations gets the prominent button (#2479).
        "default-run-kind": (
            "which run kind is primary is a Terrapod UI concept: `terraform` and "
            "`tofu` have no run button, so it could only ever be noise on that "
            "wire -- and a provider reading a workspace back from there would see "
            "a value it had just written come back absent (#2479)"
        ),
        "configure-enabled": (
            "the workspace's operation declaration; `/api/tfe/v2` serves Terraform "
            "alone and already uses `operations` for TFE's own execution-mode "
            "boolean (#1966)"
        ),
        # How a configure starts (#1974). Native-only for both reasons above:
        # they are Ansible triggers, and no `terraform`/`tofu`/`tfci`
        # invocation reads them.
        "auto-configure-enabled": (
            "an Ansible trigger; the TFE surface serves Terraform alone (#1974)"
        ),
        "configure-after-apply": (
            "an Ansible trigger; the TFE surface serves Terraform alone (#1974)"
        ),
        # How wide a DERIVED configure runs (#2229). Native-only for the same
        # two reasons: it governs an Ansible run's `--limit`, and no
        # `terraform`/`tofu`/`tfci` invocation reads it.
        "configure-all-hosts": (
            "an Ansible targeting policy; the TFE surface serves Terraform alone (#2229)"
        ),
        "config-directory": "an Ansible concept; the TFE surface serves Terraform alone (#1929)",
        "inventory-paths": "an Ansible concept; the TFE surface serves Terraform alone (#1929)",
        "default-playbook": "an Ansible concept; the TFE surface serves Terraform alone (#1929)",
        "default-limit": "an Ansible concept; the TFE surface serves Terraform alone (#1929)",
        "hard-limit": (
            "an Ansible concept, and the ceiling its sibling above is the default for; "
            "the TFE surface serves Terraform alone (#2451)"
        ),
        # Whether the repository's dynamic inventory sources are resolved on the
        # runner (#2033). Native-only for the same two reasons as the roots: both
        # govern an Ansible inventory resolution, and no `terraform`/`tofu`/`tfci`
        # invocation reads either.
        "dynamic-inventory-enabled": (
            "an Ansible concept; the TFE surface serves Terraform alone (#2033)"
        ),
        "inventory-scripts-enabled": (
            "an Ansible concept; the TFE surface serves Terraform alone (#2033)"
        ),
        # HOW a configure verifies the SSH host keys of the hosts it targets --
        # `strict`, `accept-new` or `off` (#2592, replacing #2318's boolean).
        # Native-only for the same two reasons as its neighbours: it governs an
        # Ansible connection, and no `terraform`/`tofu`/`tfci` invocation makes one.
        "ssh-host-key-policy": (
            "an Ansible connection setting; the TFE surface serves Terraform alone (#2592)"
        ),
        "storage-location": (
            "a Terrapod deployment concept; no `terraform`/`tofu`/`tfci` invocation reads it"
        ),
    }

    #: Attributes the COMPATIBILITY surface serves and the native one does not.
    #: The other direction, and a far shorter list, because it takes something
    #: rarer: an attribute that is not a setting Terrapod stores at all, kept
    #: alive only because a frozen client reads it by name.
    TFE_ONLY_ATTRS = {
        "auto-apply": (
            "the boolean projection of `auto-apply-mode`, no longer stored (#2503). "
            "`go-tfe` and the cloud backend read the key by name so it stays here, "
            'derived as `mode != "never"`; the native surface carries the mode '
            "alone, because offering both spellings of one setting is what let a "
            "caller contradict itself. The WRITE path still accepts the boolean on "
            "either prefix and maps it onto a mode"
        ),
    }

    async def test_a_terraform_workspace_reads_the_same_on_both_surfaces(self, app, client):
        """The same workspace, described identically wherever the two overlap.

        This asserted byte-identity until #1911, which was true and slightly
        stronger than the property worth holding. The surfaces are *designed* to
        differ — the native one serves every engine — so what matters is that no
        shared attribute disagrees and that every unshared one is there on
        purpose, in whichever direction it goes.

        It was a strict SUPERSET until #2503, and is no longer: `auto-apply` is
        served on the compatibility surface and not natively, because it is a
        projection of a setting rather than a setting. That is one entry and it
        is enumerated, so the next one cannot arrive unnoticed.

        Gated on the surface rather than on the workspace's engine, deliberately:
        gating on the engine would drop the attribute from the native body too,
        where the provider reads it as `Optional+Computed` and a value that turned
        null under an unchanged configuration is a perpetual diff.
        """
        set_auth(app, admin_user())
        tid = await _create(client, "tf-both", "terraform")
        native = await client.get(f"{NATIVE}/{tid}", headers=AUTH)
        tfe = await client.get(f"/api/v2/workspaces/{tid}", headers=AUTH)
        assert native.status_code == tfe.status_code == 200

        n = native.json()["data"]["attributes"]
        t = tfe.json()["data"]["attributes"]
        assert set(t) - set(n) == set(self.TFE_ONLY_ATTRS), (
            f"compatibility-only attributes changed: {set(t) - set(n)}. Add it to "
            f"TFE_ONLY_ATTRS with a reason, or serve it natively too."
        )
        shared = set(t) & set(n)
        assert {k: n[k] for k in shared} == {k: t[k] for k in shared}, (
            "a shared attribute disagrees between the two surfaces"
        )
        assert set(n) - set(t) == set(self.NATIVE_ONLY_ATTRS), (
            f"native-only attributes changed: {set(n) - set(t)}. Add it to "
            f"NATIVE_ONLY_ATTRS with a reason, or stop serving it only there."
        )

    async def test_the_compatibility_only_boolean_agrees_with_the_mode(self, app, client):
        """`auto-apply` is not a setting, so the two surfaces cannot disagree.

        The test above excuses it from the shared-attribute comparison because
        the native surface does not carry it — which would leave the ONE
        attribute that is derived rather than stored as the one nothing checks.
        So it is checked here instead, against the mode the native surface
        serves, for every mode the setting has.
        """
        set_auth(app, admin_user())
        for i, (mode, expected) in enumerate(
            (("never", False), ("always", True), ("create", True), ("create_update", True))
        ):
            tid = await _create(client, f"tf-aa-{i}", "terraform")
            patched = await client.patch(
                f"{NATIVE}/{tid}",
                json={"data": {"attributes": {"auto-apply-mode": mode}}},
                headers=AUTH,
            )
            assert patched.status_code == 200, patched.text
            n = (await client.get(f"{NATIVE}/{tid}", headers=AUTH)).json()["data"]["attributes"]
            t = (await client.get(f"/api/v2/workspaces/{tid}", headers=AUTH)).json()["data"][
                "attributes"
            ]
            assert n["auto-apply-mode"] == mode
            assert "auto-apply" not in n
            assert t["auto-apply"] is expected, mode
            assert t["auto-apply-mode"] == mode


class TestStorageLocationAgainstRealPostgres:
    @pytest.fixture(autouse=True)
    def _locations(self, monkeypatch):
        from terrapod.config import S3Config, StorageBackend, StorageLocationConfig, settings

        eu = StorageLocationConfig(backend=StorageBackend.S3, s3=S3Config(bucket="eu-bucket"))
        monkeypatch.setattr(settings.storage, "locations", {"eu-west": eu})

    async def _make(self, client, name: str, **attrs) -> tuple[str, dict]:
        r = await client.post(
            NATIVE,
            json={"data": {"attributes": {"name": name, **attrs}}},
            headers=AUTH,
        )
        assert r.status_code == 201, r.text
        return r.json()["data"]["id"], r.json()["data"]["attributes"]

    async def test_a_location_round_trips_and_is_fixed(self, app, client):
        set_auth(app, admin_user())
        wid, created = await self._make(client, "loc-eu", **{"storage-location": "eu-west"})
        assert created["storage-location"] == "eu-west"
        read = await client.get(f"{NATIVE}/{wid}", headers=AUTH)
        assert read.json()["data"]["attributes"]["storage-location"] == "eu-west"

        same = await client.patch(
            f"{NATIVE}/{wid}",
            json={"data": {"attributes": {"storage-location": "eu-west"}}},
            headers=AUTH,
        )
        assert same.status_code == 200, same.text
        for other in (None, "default"):
            moved = await client.patch(
                f"{NATIVE}/{wid}",
                json={"data": {"attributes": {"storage-location": other}}},
                headers=AUTH,
            )
            assert moved.status_code == 422, moved.text
        read = await client.get(f"{NATIVE}/{wid}", headers=AUTH)
        assert read.json()["data"]["attributes"]["storage-location"] == "eu-west"

    async def test_absent_is_null_and_both_default_and_unknown_are_refused(self, app, client):
        """`default` is refused rather than collapsed to NULL (#2621).

        It used to be stored as NULL, so a client that sent `"default"` read
        `null` back -- and the Terraform provider writes the server's answer
        into state, so that round trip is "Provider produced inconsistent
        result after apply" on every apply, unfixable from the configuration.
        """
        set_auth(app, admin_user())
        _, absent = await self._make(client, "loc-absent")
        assert absent["storage-location"] is None

        for bad, expected in (
            ("default", "omit the attribute or send null"),
            ("nowhere", "storage-location"),
        ):
            r = await client.post(
                NATIVE,
                json={"data": {"attributes": {"name": f"loc-{bad}", "storage-location": bad}}},
                headers=AUTH,
            )
            assert r.status_code == 422, r.text
            assert expected in r.json()["detail"], r.text


class TestTheSshHostKeyPolicyAgainstRealPostgres:
    """The three-state policy through the real native routes (#2592).

    Three things only a real row can answer: that each of the three stores and
    reads back BYTE-EXACTLY -- the Terraform provider writes the server's answer
    into state, so a normalised value is "Provider produced inconsistent result
    after apply" on every apply -- that an invalid one is refused rather than
    coerced, and the SNAPSHOT ASYMMETRY at the bottom, which is a contrast
    between two live rows and the one thing a mocked test cannot see.
    """

    POLICIES = ("strict", "accept-new", "off")

    async def _make(self, client, name: str, **attrs) -> tuple[str, dict]:
        r = await client.post(
            NATIVE,
            json={"data": {"attributes": {"name": name, **attrs}}},
            headers=AUTH,
        )
        assert r.status_code == 201, r.text
        return r.json()["data"]["id"], r.json()["data"]["attributes"]

    async def _read(self, client, wid: str) -> dict:
        r = await client.get(f"{NATIVE}/{wid}", headers=AUTH)
        assert r.status_code == 200, r.text
        return r.json()["data"]["attributes"]

    @pytest.mark.parametrize("policy", POLICIES)
    async def test_each_policy_round_trips_on_create(self, app, client, policy):
        set_auth(app, admin_user())
        wid, created = await self._make(
            client, f"hk-create-{policy}", **{"ssh-host-key-policy": policy}
        )
        assert created["ssh-host-key-policy"] == policy
        assert (await self._read(client, wid))["ssh-host-key-policy"] == policy

    async def test_absent_is_strict_rather_than_null(self, app, client):
        """The column is `NOT NULL` with a `strict` default, and `strict` is the
        behaviour that already existed: ansible's own `HOST_KEY_CHECKING`
        defaults true, so first contact with an unseen host already failed
        closed."""
        set_auth(app, admin_user())
        _, created = await self._make(client, "hk-absent")
        assert created["ssh-host-key-policy"] == "strict"

    @pytest.mark.parametrize("policy", POLICIES)
    async def test_patch_reaches_every_policy_from_every_other(self, app, client, policy):
        """Including `strict` -> `strict`, which is the no-op a client sending the
        whole document on every write performs constantly."""
        set_auth(app, admin_user())
        wid, _ = await self._make(client, f"hk-patch-{policy}")
        for target in (policy, *self.POLICIES):
            r = await client.patch(
                f"{NATIVE}/{wid}",
                json={"data": {"attributes": {"ssh-host-key-policy": target}}},
                headers=AUTH,
            )
            assert r.status_code == 200, r.text
            assert r.json()["data"]["attributes"]["ssh-host-key-policy"] == target
            assert (await self._read(client, wid))["ssh-host-key-policy"] == target

    @pytest.mark.parametrize(
        ("bad", "expected"),
        [
            ("yes", "ssh-host-key-policy"),
            ("no", "ssh-host-key-policy"),
            ("STRICT", "ssh-host-key-policy"),
            ("", "ssh-host-key-policy"),
            (True, "ansible-host-key-checking"),
            (7, "ssh-host-key-policy"),
        ],
    )
    async def test_an_invalid_policy_is_refused_on_create_and_on_patch(
        self, app, client, bad, expected
    ):
        """A 422 naming the field, not a 500 from the column's length.

        `True` expects the RETIRED attribute in the message instead, because that
        is what a caller sending a boolean is using -- a message listing three
        strings with no hint that the spelling is gone leaves them guessing.
        """
        set_auth(app, admin_user())
        created = await client.post(
            NATIVE,
            json={"data": {"attributes": {"name": f"hk-bad-{bad}", "ssh-host-key-policy": bad}}},
            headers=AUTH,
        )
        assert created.status_code == 422, created.text
        assert expected in created.json()["detail"], created.text

        wid, _ = await self._make(client, f"hk-badpatch-{bad}")
        patched = await client.patch(
            f"{NATIVE}/{wid}",
            json={"data": {"attributes": {"ssh-host-key-policy": bad}}},
            headers=AUTH,
        )
        assert patched.status_code == 422, patched.text
        assert expected in patched.json()["detail"], patched.text
        # And the refused write changed nothing.
        assert (await self._read(client, wid))["ssh-host-key-policy"] == "strict"

    @pytest.mark.parametrize("policy", ["accept-new", "off"])
    async def test_the_fleet_surface_sets_it_too(self, app, client, policy):
        """Bulk update, which is the surface an operator with a fleet uses.

        A fleet reached through a bastion that rewrites host keys is exactly the
        case the setting exists for, and editing it one workspace at a time is
        what this surface exists to avoid.
        """
        import uuid as _uuid

        set_auth(app, admin_user())
        tag = _uuid.uuid4().hex[:8]
        ids = [
            (await self._make(client, f"hk-bulk-{tag}-{i}", labels={"grp": tag}))[0]
            for i in range(2)
        ]

        resp = await client.post(
            "/api/terrapod/v1/workspaces/actions/bulk-update",
            json={
                "dry_run": False,
                "filter": {"labels": {"grp": tag}},
                "update": {"ssh-host-key-policy": policy},
            },
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text

        for wid in ids:
            assert (await self._read(client, wid))["ssh-host-key-policy"] == policy

    async def test_the_fleet_surface_refuses_an_invalid_policy(self, app, client):
        """One rule, shared with the single-workspace write rather than restated
        -- a second copy is how the two surfaces come to disagree about what is
        valid."""
        import uuid as _uuid

        set_auth(app, admin_user())
        tag = _uuid.uuid4().hex[:8]
        wid, _ = await self._make(client, f"hk-bulkbad-{tag}", labels={"grp": tag})

        resp = await client.post(
            "/api/terrapod/v1/workspaces/actions/bulk-update",
            json={
                "dry_run": False,
                "filter": {"labels": {"grp": tag}},
                "update": {"ssh-host-key-policy": "yes"},
            },
            headers=AUTH,
        )
        assert resp.status_code == 422, resp.text
        assert (await self._read(client, wid))["ssh-host-key-policy"] == "strict"

    async def test_a_run_pins_the_policy_and_a_later_workspace_edit_does_not_move_it(
        self, app, client
    ):
        """The asymmetry, which is the point of snapshotting it at all.

        A test of the snapshot alone cannot see this: it would pass identically
        if the run document read the policy LIVE off the workspace, because at
        creation the two agree. The property is a CONTRAST, so both states have
        to exist at once -- the run on `off`, the workspace on `strict`, at the
        same moment.

        Why this direction is the safe one: the policy is a security posture, so
        a relaxation must not be able to weaken a configure already in flight,
        and a tightening must not strand a queued run whose fleet nobody has
        pinned yet. The approved KEYS are read the opposite way, live, so a key
        marked `revoked` while a run sits queued IS refused by that run.
        """
        set_auth(app, admin_user())
        wid, _ = await self._make(client, "hk-snapshot", **{"ssh-host-key-policy": "off"})

        # A non-VCS workspace needs an uploaded configuration version before a
        # run can be queued (#358).
        cv = await client.post(
            f"/api/v2/workspaces/{wid}/configuration-versions",
            json={
                "data": {
                    "type": "configuration-versions",
                    "attributes": {"auto-queue-runs": False},
                }
            },
            headers=AUTH,
        )
        assert cv.status_code == 201, cv.text
        upload = await client.put(
            cv.json()["data"]["attributes"]["upload-url"],
            content=b"placeholder-tarball-for-tests",
            headers={"Content-Type": "application/x-tar"},
        )
        assert upload.status_code in (200, 204), upload.text

        made = await client.post(
            "/api/v1/runs",
            json={
                "data": {
                    "type": "runs",
                    "attributes": {"plan-only": True},
                    "relationships": {"workspace": {"data": {"type": "workspaces", "id": wid}}},
                }
            },
            headers=AUTH,
        )
        assert made.status_code == 201, made.text
        run_id = made.json()["data"]["id"]
        assert made.json()["data"]["attributes"]["ssh-host-key-policy"] == "off", (
            "the run must carry the workspace's policy at creation"
        )

        tightened = await client.patch(
            f"{NATIVE}/{wid}",
            json={"data": {"attributes": {"ssh-host-key-policy": "strict"}}},
            headers=AUTH,
        )
        assert tightened.status_code == 200, tightened.text
        assert (await self._read(client, wid))["ssh-host-key-policy"] == "strict"

        after = await client.get(f"/api/v1/runs/{run_id}", headers=AUTH)
        assert after.status_code == 200, after.text
        assert after.json()["data"]["attributes"]["ssh-host-key-policy"] == "off", (
            "the run's policy is SNAPSHOTTED: editing the workspace after creation "
            "must not change what an already-queued run enforces"
        )


class TestTheTfeSurfaceStaysTerraformOnly:
    async def test_it_never_returns_a_pulumi_workspace(self, app, client):
        set_auth(app, admin_user())
        pid = await _create(client, "proj::hidden", "pulumi")
        assert "proj::hidden" not in _names(await client.get(TFE_LIST, headers=AUTH))
        assert (await client.get(f"/api/v2/workspaces/{pid}", headers=AUTH)).status_code == 404
        patched = await client.patch(
            f"/api/v2/workspaces/{pid}",
            json={"data": {"type": "workspaces", "attributes": {"pulumi-bind-plan": True}}},
            headers=AUTH,
        )
        assert patched.status_code == 404


class TestDeletingAWorkspaceOfAnyEngine:
    """The delete half of the same boundary (#1574).

    `DELETE /api/v1/workspaces/{id}` resolved through the TFE surface's
    Terraform-only lookup, so a Pulumi workspace answered 404 and could not be
    deleted at all — not from the UI's delete button, nor through go-terrapod,
    the provider or MCP. It is the one route that had no native counterpart.
    """

    async def _marker(self, client, name: str) -> dict | None:
        r = await client.get("/api/v1/deleted-workspaces", headers=AUTH)
        assert r.status_code == 200, r.text
        for d in r.json()["data"]:
            if d["attributes"].get("workspace-name") == name:
                return d
        return None

    async def test_a_pulumi_workspace_deletes_and_leaves_a_marker(self, app, client):
        set_auth(app, admin_user())
        pid = await _create(client, "proj::doomed", "pulumi")

        gone = await client.delete(f"{NATIVE}/{pid}", headers=AUTH)
        assert gone.status_code == 204, gone.text

        assert (await client.get(f"{NATIVE}/{pid}", headers=AUTH)).status_code == 404
        assert "proj::doomed" not in _names(await client.get(NATIVE, headers=AUTH))
        # The marker is what makes the deletion recoverable; a delete that
        # skipped it would look identical until someone needed it back.
        assert await self._marker(client, "proj::doomed") is not None

    async def test_a_terraform_workspace_still_deletes(self, app, client):
        # The boundary moved for Pulumi; it must not have moved for Terraform.
        set_auth(app, admin_user())
        tid = await _create(client, "tf-doomed", "terraform")

        gone = await client.delete(f"{NATIVE}/{tid}", headers=AUTH)
        assert gone.status_code == 204, gone.text
        assert (await client.get(f"{NATIVE}/{tid}", headers=AUTH)).status_code == 404

    async def test_it_deletes_by_name_too(self, app, client):
        # A Pulumi workspace is addressed by its project::stack name everywhere
        # a person meets it, so the delete has to accept one.
        set_auth(app, admin_user())
        await _create(client, "proj::by-name", "pulumi")

        gone = await client.delete(f"{NATIVE}/proj::by-name", headers=AUTH)
        assert gone.status_code == 204, gone.text
        assert "proj::by-name" not in _names(await client.get(NATIVE, headers=AUTH))
