"""Reading a preview's event log for the cost engine (#1569).

The third reader of the same log. The digest is a capped summary a person
reads; the policy input is every CHANGING resource; this is every resource the
preview walked, because an estimate reports the stack's monthly total as well
as what the run adds to it, and a total computed from the changes alone is not
a total.

The translation itself is covered by `tests/services/test_cost_pulumi.py`. What
is here is the log-reading around it, and the one refusal that matters: a
preview that did not finish is not priced.
"""

from __future__ import annotations

import json

from terrapod.runner.phases import pulumi_preview

URN = "urn:pulumi:dev::shop::aws:ec2/instance:Instance::web"


def _pre_event(op="create", urn=URN, type_="aws:ec2/instance:Instance", **state):
    base = {"op": op, "urn": urn, "type": type_}
    base.update(
        state
        or {
            "new": {
                "type": type_,
                "urn": urn,
                "custom": True,
                "inputs": {"instanceType": "t3.micro"},
            }
        }
    )
    return {"resourcePreEvent": {"metadata": base}}


def _summary(**changes):
    return {"summaryEvent": {"resourceChanges": changes or {"create": 1, "same": 2}}}


def _log(tmp_path, *events, name="events.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def _planned(doc):
    return doc["planned_values"]["root_module"]["resources"]


class TestWhatTheCostEngineIsGiven:
    def test_a_resource_arrives_in_the_shape_the_engine_reads(self, tmp_path):
        doc = pulumi_preview.build_cost_input(_log(tmp_path, _pre_event(), _summary()))
        assert doc is not None
        (res,) = _planned(doc)
        assert res["type"] == "aws_instance"
        assert res["address"] == URN
        assert res["values"]["instance_type"] == "t3.micro"

    def test_unchanged_resources_are_carried_unlike_the_policy_input(self, tmp_path):
        # The policy input excludes `same` steps on purpose -- a gate must not
        # deny a resource nothing is changing. Cost is the opposite: the total
        # is the stack's monthly bill, so an unchanged resource belongs in it.
        log = _log(
            tmp_path,
            _pre_event(op="same", urn=URN + "-a"),
            _pre_event(op="same", urn=URN + "-b"),
            _summary(same=2),
        )
        doc = pulumi_preview.build_cost_input(log)
        assert len(_planned(doc)) == 2
        # ...and the policy input, from the same log, carries neither.
        assert pulumi_preview.build_policy_input(log)["resource_changes"] == []

    def test_a_preview_that_reported_no_summary_is_not_priced(self, tmp_path):
        # No summary event means the preview did not finish. An estimate over a
        # truncated walk of the stack understates the bill, and nothing about
        # the number would look partial.
        assert pulumi_preview.build_cost_input(_log(tmp_path, _pre_event())) is None

    def test_an_absent_log_is_not_priced(self, tmp_path):
        assert pulumi_preview.build_cost_input(tmp_path / "nothing.jsonl") is None

    def test_a_truncated_final_line_does_not_discard_what_arrived(self, tmp_path):
        # The log is written while the preview runs, so a killed preview leaves
        # a half-written line. The other two readers tolerate it; so does this.
        path = _log(tmp_path, _pre_event(), _summary())
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"resourcePreEvent": {"metadata": {"op": "cre')
        doc = pulumi_preview.build_cost_input(path)
        assert len(_planned(doc)) == 1

    def test_an_event_type_this_code_has_never_seen_is_ignored(self, tmp_path):
        log = _log(tmp_path, {"diagnosticEvent": {"message": "hi"}}, _pre_event(), _summary())
        assert len(_planned(pulumi_preview.build_cost_input(log))) == 1

    def test_the_digest_is_unaffected_by_any_of_this(self, tmp_path):
        # Three readers, one log: adding one must not have changed what the
        # other two report.
        log = _log(tmp_path, _pre_event(), _summary(create=1, same=2))
        digest = pulumi_preview.parse_event_log(log)
        assert digest["has_changes"] is True
        assert digest["change_summary"] == {"create": 1, "same": 2}
        assert digest["steps"] == [
            {"op": "create", "urn": URN, "type": "aws:ec2/instance:Instance"}
        ]
