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

    def test_a_huge_address_and_attribute_path_are_shortened_with_a_marker(self):
        from terrapod.services.plan_resource_list import MAX_NAME_LENGTH

        key = "k" * 20 * 1024
        raw = _plan(_rc(f"aws_x.{key}", ["update"], {key: 1}, {key: 2}))
        entries, result = _entries(raw)
        attr = entries[0]["attributes"][0]
        for name in (entries[0]["address"], attr["path"]):
            assert len(name) == MAX_NAME_LENGTH + 1 and name.endswith("…")
        assert len(json.dumps(result)) < 10_000

    def test_a_short_address_and_path_are_recorded_whole(self):
        raw = _plan(_rc("aws_x.y", ["update"], {"a": 1}, {"a": 2}))
        entry = _entries(raw)[0][0]
        assert entry["address"] == "aws_x.y" and entry["attributes"][0]["path"] == "a"


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

    def test_a_shrinking_scrub_does_not_expose_a_partial_secret_at_the_window_edge(self):
        secret = "S3cr3t-Value-0123456789-abcdefghijklmnop"
        assert len(secret) == 40
        value = secret * 40 + secret[:25]
        raw = _plan(_rc("aws_x.y", ["update"], {"k": "a"}, {"k": value}))
        after = _entries(raw, [secret])[0][0]["attributes"][0]["after"]
        assert "(sensitive value)" in after
        assert not _long_run_of(secret, after)

    def test_a_secret_spanning_the_start_of_the_scan_is_hidden_wherever_it_starts(self):
        secret = "S3cr3t-Value-0123456789-abcdefghijklmnop"
        for pad in range(0, 3 * len(secret)):
            value = "-" * pad + secret + "-" * 300
            raw = _plan(_rc("aws_x.y", ["update"], {"k": "a"}, {"k": value}))
            after = _entries(raw, [secret])[0][0]["attributes"][0]["after"]
            assert not _long_run_of(secret, after), f"leaked with {pad} characters of padding"

    def test_a_secret_overlapping_another_leaves_no_run_behind_at_the_cap(self):
        longer = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@#$%"
        text = "-" * 39 + "~" * 2 + longer * 2 + longer + "-" * 400
        raw = _plan(_rc("aws_x.y", ["update"], {"k": "a"}, {"k": text}))
        after = _entries(raw, [longer, "U", "~"])[0][0]["attributes"][0]["after"]
        assert not _long_run_of(longer, after)


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

    def test_markers_of_different_shapes_on_the_two_sides_hide_the_whole_node(self):
        raw = _plan(
            _rc(
                "aws_x.y",
                ["update"],
                {"input": {"a": "x"}},
                {"input": [98765, {"SECRETKEY": "v"}]},
                before_sensitive={"input": {}},
                after_sensitive={"input": [True, True]},
            )
        )
        result = build_resource_list(raw, [])
        assert "98765" not in json.dumps(result) and "SECRETKEY" not in json.dumps(result)
        attrs = result["entries"][0]["attributes"]
        assert [(a["path"], a["before"], a["after"]) for a in attrs] == [
            ("input", "(sensitive value)", "(sensitive value)")
        ]


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

    def test_a_list_index_in_the_path_is_followed(self):
        raw = _plan(
            _rc(
                "aws_x.y",
                ["update"],
                {"blk": [{"a": 1}, {"arn": "old"}]},
                {"blk": [{"a": 1}, {}]},
                after_unknown={"blk": [{}, {"arn": True}]},
            )
        )
        attrs = build_resource_list(raw, [])["entries"][0]["attributes"]
        assert [(a["path"], a["after"]) for a in attrs] == [("blk[1].arn", "(known after apply)")]

    def _after(self, before, after, unknown):
        raw = _plan(_rc("aws_x.y", ["update"], before, after, after_unknown=unknown))
        return [a["after"] for a in build_resource_list(raw, [])["entries"][0]["attributes"]]

    def test_a_dotted_key_is_not_taken_for_a_nested_unknown_path(self):
        assert self._after({"a.b": "old"}, {"a.b": "new"}, {"a": {"b": True}}) == ["new"]

    def test_a_whole_value_known_only_after_apply_says_so(self):
        assert self._after("old", None, True) == ["(known after apply)"]

    def test_an_empty_key_follows_its_own_unknown_flag(self):
        assert self._after({"": "old"}, {"": None}, {"": True}) == ["(known after apply)"]
        assert self._after({"": {"0": "old"}}, {"": {"0": "new"}}, {"0": True}) == ["new"]


def _deep(depth, width, leaf):
    keys = [f"k{i}-" + "w" * width for i in range(depth)]
    value = leaf
    for key in reversed(keys):
        value = {key: value}
    return keys, value


class _Counted:
    """A leaf that counts how often it is compared."""

    calls = 0

    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        _Counted.calls += 1
        return isinstance(other, _Counted) and self.value == other.value

    __hash__ = None


