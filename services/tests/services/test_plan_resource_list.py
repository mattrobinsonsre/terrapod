"""The bounded, redacted list of resources a plan changes, for the PR comment.

The status comment used to say THAT three resources change without saying
which. `build_resource_list` reads the plan JSON once, when the run records it,
and returns a short structured list -- address, action, and for an update the
attributes that change with their before and after values. It is bounded by
resource count and value length by construction, so the payload can never be
large and then truncated; and every value passes through the same redaction the
AI summariser's path uses, because a value is exactly what a PR makes public.

Terraform marks sensitive attributes in `before_sensitive` / `after_sensitive`,
which is authoritative because it comes from the provider schema. Markers alone
are not enough: a value copied from a sensitive input into an unmarked attribute
carries no marker, so values the plan marks anywhere are also matched wherever
they appear, and so are the workspace's own sensitive variable values.
"""

import json

from terrapod.services.plan_resource_list import (
    MAX_ATTRIBUTES,
    MAX_ENTRIES,
    MAX_VALUE_LENGTH,
    build_resource_list,
)


def _plan(*changes, **extra) -> bytes:
    return json.dumps(
        {"format_version": "1.2", "resource_changes": list(changes), **extra}
    ).encode()


def _rc(address, actions, before=None, after=None, **change):
    return {
        "address": address,
        "type": address.split(".")[0],
        "change": {"actions": actions, "before": before, "after": after, **change},
    }


def _entries(raw, extra_secrets=()):
    result = build_resource_list(raw, extra_secrets)
    assert result is not None
    return result["entries"], result


class TestWhatIsListed:
    def test_create_update_delete_and_replace_get_one_entry_each(self):
        raw = _plan(
            _rc("aws_instance.web", ["create"], None, {"instance_type": "t3.small"}),
            _rc(
                "aws_db_instance.main",
                ["update"],
                {"instance_class": "t3.medium"},
                {"instance_class": "t3.large"},
            ),
            _rc("aws_s3_bucket.old", ["delete"], {"bucket": "old"}, None),
            _rc("aws_eip.ip", ["delete", "create"], {"a": 1}, {"a": 2}),
        )
        entries, _ = _entries(raw)
        assert [(e["address"], e["action"]) for e in entries] == [
            ("aws_instance.web", "create"),
            ("aws_db_instance.main", "update"),
            ("aws_s3_bucket.old", "delete"),
            ("aws_eip.ip", "replace"),
        ]

    def test_no_op_and_read_are_left_out(self):
        raw = _plan(
            _rc("aws_vpc.main", ["no-op"], {"a": 1}, {"a": 1}),
            _rc("data.aws_ami.x", ["read"], None, {"id": "ami-1"}),
            _rc("aws_instance.web", ["create"], None, {}),
        )
        entries, _ = _entries(raw)
        assert [e["address"] for e in entries] == ["aws_instance.web"]

    def test_an_update_lists_only_the_attributes_that_changed(self):
        raw = _plan(
            _rc(
                "aws_db_instance.main",
                ["update"],
                {"instance_class": "t3.medium", "engine": "postgres", "tags": {"env": "prod"}},
                {"instance_class": "t3.large", "engine": "postgres", "tags": {"env": "stage"}},
            )
        )
        entries, _ = _entries(raw)
        changed = {a["path"]: (a["before"], a["after"]) for a in entries[0]["attributes"]}
        assert changed == {
            "instance_class": ("t3.medium", "t3.large"),
            "tags.env": ("prod", "stage"),
        }

    def test_a_create_or_delete_lists_no_attributes(self):
        """The whole resource is the unit of change; a full attribute dump is
        what makes a plan unreadable in a comment."""
        raw = _plan(_rc("aws_instance.web", ["create"], None, {"instance_type": "t3.small"}))
        entries, _ = _entries(raw)
        assert entries[0]["attributes"] == []

    def test_an_import_is_marked(self):
        raw = _plan(_rc("aws_s3_bucket.b", ["update"], {"a": 1}, {"a": 2}, importing={"id": "b-1"}))
        entries, _ = _entries(raw)
        assert entries[0]["imported"] is True


