"""The OPA input document built from a Pulumi preview's event log (#1567).

Terraform hands OPA its plan JSON. The Pulumi runner had nothing to hand over,
so policy sets were reported as out of scope for a Pulumi workspace rather than
enforced. This is the document that closes that gap.

**The fixtures here are shaped from Pulumi's own type definitions**, not from
what seemed plausible: `apitype.StepEventMetadata` and `StepEventStateMetadata`
in `sdk/go/common/apitype/events.go`. That matters because the whole design
turns on one line of theirs -- that the engine filters secrets out of the state
it writes to the log -- and a fixture invented to match our assumption would
have proved nothing about it.
"""

import json

from terrapod.runner.phases import pulumi_preview

URN = "urn:pulumi:dev::shop::aws:s3/bucket:Bucket::assets"


def _pre_event(**metadata):
    """A `resourcePreEvent` in the engine's real shape."""
    base = {
        "op": "create",
        "urn": URN,
        "type": "aws:s3/bucket:Bucket",
        "new": {
            "type": "aws:s3/bucket:Bucket",
            "urn": URN,
            "custom": True,
            "id": "",
            "parent": "urn:pulumi:dev::shop::pulumi:pulumi:Stack::shop-dev",
            "provider": "urn:pulumi:dev::shop::pulumi:providers:aws::default_6_0_0::uuid",
            # Engine-filtered before it reaches the log: a property marked
            # secret is replaced with the literal "[secret]" unless the CLI was
            # given --show-secrets, which a preview never is.
            "inputs": {"acl": "public-read", "tags": {"env": "dev"}},
            "outputs": {"arn": "arn:aws:s3:::assets"},
        },
        "diffs": ["acl"],
        "detailedDiff": {"acl": {"kind": "update"}},
    }
    base.update(metadata)
    return {"resourcePreEvent": {"metadata": base}}


def _summary(**changes):
    return {"summaryEvent": {"resourceChanges": changes or {"create": 1, "same": 2}}}