class TestADeepPathIsBuiltOnlyForWhatIsRecorded:
    """A path used to be formatted at every nesting level and every level's string
    stayed alive, so deep nesting of wide keys grew memory with depth squared."""

    def test_a_deep_path_of_wide_keys_is_recorded_correctly(self):
        from terrapod.services.plan_resource_list import MAX_NAME_LENGTH

        keys, before = _deep(40, 300, "old")
        _, after = _deep(40, 300, "new")
        _, unknown = _deep(40, 300, True)
        raw = _plan(
            _rc("aws_x.y", ["update"], {"v": before}, {"v": after}, after_unknown={"v": unknown})
        )
        attrs = _entries(raw)[0][0]["attributes"]
        assert len(attrs) == 1
        assert attrs[0]["path"] == ".".join(["v", *keys])[:MAX_NAME_LENGTH] + "…"
        assert (attrs[0]["before"], attrs[0]["after"]) == ("old", "(known after apply)")

    def test_a_deep_path_of_wide_keys_needs_no_memory_per_level(self):
        import tracemalloc

        _, before = _deep(200, 2000, "old")
        _, after = _deep(200, 2000, "new")
        _, unknown = _deep(200, 2000, True)
        raw = _plan(
            _rc("aws_x.y", ["update"], {"v": before}, {"v": after}, after_unknown={"v": unknown})
        )
        tracemalloc.start()
        try:
            result = build_resource_list(raw, ["zz-secret"])
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert result["entries"][0]["attributes"][0]["after"] == "(known after apply)"
        assert peak < 10 * len(raw), f"peak {peak} for a {len(raw)}-byte plan"

    def test_a_deep_change_compares_each_leaf_once(self):
        from terrapod.services.plan_resource_list import _walk

        before, after = _Counted("old"), _Counted("new")
        for i in range(50):
            before, after = {f"k{i}": before}, {f"k{i}": after}
        out = []
        _Counted.calls = 0
        _walk(before, after, None, None, out)
        assert len(out) == 1
        assert _Counted.calls <= 2

    def test_a_secret_straddling_the_cap_of_a_deep_path_leaves_no_prefix_behind(self):
        from terrapod.services.plan_resource_list import MAX_NAME_LENGTH

        secret = "Sup3rS3cretPassw0rd!XYZ"
        for pad in range(MAX_NAME_LENGTH - len(secret) - 2, MAX_NAME_LENGTH + 2):
            inner = {"x" * pad: {secret: {"tail": "old"}}}
            changed = {"x" * pad: {secret: {"tail": "new"}}}
            raw = _plan(_rc("aws_x.y", ["update"], {"v": inner}, {"v": changed}))
            blob = json.dumps(build_resource_list(raw, [secret]))
            assert "Sup3r" not in blob, f"a prefix leaked with {pad} characters of padding"


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


def _long_run_of(secret, text, n=8):
    """The `n`-character runs of `secret` that appear in `text`."""
    return [secret[i : i + n] for i in range(len(secret) - n + 1) if secret[i : i + n] in text]


def _mask_union(text, literals):
    """Naive oracle: hide every occurrence of every literal, overlaps merged."""
    out = []
    for piece in text.split("(sensitive value)"):
        spans = sorted(
            (i, i + len(lit))
            for lit in literals
            for i in range(len(piece))
            if piece.startswith(lit, i)
        )
        merged: list[list[int]] = []
        for start, end in spans:
            if merged and start < merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        pos, parts = 0, []
        for start, end in merged:
            parts += [piece[pos:start], "(sensitive value)"]
            pos = end
        out.append("".join(parts) + piece[pos:])
    return "(sensitive value)".join(out)


def _random_cases():
    """200 seeded cases: a few literals, some inside others, and a text."""
    import random

    rng = random.Random(2462)

    def word(low, high):
        return "".join(rng.choices("abU-", k=rng.randint(low, high)))

    for case in range(200):
        literals = {word(1, 6)}
        for _ in range(rng.randint(0, 4)):
            base = rng.choice(sorted(literals))
            i = rng.randint(0, len(base) - 1)
            j = rng.randint(i + 1, len(base))
            literals.add(
                rng.choice([base[i:j], base[:2] + word(0, 3), word(0, 3) + base[-2:], word(1, 6)])
            )
        literals.discard("")
        text = word(0, 200)
        if rng.random() < 0.5:
            cut = rng.randint(0, len(text))
            text = text[:cut] + "(sensitive value)" + text[cut:]
        yield case, sorted(literals), text


class TestTheScrubHidesEveryOccurrence:
    """Secrets that overlap are hidden as one span, a placeholder is never
    rewritten, and a bounded scrub stops once it has what the cap needs."""

    def test_overlapping_secrets_leave_no_fragment_of_either(self):
        from terrapod.services.plan_resource_list import _compile, _scrub

        assert _scrub("..XYZW..", _compile(["XY", "YZW"])) == "..(sensitive value).."

    def test_a_secret_inside_another_is_one_span(self):
        from terrapod.services.plan_resource_list import _compile, _scrub

        assert _scrub("a-bcd-e", _compile(["bcd", "c"])) == "a-(sensitive value)-e"

    def test_a_later_secret_never_rewrites_the_placeholder_of_an_earlier_one(self):
        from terrapod.services.plan_resource_list import _compile, _scrub

        assert _scrub("abc v", _compile(["abc", "v"])) == "(sensitive value) (sensitive value)"

    def test_a_placeholder_already_in_the_text_is_left_alone(self):
        from terrapod.services.plan_resource_list import _cap, _compile

        text = "a (sensitive value) a"
        assert (
            _cap(text, _compile(["a"]), 120)
            == "(sensitive value) (sensitive value) (sensitive value)"
        )

    def test_the_scrub_matches_a_naive_scan_on_random_overlapping_secrets(self):
        from terrapod.services.plan_resource_list import _compile, _scrub

        for case, literals, text in _random_cases():
            scrubber = _compile(sorted(literals, key=len, reverse=True))
            assert _scrub(text, scrubber) == _mask_union(text, literals), (case, literals, text)

    def test_the_cap_matches_a_naive_scan_on_random_overlapping_secrets(self):
        from terrapod.services.plan_resource_list import _cap, _compile

        for case, literals, text in _random_cases():
            oracle = _mask_union(text, literals)
            scrubber = _compile(sorted(literals, key=len, reverse=True))
            for limit in (1, 5, 17, 18, 40, 120):
                want = oracle[:limit] + "…" if len(oracle) > limit else oracle
                assert _cap(text, scrubber, limit) == want, (case, limit, literals, text)