class TestBoundedByConstruction:
    def test_keeps_the_first_entries_and_counts_the_rest(self):
        raw = _plan(
            *[_rc(f"aws_instance.n{i}", ["create"], None, {}) for i in range(MAX_ENTRIES + 7)]
        )
        entries, result = _entries(raw)
        assert len(entries) == MAX_ENTRIES
        assert result["omitted"] == 7

    def test_nothing_omitted_when_it_fits(self):
        _, result = _entries(_plan(_rc("aws_instance.web", ["create"], None, {})))
        assert result["omitted"] == 0

    def test_caps_the_attributes_of_one_update_and_counts_the_rest(self):
        before = {f"k{i}": i for i in range(MAX_ATTRIBUTES + 4)}
        after = {f"k{i}": i + 1 for i in range(MAX_ATTRIBUTES + 4)}
        entries, _ = _entries(_plan(_rc("aws_x.y", ["update"], before, after)))
        assert len(entries[0]["attributes"]) == MAX_ATTRIBUTES
        assert entries[0]["attributes_omitted"] == 4

    def test_a_long_value_is_shortened_with_a_marker(self):
        raw = _plan(_rc("aws_x.y", ["update"], {"policy": "a" * 5000}, {"policy": "b" * 5000}))
        attr = _entries(raw)[0][0]["attributes"][0]
        for side in ("before", "after"):
            assert len(attr[side]) <= MAX_VALUE_LENGTH + 1
            assert attr[side].endswith("…")

    def test_a_structured_value_is_rendered_compactly_not_dumped(self):
        raw = _plan(_rc("aws_x.y", ["update"], {"cfg": {"a": [1, 2]}}, {"cfg": {"a": [1, 3]}}))
        paths = [a["path"] for a in _entries(raw)[0][0]["attributes"]]
        assert paths == ["cfg.a[1]"]


class TestRedaction:
    def test_a_marked_attribute_shows_the_placeholder_not_the_value(self):
        raw = _plan(
            _rc(
                "aws_db_instance.main",
                ["update"],
                {"password": "old-hunter2-secret"},
                {"password": "new-hunter2-secret"},
                before_sensitive={"password": True},
                after_sensitive={"password": True},
            )
        )
        attr = _entries(raw)[0][0]["attributes"][0]
        assert attr["before"] == "(sensitive value)"
        assert attr["after"] == "(sensitive value)"
        assert "hunter2" not in json.dumps(_entries(raw)[1])

    def test_a_copy_of_a_marked_value_in_an_unmarked_attribute_is_redacted_too(self):
        """The case markers cannot see: a value copied out of a sensitive input
        into an attribute the provider never marked."""
        raw = _plan(
            _rc(
                "terraform_data.copy",
                ["update"],
                {"input": "supersecret-value-123", "output": "old"},
                {"input": "supersecret-value-123", "output": "supersecret-value-124"},
                before_sensitive={"input": True},
                after_sensitive={"input": True},
            ),
            _rc(
                "aws_x.y",
                ["update"],
                {"note": "old"},
                {"note": "contains supersecret-value-123 inline"},
            ),
        )
        blob = json.dumps(_entries(raw)[1])
        assert "supersecret-value-123" not in blob

    def test_the_workspaces_own_sensitive_variable_values_are_redacted(self):
        raw = _plan(
            _rc("aws_x.y", ["update"], {"note": "a"}, {"note": "token is variable-secret-9999"})
        )
        blob = json.dumps(_entries(raw, extra_secrets=["variable-secret-9999"])[1])
        assert "variable-secret-9999" not in blob

    def test_a_sensitive_value_never_survives_into_an_address_or_path(self):
        raw = _plan(
            _rc(
                "aws_x.y",
                ["update"],
                {"labels": {"supersecret-key-777": "v"}},
                {"labels": {"supersecret-key-777": "w"}},
                before_sensitive={"labels": {"supersecret-key-777": True}},
                after_sensitive={"labels": {"supersecret-key-777": True}},
            )
        )
        attr = _entries(raw)[0][0]["attributes"][0]
        assert attr["before"] == "(sensitive value)"

    def test_a_whole_node_marked_sensitive_hides_every_leaf_under_it(self):
        raw = _plan(
            _rc(
                "aws_x.y",
                ["update"],
                {"creds": {"user": "u1", "pass": "p1-longer-secret"}},
                {"creds": {"user": "u2", "pass": "p2-longer-secret"}},
                before_sensitive={"creds": True},
                after_sensitive={"creds": True},
            )
        )
        blob = json.dumps(_entries(raw)[1])
        assert "longer-secret" not in blob and '"u1"' not in blob

    def test_a_short_sensitive_value_is_still_hidden_where_it_is_marked(self):
        """The literal pass has a length floor, but a marker has none: a marked
        value is hidden wherever the marker points, however short."""
        raw = _plan(
            _rc(
                "aws_x.y",
                ["update"],
                {"pin": "123"},
                {"pin": "456"},
                before_sensitive={"pin": True},
                after_sensitive={"pin": True},
            )
        )
        attr = _entries(raw)[0][0]["attributes"][0]
        assert (attr["before"], attr["after"]) == ("(sensitive value)", "(sensitive value)")