def _log(tmp_path, *events, name="events.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


class TestWhatAPolicyCanRead:
    def test_a_resource_carries_what_a_rule_decides_on(self, tmp_path):
        doc = pulumi_preview.build_policy_input(_log(tmp_path, _pre_event(), _summary()))
        assert doc is not None
        (res,) = doc["resource_changes"]
        assert res["op"] == "create"
        assert res["type"] == "aws:s3/bucket:Bucket"
        assert res["urn"] == URN
        assert res["inputs"] == {"acl": "public-read", "tags": {"env": "dev"}}

    def test_the_name_is_lifted_out_of_the_urn(self, tmp_path):
        # A URN reads urn:pulumi:<stack>::<project>::<type-chain>::<name>, and a
        # policy matching on a resource's name should not have to parse that.
        doc = pulumi_preview.build_policy_input(_log(tmp_path, _pre_event(), _summary()))
        assert doc["resource_changes"][0]["name"] == "assets"

    def test_a_urn_without_segments_yields_an_empty_name(self, tmp_path):
        doc = pulumi_preview.build_policy_input(
            _log(tmp_path, _pre_event(urn="mangled"), _summary())
        )
        assert doc["resource_changes"][0]["name"] == ""

    def test_the_changed_property_paths_are_carried(self, tmp_path):
        # How a policy asks "did anything under `acl` change?" without diffing
        # the two states itself.
        doc = pulumi_preview.build_policy_input(_log(tmp_path, _pre_event(), _summary()))
        res = doc["resource_changes"][0]
        assert res["diffs"] == ["acl"]
        assert res["detailed_diff"] == {"acl": {"kind": "update"}}

    def test_the_summary_and_has_changes_come_across(self, tmp_path):
        doc = pulumi_preview.build_policy_input(
            _log(tmp_path, _pre_event(), _summary(create=2, same=5))
        )
        assert doc["engine"] == "pulumi"
        assert doc["change_summary"] == {"create": 2, "same": 5}
        assert doc["has_changes"] is True

    def test_a_no_op_preview_says_so(self, tmp_path):
        # A real no-op preview emits a pre-event per unchanged resource, so the
        # fixture carries them: an empty log would prove nothing here.
        same = [_pre_event(op="same", urn=f"{URN}-{i}") for i in range(9)]
        doc = pulumi_preview.build_policy_input(_log(tmp_path, *same, _summary(same=9)))
        assert doc["has_changes"] is False
        assert doc["resource_changes"] == []


class TestUnchangedResourcesAreExcluded:
    """`same` steps must not reach a policy (#1567 review).

    The engine emits a `resourcePreEvent` for every resource it walks, not only
    the ones it will touch -- `executeStep` skips only `DiffStep`, so a
    `SameStep` is reported like any other. Carrying those would put a stack's
    whole inventory under a key named `resource_changes`, and a rule matching
    on type and a property would then deny a resource that is not changing:
    a stack non-compliant since before the policy existed could never be
    applied again, for any unrelated change.
    """

    def test_a_same_step_is_not_a_resource_change(self, tmp_path):
        path = _log(
            tmp_path,
            _pre_event(op="same", urn=f"{URN}-untouched"),
            _pre_event(op="create"),
            _summary(create=1, same=1),
        )
        doc = pulumi_preview.build_policy_input(path)
        assert [r["op"] for r in doc["resource_changes"]] == ["create"]
        assert "untouched" not in json.dumps(doc)

    def test_the_docs_example_rule_cannot_deny_an_unchanged_resource(self, tmp_path):
        """The worked example in docs/policies.md has no `op` guard.

        That is deliberate -- a rule should not have to remember one -- and it
        is only safe because unchanged resources never arrive. This pins the
        property the documented rule depends on.
        """
        offending_but_unchanged = _pre_event(
            op="same",
            new={"inputs": {"acl": "public-read"}, "parent": "", "provider": ""},
        )
        doc = pulumi_preview.build_policy_input(
            _log(tmp_path, offending_but_unchanged, _summary(same=1))
        )
        matches = [
            r
            for r in doc["resource_changes"]
            if r["type"] == "aws:s3/bucket:Bucket" and r["inputs"].get("acl") == "public-read"
        ]
        assert matches == []

    def test_the_summary_still_counts_what_was_excluded(self, tmp_path):
        # A policy that wants to reason about the unchanged portion has no
        # per-resource view of it, but the counts are still honest.
        path = _log(
            tmp_path,
            _pre_event(op="same"),
            _pre_event(op="update"),
            _summary(update=1, same=1),
        )
        doc = pulumi_preview.build_policy_input(path)
        assert doc["change_summary"] == {"update": 1, "same": 1}
        assert len(doc["resource_changes"]) == 1


class TestWhatItDeliberatelyOmits:
    def test_outputs_are_not_carried(self, tmp_path):
        """The provider's complete returned state, not anything the program said.

        It adds little to a policy decision and a great deal to the document's
        size, so it is left out -- and leaving it out is the kind of thing a
        later refactor helpfully "fixes", hence the test.
        """
        doc = pulumi_preview.build_policy_input(_log(tmp_path, _pre_event(), _summary()))
        assert "outputs" not in doc["resource_changes"][0]
        assert "arn:aws:s3:::assets" not in json.dumps(doc)

    def test_the_old_state_is_ignored_when_there_is_a_new_one(self, tmp_path):
        # For a create or an update the policy decides about what is being
        # asked for, not what was there. (A delete is the exception — it has no
        # new state at all; see TestADeleteStep.)
        event = _pre_event(old={"inputs": {"acl": "private"}})
        doc = pulumi_preview.build_policy_input(_log(tmp_path, event, _summary()))
        assert "old" not in doc["resource_changes"][0]
        assert "private" not in json.dumps(doc)


class TestADeleteStep:
    """A delete carries no new state, and reading only `new` lies (#1567 review).

    `DeleteStep.New()` returns nil, so `metadata.new` is null on a delete.
    Taking the fields from `new` alone would report `inputs: {}` and
    `protect: false` for every deletion — making the most obvious Pulumi policy
    there is, "do not delete a protected resource", impossible to write while
    appearing to work.
    """

    def _delete(self, **old):
        base = {
            "type": "aws:s3/bucket:Bucket",
            "urn": URN,
            "custom": True,
            "parent": "urn:pulumi:dev::shop::pulumi:pulumi:Stack::shop-dev",
            "provider": "urn:pulumi:dev::shop::pulumi:providers:aws::default::uuid",
            "protect": True,
            "inputs": {"acl": "private"},
        }
        base.update(old)
        return _pre_event(op="delete", new=None, old=base)

    def test_a_protected_resource_reads_as_protected(self, tmp_path):
        doc = pulumi_preview.build_policy_input(_log(tmp_path, self._delete(), _summary(delete=1)))
        (res,) = doc["resource_changes"]
        assert res["op"] == "delete"
        assert res["protect"] is True, "a policy must be able to deny this"

    def test_the_deleted_resource_describes_itself(self, tmp_path):
        doc = pulumi_preview.build_policy_input(_log(tmp_path, self._delete(), _summary(delete=1)))
        (res,) = doc["resource_changes"]
        assert res["inputs"] == {"acl": "private"}
        assert res["custom"] is True
        assert res["parent"].endswith("shop-dev")

    def test_an_unprotected_delete_is_still_false(self, tmp_path):
        # The fallback must not turn every delete into "protected".
        doc = pulumi_preview.build_policy_input(
            _log(tmp_path, self._delete(protect=False), _summary(delete=1))
        )
        assert doc["resource_changes"][0]["protect"] is False


class TestTheResourceName:
    def test_a_name_containing_the_delimiter_survives(self, tmp_path):
        """Pulumi's own URN.Name() rejoins everything from the fourth part on.

        `rsplit("::", 1)` would return "b" for a resource named "a::b" — a
        different resource, and a policy matching on name would act on the
        wrong one.
        """
        urn = "urn:pulumi:dev::shop::aws:s3/bucket:Bucket::a::b"
        doc = pulumi_preview.build_policy_input(_log(tmp_path, _pre_event(urn=urn), _summary()))
        assert doc["resource_changes"][0]["name"] == "a::b"

    def test_an_ordinary_name_is_unaffected(self, tmp_path):
        doc = pulumi_preview.build_policy_input(_log(tmp_path, _pre_event(), _summary()))
        assert doc["resource_changes"][0]["name"] == "assets"

    def test_an_engine_redacted_secret_stays_redacted(self, tmp_path):
        """Pulumi replaces a marked secret with "[secret]" before writing the log.

        This pins that we carry the engine's redaction through rather than
        reaching past it -- if a future change started reading the raw state,
        or passed --show-secrets, the literal would stop being what arrives.
        """
        event = _pre_event(new={"inputs": {"password": "[secret]"}, "parent": "", "provider": ""})
        doc = pulumi_preview.build_policy_input(_log(tmp_path, event, _summary()))
        assert doc["resource_changes"][0]["inputs"] == {"password": "[secret]"}


class TestItIsNotTheDigest:
    def test_every_step_is_carried_however_many(self, tmp_path):
        """A gate must not decide on a truncated list of resources.

        The digest caps at MAX_STEPS because it exists to be read. A policy
        evaluated over the first N resources can pass because the offending one
        fell off the end, so this document carries them all.
        """
        many = [_pre_event(urn=f"{URN}-{i}") for i in range(pulumi_preview.MAX_STEPS + 25)]
        path = _log(tmp_path, *many, _summary())

        doc = pulumi_preview.build_policy_input(path)
        digest = pulumi_preview.parse_event_log(path)

        assert len(doc["resource_changes"]) == pulumi_preview.MAX_STEPS + 25
        assert len(digest["steps"]) == pulumi_preview.MAX_STEPS
        assert digest["steps_truncated"] is True


class TestWhenThePreviewDidNotFinish:
    def test_no_summary_means_no_document(self, tmp_path):
        # Same rule the digest follows: without a summary event the preview did
        # not finish, and a gate must not decide on a partial account of it.
        assert pulumi_preview.build_policy_input(_log(tmp_path, _pre_event())) is None

    def test_a_missing_log_is_not_an_error(self, tmp_path):
        assert pulumi_preview.build_policy_input(tmp_path / "absent.jsonl") is None

    def test_a_truncated_final_line_does_not_discard_the_rest(self, tmp_path):
        # A killed preview leaves a half-written line; what did arrive still counts.
        path = tmp_path / "events.jsonl"
        path.write_text(
            json.dumps(_pre_event()) + "\n" + json.dumps(_summary()) + '\n{"resourcePre',
            encoding="utf-8",
        )
        doc = pulumi_preview.build_policy_input(path)
        assert doc is not None
        assert len(doc["resource_changes"]) == 1

    def test_an_event_with_no_metadata_is_skipped_not_fatal(self, tmp_path):
        path = _log(tmp_path, {"resourcePreEvent": {}}, _pre_event(), _summary())
        doc = pulumi_preview.build_policy_input(path)
        assert len(doc["resource_changes"]) == 1


class TestWriting:
    def test_it_writes_json_opa_can_read_on_stdin(self, tmp_path):
        doc = pulumi_preview.build_policy_input(_log(tmp_path, _pre_event(), _summary()))
        out = pulumi_preview.write_policy_input(doc, tmp_path / "policy-input.json")
        assert json.loads(out.read_text(encoding="utf-8")) == doc