class TestAnEscapedSecretIsHiddenInAContainer:
    """A value inside a list or dict is rendered with json.dumps, which escapes a
    quote, backslash or control character, so the raw secret no longer matches."""

    def _after(self, leaf, secret=None):
        raw = _plan(_rc("aws_x.y", ["update"], {"blk": []}, {"blk": [{"note": leaf}]}))
        return _entries(raw, [secret or leaf])[0][0]["attributes"][0]["after"]

    def test_a_secret_with_a_quote_and_a_newline_is_hidden(self):
        secret = 'Sup3r"Secret\nValue-0123456789'
        after = self._after(secret)
        assert not _long_run_of(secret, after) and "Value-0123456789" not in after

    def test_a_secret_with_a_backslash_and_a_tab_is_hidden(self):
        secret = "pa\\ss\tword-0123456789-end"
        after = self._after(secret)
        escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
        assert not _long_run_of(secret, after) and not _long_run_of(escaped, after)

    def test_a_plain_secret_is_still_hidden_once(self):
        assert self._after("plain-secret-value") == '{"note": "(sensitive value)"}'

    def test_a_secret_inside_a_jsonencoded_string_in_a_block_is_hidden(self):
        secret = 'Sup3r"Secret\nValue-0123456789'
        after = self._after(json.dumps({"pw": secret}), secret)
        assert not _long_run_of(secret, after) and "Value-0123456789" not in after
        assert "Secret" not in after

    def test_a_secret_goescaped_by_terraforms_jsonencode_is_hidden(self):
        secret = "p<ss>&word-0123456789"
        after = self._after('{"pw":"p\\u003css\\u003e\\u0026word-0123456789"}', secret)
        assert "word-0123456789" not in after
        assert not _long_run_of(secret, after)

    def test_a_secret_encoded_twice_inside_a_string_is_hidden(self):
        secret = 'Sup3r"Secret\nValue-0123456789'
        after = self._after(json.dumps({"outer": json.dumps({"pw": secret})}), secret)
        assert "Value-0123456789" not in after and "Secret" not in after

    def test_a_non_ascii_secret_in_its_ascii_escaped_form_is_hidden(self):
        secret = "caf\u00e9-secret-0123456789"
        after = self._after(json.dumps({"pw": secret}), secret)
        assert "secret-0123456789" not in after

    def test_a_secret_that_is_a_dict_key_inside_a_block_is_hidden(self):
        raw = _plan(
            _rc("aws_x.y", ["update"], {"blk": []}, {"blk": [{"key-secret-0123456789": 1}]})
        )
        after = _entries(raw, ["key-secret-0123456789"])[0][0]["attributes"][0]["after"]
        assert "secret-0123456789" not in after


def _nested(secret, depth):
    """`secret` inside `depth` rounds of jsonencode, as one string."""
    leaf = json.dumps({"pw": secret})
    for _ in range(depth - 1):
        leaf = json.dumps({"o": leaf})
    return leaf


def _hidden(secret, leaf):
    from terrapod.services.plan_resource_list import _compile, _literals, _scrub

    return _scrub(leaf, _compile(_literals([secret])))