class TestAvoidsClaimingWhatItDoesNotKnow:
    def test_unparseable_input_is_none_so_the_caller_records_nothing(self):
        assert build_resource_list(b"not json", []) is None
        assert build_resource_list(b"[1,2]", []) is None

    def test_a_plan_with_no_resource_changes_key_is_none(self):
        """Not an empty list: that would claim `nothing changes` for a payload
        that simply was not a Terraform plan (a Pulumi digest, say)."""
        assert build_resource_list(json.dumps({"engine": "pulumi"}).encode(), []) is None

    def test_a_plan_with_no_changes_is_an_empty_list_not_none(self):
        result = build_resource_list(_plan(), [])
        assert result == {"entries": [], "omitted": 0}

    def test_an_entry_missing_its_address_is_skipped_not_fatal(self):
        raw = _plan({"change": {"actions": ["create"]}}, _rc("aws_x.y", ["create"], None, {}))
        entries, _ = _entries(raw)
        assert [e["address"] for e in entries] == ["aws_x.y"]


class TestASecretAtTheEdgeOfTheCap:
    """The cap cuts a value to MAX_VALUE_LENGTH characters. Redaction has to see
    the WHOLE value first: scrub after cutting and a secret straddling the cut no
    longer matches, so its first characters reach the comment."""

    SECRET = "Sup3rS3cretPassw0rd!XYZ"

    def test_a_secret_straddling_the_cap_leaves_no_prefix_behind(self):
        for pad in range(MAX_VALUE_LENGTH - len(self.SECRET) - 2, MAX_VALUE_LENGTH + 5):
            raw = _plan(
                _rc(
                    "aws_x.y",
                    ["update"],
                    {"user_data": "a"},
                    {"user_data": "x" * pad + self.SECRET},
                )
            )
            blob = json.dumps(build_resource_list(raw, [self.SECRET]))
            assert "Sup3r" not in blob, f"a prefix leaked with {pad} characters of padding"

    def test_a_secret_wholly_inside_the_cap_is_still_replaced(self):
        raw = _plan(_rc("aws_x.y", ["update"], {"k": "a"}, {"k": "pre " + self.SECRET + " post"}))
        blob = json.dumps(build_resource_list(raw, [self.SECRET]))
        assert self.SECRET not in blob and "(sensitive value)" in blob


class TestAShapeChangeDoesNotBypassTheMarkers:
    """When the two sides differ in shape the diff cannot walk them together, so
    it used to emit the whole container as one leaf and never consult the
    markers. An ADDED nested block is the common case: `before: []`,
    `after: [{...}]`."""

    def test_a_block_added_with_a_sensitive_attribute_hides_it(self):
        raw = _plan(
            _rc(
                "aws_x.y",
                ["update"],
                {"settings": []},
                {"settings": [{"password": "hunter2-longer", "name": "db"}]},
                after_sensitive={"settings": [{"password": True}]},
            )
        )
        blob = json.dumps(build_resource_list(raw, []))
        assert "hunter2" not in blob

    def test_the_non_sensitive_part_of_that_block_is_still_shown(self):
        raw = _plan(
            _rc(
                "aws_x.y",
                ["update"],
                {"settings": []},
                {"settings": [{"password": "hunter2-longer", "name": "visible-name"}]},
                after_sensitive={"settings": [{"password": True}]},
            )
        )
        blob = json.dumps(build_resource_list(raw, []))
        assert "visible-name" in blob

    def test_a_dict_added_where_there_was_none_hides_its_sensitive_attribute(self):
        raw = _plan(
            _rc(
                "aws_x.y",
                ["update"],
                {"creds": None},
                {"creds": {"token": "tok-longer-value"}},
                after_sensitive={"creds": {"token": True}},
            )
        )
        assert "tok-longer" not in json.dumps(build_resource_list(raw, []))

    def test_a_block_removed_hides_the_before_side_by_its_own_markers(self):
        raw = _plan(
            _rc(
                "aws_x.y",
                ["update"],
                {"settings": [{"password": "hunter2-longer"}]},
                {"settings": []},
                before_sensitive={"settings": [{"password": True}]},
            )
        )
        assert "hunter2" not in json.dumps(build_resource_list(raw, []))

    def test_markers_on_only_one_side_still_protect_that_sides_value(self):
        raw = _plan(
            _rc(
                "aws_x.y",
                ["update"],
                {"pin": "1234"},
                {"pin": "5678"},
                before_sensitive={"pin": True},
            )
        )
        blob = json.dumps(build_resource_list(raw, []))
        assert "1234" not in blob and "5678" not in blob, (
            "a value is hidden if EITHER side marks it"
        )

    def test_a_numeric_sensitive_value_is_hidden_though_the_literal_pass_skips_numbers(self):
        raw = _plan(
            _rc(
                "aws_x.y",
                ["update"],
                {"settings": []},
                {"settings": [{"pin": 12345678}]},
                after_sensitive={"settings": [{"pin": True}]},
            )
        )
        assert "12345678" not in json.dumps(build_resource_list(raw, []))

    def test_a_secret_with_characters_json_would_escape_is_hidden_in_a_container(self):
        """json.dumps turns a quote or a newline into an escape sequence, so the
        raw literal no longer matches the rendered text."""
        pem = 'line one "quoted"\nline two-secret-material'
        raw = _plan(
            _rc(
                "aws_x.y",
                ["update"],
                {"tls": []},
                {"tls": [{"key": pem}]},
                after_sensitive={"tls": [{"key": True}]},
            )
        )
        blob = json.dumps(build_resource_list(raw, [pem]))
        assert "secret-material" not in blob


class TestAShortWorkspaceSecret:
    """`collect_literals` has an 8-character floor, a judgement made for an
    internal AI prompt. On a public pull request a short secret is not
    acceptable, so an attribute whose whole value equals one is hidden at any
    length."""

    def test_a_short_sensitive_variable_value_is_hidden_when_it_is_the_whole_value(self):
        raw = _plan(_rc("aws_x.y", ["update"], {"pw": "old"}, {"pw": "hunter2"}))
        blob = json.dumps(build_resource_list(raw, ["hunter2"]))
        assert "hunter2" not in blob

    def test_a_short_secret_inside_a_longer_value_is_hidden_too(self):
        raw = _plan(
            _rc("aws_x.y", ["update"], {"url": "a"}, {"url": "postgres://u:hunter2@host/db"})
        )
        blob = json.dumps(build_resource_list(raw, ["hunter2"]))
        assert "hunter2" not in blob

    def test_an_empty_secret_does_not_blank_everything(self):
        raw = _plan(_rc("aws_x.y", ["update"], {"k": "a"}, {"k": "visible"}))
        assert "visible" in json.dumps(build_resource_list(raw, [""]))