class TestEveryEncodingOfASecretIsHidden:
    """A secret inside a string that is itself JSON, nested up to the stated depth,
    in the escapings the common encoders write."""

    S = 'Sup3r"Secret\nValue-0123456789'

    def test_three_rounds_of_jsonencode_in_a_block_are_hidden(self):
        leaf = json.dumps({"o": json.dumps({"i": json.dumps({"pw": self.S})})})
        raw = _plan(_rc("aws_x.y", ["update"], {"env": []}, {"env": [leaf]}))
        after = _entries(raw, [self.S])[0][0]["attributes"][0]["after"]
        assert "Value-0123456789" not in after and "Secret" not in after

    def test_three_rounds_in_a_top_level_string_are_hidden(self):
        raw = _plan(_rc("aws_x.y", ["update"], {"k": "a"}, {"k": _nested(self.S, 3)}))
        after = _entries(raw, [self.S])[0][0]["attributes"][0]["after"]
        assert "Value-0123456789" not in after

    def test_four_rounds_are_hidden(self):
        out = _hidden(self.S, _nested(self.S, 4))
        assert "Value-0123456789" not in out and "Secret" not in out

    def test_five_rounds_on_an_unmarked_value_are_hidden_whole(self):
        # Past MAX_ESCAPE_DEPTH the leaf has a run of 16 backslashes, so it fails closed.
        for depth in (5, 6):
            raw = _plan(_rc("aws_x.y", ["update"], {"k": "a"}, {"k": _nested(self.S, depth)}))
            assert _entries(raw, [self.S])[0][0]["attributes"][0]["after"] == "(sensitive value)"

    def test_five_rounds_in_a_block_are_hidden(self):
        leaf = _nested(self.S, 5)
        raw = _plan(_rc("aws_x.y", ["update"], {"env": None}, {"env": [leaf, "visible"]}))
        after = _entries(raw, [self.S])[0][0]["attributes"][0]["after"]
        assert after == '["(sensitive value)", "visible"]'

    def test_the_modern_go_form_is_hidden(self):
        secret = "pa<ss\bword-0123456789"
        raw = _plan(
            _rc("aws_x.y", ["update"], {"k": "a"}, {"k": '{"pw":"pa\\u003css\\bword-0123456789"}'})
        )
        after = _entries(raw, [secret])[0][0]["attributes"][0]["after"]
        assert "word-0123456789" not in after and not _long_run_of(secret, after)

    def test_the_old_and_modern_go_forms_are_both_hidden(self):
        secret = "pa<ss\bword\f-0123456789"
        for leaf in (
            "x pa\\u003css\\u0008word\\u000c-0123456789 y",
            "x pa\\u003css\\bword\\f-0123456789 y",
        ):
            assert "-0123456789" not in _hidden(secret, leaf), leaf

    def test_the_ascii_and_html_escaped_form_is_hidden(self):
        secret = "\u00e9b>b\n-0123456789"
        assert "-0123456789" not in _hidden(secret, "x \\u00e9b\\u003eb\\n-0123456789 y")

    def test_a_real_backslash_before_b_is_not_read_as_a_backspace(self):
        from terrapod.services.plan_resource_list import _literals

        forms = _literals(["a\\b<c"])
        assert "a\\\\b\\u003cc" in forms and "a\\u0008\\u003cc" not in forms

    def test_upper_case_hex_over_a_go_escaped_secret_is_hidden(self):
        secret = ">\nc8ehh5g5fa26\u00e9\f"
        leaf = '{"pw":"{\\"pw\\":\\"\\\\u003e\\\\nc8ehh5g5fa26\\u00E9\\\\f\\"}"}'
        assert "c8ehh5g5fa26" not in _hidden(secret, "pre " + leaf + " post")

    def test_the_upper_case_hex_escaping_is_hidden(self):
        secret = "caf\u00e9-secret-0123456789"
        assert "secret-0123456789" not in _hidden(secret, "x caf\\u00E9-secret-0123456789 y")

    def test_the_php_escaped_slash_is_hidden(self):
        secret = "https://host/path-0123456789"
        assert "path-0123456789" not in _hidden(secret, "x https:\\/\\/host\\/path-0123456789 y")

    def test_the_old_go_escaping_of_backspace_and_form_feed_is_hidden(self):
        secret = "pass\bword\f-0123456789"
        assert "-0123456789" not in _hidden(secret, "x pass\\u0008word\\u000c-0123456789 y")

    def test_a_plain_secret_over_the_form_length_is_registered_raw_only(self):
        from terrapod.services.plan_resource_list import MAX_FORM_LENGTH, _literals

        secret = "q" * (MAX_FORM_LENGTH + 1)
        assert _literals([secret]) == [secret]

    def test_an_escapable_secret_over_the_form_length_hides_everything(self):
        from terrapod.services.plan_resource_list import MAX_FORM_LENGTH

        secret = "k8pw4mzq" + "a" * MAX_FORM_LENGTH + "\n"
        raw = _plan(_rc("aws_x.y", ["update"], {"k": "a"}, {"k": json.dumps(secret)}))
        assert _entries(raw, [secret])[0][0]["attributes"][0]["after"] == "(sensitive value)"

    def test_the_form_length_is_read_when_the_literals_are_built(self, monkeypatch):
        from terrapod.services import plan_resource_list as prl

        assert len(prl._literals(['a"bcdef'])) > 1
        monkeypatch.setattr(prl, "MAX_FORM_LENGTH", 4)
        assert prl._literals(['a"bcdef']) == ['a"bcdef']

    def test_a_secret_holding_delete_is_hidden_in_its_ascii_form(self):
        secret = "k8pw4mzq\x7fxr7tn2vb"
        raw = _plan(
            _rc("aws_x.y", ["update"], {"k": "a"}, {"k": '{"pw": "k8pw4mzq\\u007fxr7tn2vb"}'})
        )
        after = _entries(raw, [secret])[0][0]["attributes"][0]["after"]
        assert "k8pw4mzq" not in after and "xr7tn2vb" not in after

    def test_a_secret_holding_delete_is_hidden_in_the_go_ascii_and_upper_hex_forms(self):
        secret = "k8pw4mzq\x7fxr7tn2vb"
        for leaf in (
            "x k8pw4mzq\\u007Fxr7tn2vb y",
            "x k8pw4mzq\\\\u007fxr7tn2vb y",
            json.dumps({"pw": json.dumps({"pw": secret})}),
        ):
            assert "xr7tn2vb" not in _hidden(secret, leaf), leaf

    def test_a_secret_under_the_form_length_is_hidden_at_every_depth(self):
        # The cap applies to the secret, so a long escaped form of a short secret is still registered.
        for quotes, depth in ((33000, 2), (20000, 3), (10000, 4)):
            secret = "tailk8pw4mzq" + '"' * quotes
            raw = _plan(_rc("aws_x.y", ["update"], {"k": "a"}, {"k": _nested(secret, depth)}))
            after = _entries(raw, [secret])[0][0]["attributes"][0]["after"]
            assert "k8pw4mzq" not in after, (quotes, depth)

    def test_a_form_over_the_hard_bound_fails_closed(self, monkeypatch):
        from terrapod.services import plan_resource_list as prl

        monkeypatch.setattr(prl, "MAX_FORM_LENGTH", 4)
        monkeypatch.setattr(prl, "_ENCODERS", ((lambda t: t * 1000, prl._flat, None),))
        budget = prl._Budget(10**9)
        prl._literals(['a"b'], budget)
        assert budget.exhausted and budget.reason == "forms"


class TestADeeplyEscapedLeafIsHidden:
    """Each round of escaping doubles a backslash run, so 16 in a row means more rounds
    than the registered forms cover; such a leaf is hidden whole."""

    def _after(self, before, after, secrets=("unrelated-secret",)):
        raw = _plan(_rc("aws_x.y", ["update"], {"k": before}, {"k": after}))
        return _entries(raw, list(secrets))[0][0]["attributes"][0]["after"]

    def test_sixteen_backslashes_hide_the_leaf_and_fifteen_do_not(self):
        assert self._after("a", "x" + "\\" * 16 + "y") == "(sensitive value)"
        assert self._after("a", "x" + "\\" * 15 + "y") == "x" + "\\" * 15 + "y"

    def test_a_windows_path_and_a_few_backslashes_are_unchanged(self):
        for value in ("C:\\Users\\name\\file.txt", "\\\\server\\share\\dir", "a\\\\\\b \\n \\t"):
            assert self._after("a", value) == value

    def test_a_deeply_escaped_dict_key_in_a_block_is_hidden(self):
        key = "k" + "\\" * 16
        raw = _plan(_rc("aws_x.y", ["update"], {"blk": None}, {"blk": [{key: "v"}]}))
        after = _entries(raw, ["unrelated-secret"])[0][0]["attributes"][0]["after"]
        assert after == '[{"(sensitive value)": "v"}]'

    def test_a_deeply_escaped_path_and_address_are_hidden(self):
        key = "k" + "\\" * 16
        raw = _plan(_rc(f"aws_x.{key}", ["update"], {key: 1}, {key: 2}))
        entry = _entries(raw, ["unrelated-secret"])[0][0]
        assert entry["address"] == "(sensitive value)"
        assert entry["attributes"][0]["path"] == "(sensitive value)"

    def test_with_no_secret_to_hide_the_value_is_shown(self):
        raw = _plan(_rc("aws_x.y", ["update"], {"k": "a"}, {"k": "\\" * 20}))
        assert _entries(raw)[0][0]["attributes"][0]["after"] == "\\" * 20

    def test_the_run_length_follows_the_escape_depth(self, monkeypatch):
        from terrapod.services import plan_resource_list as prl

        monkeypatch.setattr(prl, "MAX_ESCAPE_DEPTH", 3)
        assert self._after("a", "x" + "\\" * 8 + "y") == "(sensitive value)"
        assert self._after("a", "x" + "\\" * 7 + "y") == "x" + "\\" * 7 + "y"


class TestConstructionIsCharged:
    """Every literal and every escaped form costs budget, so no plan can make
    building the scrubber unbounded."""

    def test_a_short_literal_costs_a_step(self):
        from terrapod.services.plan_resource_list import _Budget, _compile

        budget = _Budget(10**9)
        _compile([f"s{i}" for i in range(10_000)], budget)
        assert 10**9 - budget.remaining >= 10_000

    def test_an_escaped_form_costs_a_step(self):
        from terrapod.services.plan_resource_list import _Budget, _literals

        budget = _Budget(10**9)
        forms = _literals(['a"b<c'], budget)
        assert len(forms) > 1 and 10**9 - budget.remaining >= len(forms) - 1

    def test_too_many_literals_fails_closed(self, monkeypatch):
        from terrapod.services import plan_resource_list as prl

        monkeypatch.setattr(prl, "MAX_LITERALS", 10)
        budget = prl._Budget(10**9)
        prl._literals([f"secret-{i}" for i in range(50)], budget)
        assert budget.exhausted and budget.remaining <= 0

    def test_too_many_literals_hides_every_value_and_warns_once(self, monkeypatch):
        from unittest.mock import patch

        from terrapod.services import plan_resource_list as prl

        monkeypatch.setattr(prl, "MAX_LITERALS", 10)
        raw = _plan(_rc("aws_x.y", ["update"], {"k": "old"}, {"k": "plain text"}))
        with patch.object(prl.logger, "warning") as warn:
            entry = _entries(raw, [f"secret-{i}" for i in range(50)])[0][0]
        assert entry["address"] == prl.PLACEHOLDER
        assert entry["attributes"][0]["after"] == prl.PLACEHOLDER
        assert warn.call_count == 1

    def test_a_plan_with_a_huge_number_of_special_secrets_fails_closed_without_cost(
        self, monkeypatch
    ):
        import time
        from unittest.mock import patch

        from terrapod.services import plan_resource_list as prl

        made = []

        class Spy(prl._Budget):
            def __init__(self, remaining):
                super().__init__(remaining)
                made.append(self)

        monkeypatch.setattr(prl, "_Budget", Spy)
        secrets = [f'\U0001f600<"{i:07d}' for i in range(200_000)]
        raw = _plan(
            _rc(
                "aws_x.y",
                ["update"],
                {"k": None, "z": "old"},
                {"k": secrets, "z": "new"},
                after_sensitive={"k": True},
            )
        )
        started = time.perf_counter()
        with patch.object(prl.logger, "warning") as warn:
            entry = _entries(raw)[0][0]
        elapsed = time.perf_counter() - started
        assert entry["attributes"] and all(
            a["path"] == prl.PLACEHOLDER and a["after"] == prl.PLACEHOLDER
            for a in entry["attributes"]
        )
        assert made[0].exhausted and made[0].remaining <= 0
        assert warn.call_count == 1
        assert elapsed < 10.0, f"took {elapsed:.2f}s"

    def test_every_backspace_and_form_feed_is_charged(self):
        from terrapod.services.plan_resource_list import _Budget, _literals

        def cost(secret):
            budget = _Budget(10**12)
            _literals([secret], budget)
            return 10**12 - budget.remaining

        n = 20_000
        assert cost("\b" * n + "x") >= n and cost("\b\f" * (n // 2) + "x") >= n

    def test_encoding_runs_no_python_step_per_backspace(self, monkeypatch):
        from terrapod.services import plan_resource_list as prl

        calls = []
        real = json.dumps

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        monkeypatch.setattr(prl.json, "dumps", counting)

        def runs(n):
            calls.clear()
            prl._literals(["\b\f" * n + "x"], prl._Budget(10**12))
            return len(calls)

        assert runs(5000) <= runs(5)

    def test_a_plan_of_backspace_secrets_fails_closed_quickly(self, monkeypatch):
        import time

        from terrapod.services import plan_resource_list as prl

        made = []

        class Spy(prl._Budget):
            def __init__(self, remaining):
                super().__init__(remaining)
                made.append(self)

        monkeypatch.setattr(prl, "_Budget", Spy)
        secrets = ["\b" * 60000 + f"{i:05d}" for i in range(200)]
        raw = _plan(
            _rc("aws_x.y", ["update"], {"k": None}, {"k": secrets}, after_sensitive={"k": True})
        )
        started = time.perf_counter()
        entry = _entries(raw)[0][0]
        elapsed = time.perf_counter() - started
        assert entry["attributes"][0]["after"] == prl.PLACEHOLDER
        assert made[0].exhausted and made[0].reason == "steps"
        assert elapsed < 3.0, f"took {elapsed:.2f}s"


class TestTheCapsFailClosed:
    """Hitting any cap hides everything and logs one warning naming the cap."""

    def _build(self, raw, secrets):
        from unittest.mock import patch

        from terrapod.services import plan_resource_list as prl

        with patch.object(prl.logger, "warning") as warn:
            result = build_resource_list(raw, secrets)
        assert warn.call_count == 1
        return result, warn.call_args.kwargs["reason"]

    def test_too_many_forms_for_one_secret_hides_everything(self, monkeypatch):
        from terrapod.services import plan_resource_list as prl

        monkeypatch.setattr(prl, "MAX_FORMS", 2)
        secret = 'Sup3r"Secret\nValue-0123456789'
        raw = _plan(_rc("aws_x.y", ["update"], {"k": "a"}, {"k": _nested(secret, 3)}))
        result, reason = self._build(raw, [secret])
        entry = result["entries"][0]
        assert reason == "forms"
        assert entry["address"] == prl.PLACEHOLDER
        assert entry["attributes"][0]["after"] == prl.PLACEHOLDER

    def test_the_forms_cap_is_reported_by_the_budget(self, monkeypatch):
        from terrapod.services import plan_resource_list as prl

        monkeypatch.setattr(prl, "MAX_FORMS", 2)
        budget = prl._Budget(10**9)
        prl._literals(['a"b<c\n'], budget)
        assert budget.exhausted and budget.remaining <= 0 and budget.reason == "forms"

    def test_the_literal_cap_is_named_in_the_warning(self, monkeypatch):
        from terrapod.services import plan_resource_list as prl

        monkeypatch.setattr(prl, "MAX_LITERALS", 10)
        raw = _plan(_rc("aws_x.y", ["update"], {"k": "old"}, {"k": "plain"}))
        assert self._build(raw, [f"secret-{i}" for i in range(50)])[1] == "literals"

    def test_the_step_budget_is_named_in_the_warning(self, monkeypatch):
        from terrapod.services import plan_resource_list as prl

        monkeypatch.setattr(prl, "MAX_SCRUB_STEPS", 50)
        raw = _plan(_rc("aws_x.y", ["update"], {"k": "old"}, {"k": "padding " * 40}))
        assert self._build(raw, ["TopSecret"])[1] == "steps"


class TestALongSecretDoesNotOverchargeShortValues:
    def test_a_long_marked_value_sharing_a_first_character_leaves_the_plan_visible(self):
        sec = "a" + "x" * 1_000_000
        vals = {f"k{j}": "a" * 200 + str(j) for j in range(MAX_ATTRIBUTES - 1)}
        before = {"s": "old", **dict.fromkeys(vals, "o")}
        changes = [
            _rc(f"aws_x.r{i}", ["update"], before, {"s": sec, **vals}, after_sensitive={"s": True})
            for i in range(MAX_ENTRIES)
        ]
        last = _entries(_plan(*changes))[0][-1]
        assert last["address"] == f"aws_x.r{MAX_ENTRIES - 1}"
        assert last["attributes"][0]["after"].startswith("a" * 100)


class TestASecretContainingThePlaceholder:
    """Text is split on the placeholder before matching, so a secret that contains
    the placeholder text could never match whole; its fragments are hidden instead."""

    SECRET = "AAAAAAAAAA(sensitive value)BBBBBBBBBB"

    def test_each_fragment_is_hidden_in_a_value(self):
        raw = _plan(_rc("aws_x.y", ["update"], {"k": "a"}, {"k": f"pre {self.SECRET} post"}))
        blob = json.dumps(build_resource_list(raw, [self.SECRET]))
        assert "AAAAAAAAAA" not in blob and "BBBBBBBBBB" not in blob

    def test_each_fragment_is_hidden_in_an_address(self):
        raw = _plan(_rc(f"aws_x.{self.SECRET}", ["create"], None, {}))
        blob = json.dumps(build_resource_list(raw, [self.SECRET]))
        assert "AAAAAAAAAA" not in blob and "BBBBBBBBBB" not in blob


def _spent(budget, start):
    return start - budget.remaining


class TestTheScrubIsBounded:
    """Whoever writes a plan controls both its values and its sensitive literals,
    so the scan has a total work budget and fails closed when it is spent."""

    def test_a_plan_built_to_make_the_scan_slow_is_hidden_and_quick(self):
        import time

        from terrapod.services.plan_resource_list import PLACEHOLDER

        literals = ["a" + "b" * n for n in range(1, 1001)] + ["aa"]

        def change(r):
            before = {f"k{a}": "a" * 16384 for a in range(MAX_ATTRIBUTES)}
            after = {f"k{a}": "a" * 16383 + "z" for a in range(MAX_ATTRIBUTES)}
            return _rc(f"aws_x.r{r}", ["update"], before, after)

        raw = _plan(*[change(r) for r in range(MAX_ENTRIES)])
        started = time.perf_counter()
        result = build_resource_list(raw, literals)
        elapsed = time.perf_counter() - started
        assert result is not None and len(result["entries"]) == MAX_ENTRIES
        for entry in result["entries"]:
            for attr in entry["attributes"]:
                for side in ("before", "after"):
                    assert attr[side].replace(PLACEHOLDER, "") in ("", "z"), attr[side]
        assert elapsed < 1.0, f"took {elapsed:.2f}s"

    def test_an_ordinary_plan_does_not_come_near_the_budget(self, monkeypatch):
        from terrapod.services import plan_resource_list as prl

        made = []

        class Spy(prl._Budget):
            def __init__(self, remaining):
                super().__init__(remaining)
                made.append(self)

        monkeypatch.setattr(prl, "_Budget", Spy)
        secrets = ["s3cr3t-token", "another-secret-value", "hunter2"]

        def change(r):
            before = {f"k{a}": f"old {r} {a} " + "padding " * 40 + secrets[a % 3] for a in range(8)}
            after = {f"k{a}": f"new {r} {a} " + "padding " * 40 + secrets[a % 3] for a in range(8)}
            return _rc("aws_x." + "n" * 250 + str(r), ["update"], before, after)

        raw = _plan(*[change(r) for r in range(MAX_ENTRIES)])
        with_budget = build_resource_list(raw, secrets)
        assert made and made[0].remaining > prl.MAX_SCRUB_STEPS // 2
        monkeypatch.setattr(prl, "MAX_SCRUB_STEPS", 10**12)
        assert build_resource_list(raw, secrets) == with_budget

    def test_a_spent_budget_hides_the_rest_wholesale_and_never_shows_raw_text(self, monkeypatch):
        from terrapod.services import plan_resource_list as prl

        monkeypatch.setattr(prl, "MAX_SCRUB_STEPS", 300)
        secret = "TopSecretValue-0123456789"

        def change(r):
            value = f"value-{r}-" + "x" * 60 + secret + "tail" * 20
            return _rc(f"aws_x.r{r}", ["update"], {"k": "old"}, {"k": value})

        result = build_resource_list(_plan(*[change(r) for r in range(10)]), [secret])
        entries = result["entries"]
        blob = json.dumps(result)
        assert "TopSecret" not in blob and "Value-0123" not in blob
        assert entries[0]["attributes"][0]["after"].startswith("value-0-")
        for entry in entries[-3:]:
            assert entry["address"] == prl.PLACEHOLDER
            assert entry["attributes"][0]["path"] == prl.PLACEHOLDER
            assert entry["attributes"][0]["before"] == prl.PLACEHOLDER
            assert entry["attributes"][0]["after"] == prl.PLACEHOLDER

    def test_a_huge_container_value_is_charged_and_stops_once_the_budget_is_spent(
        self, monkeypatch
    ):
        from unittest.mock import patch

        from terrapod.services import plan_resource_list as prl

        calls = []
        real = prl._scrubbed_tree

        def counting(*args):
            calls.append(1)
            return real(*args)

        monkeypatch.setattr(prl, "_scrubbed_tree", counting)
        monkeypatch.setattr(prl, "MAX_SCRUB_STEPS", 10_000)

        def change(r):
            after = {"k": [""] * 200_000, "s": "x"}
            return _rc(
                f"aws_x.r{r}", ["update"], {"k": None, "s": "o"}, after, after_sensitive={"s": True}
            )

        with patch.object(prl.logger, "warning") as warn:
            entries = _entries(_plan(change(0), change(1)), [])[0]
        assert [e["attributes"][0]["after"] for e in entries] == [prl.PLACEHOLDER] * 2
        assert warn.call_args.kwargs["reason"] == "steps"
        assert len(calls) <= 10_100, len(calls)

    def test_a_value_cut_off_mid_scan_keeps_what_was_scanned_and_hides_the_rest(self):
        from terrapod.services.plan_resource_list import _Budget, _cap, _compile

        scrubber = _compile(["SECRET"])
        out = _cap("x" * 10 + "SECRET" + "y" * 5, scrubber, 120, _Budget(5))
        assert out == "xxxxx(sensitive value)"

    def test_a_budget_spent_inside_a_possible_match_hides_the_start_of_it_too(self):
        from terrapod.services.plan_resource_list import PLACEHOLDER, _Budget, _cap, _compile

        scrubber = _compile(["SECRET"])
        for budget in range(0, 10):
            assert _cap("SECRET", scrubber, 120, _Budget(budget)) == PLACEHOLDER, budget
        for budget in range(0, 4):
            assert "SEC" not in _cap("SEC", scrubber, 120, _Budget(budget)), budget

    def test_an_exhausted_budget_stays_exhausted_for_later_values(self):
        from terrapod.services.plan_resource_list import PLACEHOLDER, _Budget, _cap, _compile

        budget = _Budget(0)
        scrubber = _compile(["SECRET"])
        assert _cap("harmless", scrubber, 120, budget) == PLACEHOLDER
        assert _cap("harmless", scrubber, 120, budget) == PLACEHOLDER
        assert _cap("", scrubber, 120, budget) == ""

    def test_capping_a_long_value_costs_a_few_steps_per_kept_character(self):
        from terrapod.services.plan_resource_list import _Budget, _cap, _compile

        start = 10**9
        budget = _Budget(start)
        out = _cap("x" * 16384, _compile(["abc", "def-secret"]), 120, budget)
        assert out == "x" * 120 + "…"
        assert _spent(budget, start) <= 2 * 121

    def test_a_long_secret_that_shares_a_first_character_costs_little_per_position(self):
        from terrapod.services.plan_resource_list import _Budget, _cap, _compile

        start = 10**9
        budget = _Budget(start)
        out = _cap("-" * 16384, _compile(["-" + "q" * 2000]), 120, budget)
        assert out == "-" * 120 + "…"
        assert _spent(budget, start) < 4000

    def test_a_secret_with_many_distinct_lengths_costs_by_its_match_not_its_length_sum(self):
        from terrapod.services.plan_resource_list import _Budget, _cap, _compile

        literals = ["a" + "b" * n for n in range(1, 1001)]
        start = 10**9
        budget = _Budget(start)
        out = _cap("a" + "b" * 40 + "c" * 500, _compile(literals), 120, budget)
        assert out.startswith("(sensitive value)c")
        assert _spent(budget, start) < 2000

    def test_a_huge_secret_sharing_a_prefix_with_its_escaped_form_builds_quickly(self):
        import time

        big = "q" * 1_600_000 + "\n"
        marked = _plan(
            _rc("aws_x.y", ["update"], {"k": "a"}, {"k": big}, after_sensitive={"k": True})
        )
        started = time.perf_counter()
        result = build_resource_list(marked, [])
        elapsed = time.perf_counter() - started
        assert result["entries"][0]["attributes"][0]["after"] == "(sensitive value)"
        assert elapsed < 2.0, f"took {elapsed:.2f}s"

    def test_the_same_shape_through_an_extra_secret_is_hidden_and_quick(self):
        import time

        big = "q" * 1_600_000 + "\n"
        raw = _plan(_rc("aws_x.y", ["update"], {"k": "a"}, {"k": "head " + big + " tail"}))
        started = time.perf_counter()
        after = _entries(raw, [big])[0][0]["attributes"][0]["after"]
        elapsed = time.perf_counter() - started
        assert "qqqqqqqq" not in after and "(sensitive value)" in after
        assert elapsed < 2.0, f"took {elapsed:.2f}s"

    def test_building_the_trie_costs_linear_work_in_the_literal_length(self):
        from terrapod.services.plan_resource_list import _Budget, _compile

        def cost(n):
            budget = _Budget(10**12)
            _compile(["q" * n + "a", "q" * n + "b", "q" * (n // 2) + "c"], budget)
            return 10**12 - budget.remaining

        assert 0 < cost(100_000) and cost(400_000) <= 5 * cost(100_000)

    def test_a_trie_that_cannot_be_afforded_hides_everything(self, monkeypatch):
        from terrapod.services import plan_resource_list as prl

        monkeypatch.setattr(prl, "MAX_SCRUB_STEPS", 5)
        raw = _plan(_rc("aws_x.y", ["update"], {"k": "old"}, {"k": "hello world"}))
        entry = _entries(raw, ["z" * 4000])[0][0]
        assert entry["address"] == prl.PLACEHOLDER
        assert entry["attributes"][0]["after"] == prl.PLACEHOLDER
        assert entry["attributes"][0]["path"] == prl.PLACEHOLDER

    def test_running_out_of_budget_logs_one_warning(self, monkeypatch):
        from unittest.mock import patch

        from terrapod.services import plan_resource_list as prl

        monkeypatch.setattr(prl, "MAX_SCRUB_STEPS", 50)
        changes = [
            _rc(f"aws_x.r{i}", ["update"], {"k": "old"}, {"k": "padding " * 40 + "TopSecret"})
            for i in range(5)
        ]
        with patch.object(prl.logger, "warning") as warn:
            build_resource_list(_plan(*changes), ["TopSecret"])
        assert warn.call_count == 1
        assert warn.call_args.args[0] == "plan_resource_list.scrub_budget_exhausted"

    def test_an_ordinary_build_logs_no_warning(self):
        from unittest.mock import patch

        from terrapod.services import plan_resource_list as prl

        raw = _plan(_rc("aws_x.y", ["update"], {"k": "old"}, {"k": "new"}))
        with patch.object(prl.logger, "warning") as warn:
            build_resource_list(raw, ["secret"])
        warn.assert_not_called()

    def test_the_cap_matches_a_naive_scan_over_the_registered_forms_of_special_secrets(self):
        import random

        from terrapod.services.plan_resource_list import (
            PLACEHOLDER,
            _cap,
            _compile,
            _literals,
            _scrub,
        )

        rng = random.Random(24621)
        alphabet = 'ab"\\\n<>&\b\f'
        for case in range(200):
            raw = []
            for _ in range(rng.randint(1, 3)):
                secret = "".join(rng.choices(alphabet, k=rng.randint(1, 4)))
                if rng.random() < 0.3:
                    secret += PLACEHOLDER + "".join(rng.choices(alphabet, k=rng.randint(1, 3)))
                raw.append(secret)
            literals = _literals(raw)
            text = "".join(
                rng.choice(literals)[: rng.randint(1, 12)]
                if rng.random() < 0.5
                else rng.choice(alphabet)
                for _ in range(rng.randint(0, 12))
            )
            if rng.random() < 0.3:
                cut = rng.randint(0, len(text))
                text = text[:cut] + PLACEHOLDER + text[cut:]
            oracle = _mask_union(text, literals)
            scrubber = _compile(literals)
            assert _scrub(text, scrubber) == oracle, (case, raw, text)
            for limit in (1, 17, 40, 120):
                want = oracle[:limit] + "…" if len(oracle) > limit else oracle
                assert _cap(text, scrubber, limit) == want, (case, limit, raw, text)

    def test_the_cap_matches_a_naive_scan_with_long_overlapping_literals(self):
        import random

        from terrapod.services.plan_resource_list import _cap, _compile, _scrub

        rng = random.Random(24620)
        for case in range(200):
            literals = {
                "".join(rng.choices("ab", k=rng.randint(1, 30))) for _ in range(rng.randint(1, 12))
            }
            pool = sorted(literals)
            text = "".join(
                rng.choice(pool)[: rng.randint(1, 30)] if rng.random() < 0.6 else rng.choice("abc")
                for _ in range(rng.randint(0, 40))
            )
            oracle = _mask_union(text, literals)
            scrubber = _compile(sorted(literals, key=len, reverse=True))
            assert _scrub(text, scrubber) == oracle, (case, pool, text)
            for limit in (1, 17, 40, 120):
                want = oracle[:limit] + "…" if len(oracle) > limit else oracle
                assert _cap(text, scrubber, limit) == want, (case, limit, pool, text)


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

    def test_big_values_with_registered_secrets_build_quickly(self):
        import time

        secrets = ["s3cr3t", "another-secret-value", "x" * 33, "short", "p@ss/w0rd=" * 4]
        filler = "".join(f"line {n} of a long user-data script\n" for n in range(400))
        assert len(filler) > 12 * 1024

        def value(r, a, side):
            return f"{side}-{r}-{a} {secrets[a % 5]} " + filler + secrets[(r + a) % 5] + filler

        def change(r):
            before = {f"k{a}": value(r, a, "old") for a in range(MAX_ATTRIBUTES)}
            after = {f"k{a}": value(r, a, "new") for a in range(MAX_ATTRIBUTES)}
            return _rc(f"aws_x.r{r}", ["update"], before, after)

        raw = _plan(*[change(r) for r in range(MAX_ENTRIES)])
        started = time.perf_counter()
        result = build_resource_list(raw, secrets)
        elapsed = time.perf_counter() - started
        assert result is not None and len(result["entries"]) == MAX_ENTRIES
        blob = json.dumps(result)
        assert not any(s in blob for s in secrets)
        assert elapsed < 2.0, f"took {elapsed:.2f}s"

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