class TestUnknownValues:
    def test_a_value_known_only_after_apply_says_so_rather_than_null(self):
        raw = _plan(
            _rc(
                "aws_x.y",
                ["update"],
                {"arn": "arn:old"},
                {},
                after_unknown={"arn": True},
            )
        )
        attrs = build_resource_list(raw, [])["entries"][0]["attributes"]
        assert attrs[0]["after"] == "(known after apply)"


class TestTheLiteralPassSparesThePlaceholder:
    """There is no length floor on this surface, so a one-character secret is a
    literal, and `(sensitive value)` contains a `v`, an `e` and an `s`. Matching
    a secret inside already-redacted text rewrote the placeholder and left
    fragments of the marker where the secret had been."""

    def test_a_one_character_secret_does_not_corrupt_a_placeholder(self):
        raw = _plan(
            _rc(
                "aws_x.y",
                ["update"],
                {"a": "1", "b": "old"},
                {"a": "2", "b": "new"},
                before_sensitive={"a": True},
                after_sensitive={"a": True},
            )
        )
        result = build_resource_list(raw, ["e"])
        by_path = {x["path"]: x for x in result["entries"][0]["attributes"]}
        assert by_path["a"]["before"] == "(sensitive value)"
        assert by_path["a"]["after"] == "(sensitive value)"

    def test_the_secret_is_still_removed_from_the_text_around_a_placeholder(self):
        raw = _plan(
            _rc(
                "aws_x.y",
                ["update"],
                {"n": "a"},
                {"n": "has hunter2 and more"},
            )
        )
        blob = json.dumps(build_resource_list(raw, ["hunter2"]))
        assert "hunter2" not in blob


def _replace_each(text, literals):
    for s in literals:
        text = text.replace(s, "(sensitive value)")
    return text


class TestItStaysFastOnABigPlan:
    """A plan whose attributes are mostly marked sensitive yields a literal for
    every marked value, and a large one yields tens of thousands. Scrubbing each
    rendered string against each literal one `replace` at a time is
    O(strings x literals) and held the interpreter for seconds on a multi-MB plan,
    in a worker thread that shares the GIL with the event loop."""

    def test_a_large_plan_with_many_marked_values_builds_quickly(self):
        import time

        def change(i):
            before = {f"k{j}": f"old-{i}-{j}-value-padding" for j in range(40)}
            after = {f"k{j}": f"new-{i}-{j}-value-padding" for j in range(40)}
            marked = {f"k{j}": True for j in range(30)}
            return _rc(
                f"aws_x.r{i}",
                ["update"],
                before,
                after,
                before_sensitive=marked,
                after_sensitive=marked,
            )

        raw = _plan(*[change(i) for i in range(1500)])
        started = time.perf_counter()
        result = build_resource_list(raw, [])
        elapsed = time.perf_counter() - started
        assert result is not None and len(result["entries"]) == MAX_ENTRIES
        assert elapsed < 1.5, f"took {elapsed:.2f}s"

    def test_the_scrub_result_is_the_same_as_replacing_one_at_a_time(self):
        """The fast path must not change what is hidden, longest literal first."""
        from terrapod.services.plan_resource_list import _compile, _scrub

        literals = sorted({"abc", "abcdef", "def", "x"}, key=len, reverse=True)
        for text in (
            "abcdef and abc and def and x end",
            "xx abcabc defdef",
            "nothing here",
            "",
            "(sensitive value) and abc",
        ):
            slow = "(sensitive value)".join(
                _replace_each(piece, literals) for piece in text.split("(sensitive value)")
            )
            assert _scrub(text, _compile(literals)) == slow, text

    def test_a_secret_longer_than_the_text_is_simply_not_found(self):
        from terrapod.services.plan_resource_list import _compile, _scrub

        assert _scrub("short", _compile(["a-very-long-secret-value"])) == "short"
