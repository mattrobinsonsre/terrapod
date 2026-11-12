"""The PR status comment names the resources a plan changes.

It used to say THAT three resources change. The recorded list (see
`plan_resource_list`) goes into the per-workspace `<details>` block the comment
already has for gate detail, so there is still one comment and no new setting.

Two properties matter more than the layout:

* **The comment is shared by the whole PR.** One comment carries every affected
  workspace, so a per-workspace cap is not enough: a monorepo PR with many
  workspaces can still pass the provider's comment limit. There is an overall
  budget, and past it a list degrades to a one-line pointer to the run while the
  table, which is what a reviewer triages from, always stays.
* **The text is plan content, and a plan can contain anything.** A value that
  reads `@someone` would notify them, a markdown image would call out to a
  third-party host, and a `</details>` would break the block. So the list is a
  fenced code block, where none of that is interpreted, with a fence longer than
  any backtick run inside it.
"""

from terrapod.services.vcs_status_comment import (
    COMMENT_MAX_CHARS,
    GateVerdict,
    _Row,
    render_comment,
)


def _row(name="prod-vpc", resource_list=None, run_url="https://tp.example/run/1", gates=(), **kw):
    return _Row(
        workspace_name=name,
        mode=kw.get("mode", "apply_then_merge"),
        plan_summary=kw.get("plan_summary", "+1 ~1"),
        apply_summary=kw.get("apply_summary", "not applied"),
        mergeable_summary=kw.get("mergeable_summary", "yes"),
        gates=gates,
        run_url=run_url,
        resource_list=resource_list,
    )


def _entry(address, action="create", attributes=(), attributes_omitted=0, imported=False):
    return {
        "address": address,
        "action": action,
        "attributes": list(attributes),
        "attributes_omitted": attributes_omitted,
        "imported": imported,
    }


def _attr(path, before, after):
    return {"path": path, "before": before, "after": after}


class TestTheListInTheDetailsBlock:
    def test_names_each_resource_with_its_action(self):
        out = render_comment(
            [
                _row(
                    resource_list={
                        "entries": [
                            _entry("aws_instance.web", "create"),
                            _entry("aws_db_instance.main", "update"),
                            _entry("aws_s3_bucket.old", "delete"),
                            _entry("aws_eip.ip", "replace"),
                        ],
                        "omitted": 0,
                    }
                )
            ]
        )
        assert "<details>" in out and "</details>" in out
        assert "+ aws_instance.web" in out
        assert "~ aws_db_instance.main" in out
        assert "- aws_s3_bucket.old" in out
        assert "-/+ aws_eip.ip" in out

    def test_an_update_shows_each_changed_attribute_before_and_after(self):
        out = render_comment(
            [
                _row(
                    resource_list={
                        "entries": [
                            _entry(
                                "aws_db_instance.main",
                                "update",
                                [_attr("instance_class", "t3.medium", "t3.large")],
                            )
                        ],
                        "omitted": 0,
                    }
                )
            ]
        )
        assert "instance_class: t3.medium → t3.large" in out

    def test_says_when_attributes_were_left_out(self):
        out = render_comment(
            [
                _row(
                    resource_list={
                        "entries": [
                            _entry("a.b", "update", [_attr("k", "1", "2")], attributes_omitted=4)
                        ],
                        "omitted": 0,
                    }
                )
            ]
        )
        assert "4 more attributes" in out

    def test_says_how_many_resources_were_left_out_and_links_the_run(self):
        out = render_comment([_row(resource_list={"entries": [_entry("a.b")], "omitted": 12})])
        assert "12 more" in out
        assert "https://tp.example/run/1" in out

    def test_marks_an_import(self):
        out = render_comment(
            [
                _row(
                    resource_list={
                        "entries": [_entry("a.b", "update", imported=True)],
                        "omitted": 0,
                    }
                )
            ]
        )
        assert "~ a.b (import)" in out

    def test_a_workspace_with_no_list_and_no_gates_still_gets_no_block(self):
        """Nothing to disclose, so no empty triangle: the comment is exactly what
        it was before this existed for a run that recorded no list."""
        out = render_comment([_row(resource_list=None)])
        assert "<details>" not in out

    def test_an_empty_list_adds_nothing(self):
        """A plan that changes nothing is a true, empty answer, not a block."""
        out = render_comment([_row(resource_list={"entries": [], "omitted": 0})])
        assert "<details>" not in out

    def test_the_list_sits_beside_the_gates_in_one_block(self):
        out = render_comment(
            [
                _row(
                    resource_list={"entries": [_entry("a.b")], "omitted": 0},
                    gates=(GateVerdict("policy", "guardrails", True, "mandatory"),),
                )
            ]
        )
        assert out.count("<details>") == 1
        assert "+ a.b" in out and "guardrails" in out


class TestPlanContentCannotEscapeTheBlock:
    def test_a_mention_a_link_and_a_closing_tag_stay_inside_a_code_fence(self):
        evil = "@some-team ![x](https://evil.example/p.png) </details> <script>"
        out = render_comment(
            [
                _row(
                    resource_list={
                        "entries": [_entry("a.b", "update", [_attr("note", "old", evil)])],
                        "omitted": 0,
                    }
                )
            ]
        )
        body = out.split("<details>", 1)[1]
        fence_open = body.index("```")
        fence_close = body.index("```", fence_open + 3)
        assert evil in body[fence_open:fence_close], "the text must be inside the fence"
        # The injected `</details>` is inside the fence, so the block's own
        # closing tag is the one AFTER it, and nothing of the plan follows it.
        assert body[fence_close:].count("</details>") == 1
        assert "evil.example" not in body[fence_close:]

    def test_the_fence_is_longer_than_any_backtick_run_in_the_content(self):
        out = render_comment(
            [
                _row(
                    resource_list={
                        "entries": [_entry("a.b", "update", [_attr("k", "x", "```` end ```")])],
                        "omitted": 0,
                    }
                )
            ]
        )
        assert "`````" in out, "a 4-backtick run needs a fence of at least 5"

    def test_a_newline_in_a_value_does_not_start_a_new_line_of_the_list(self):
        out = render_comment(
            [
                _row(
                    resource_list={
                        "entries": [_entry("a.b", "update", [_attr("k", "x", "line1\nline2")])],
                        "omitted": 0,
                    }
                )
            ]
        )
        assert "line1\nline2" not in out
        assert "line1" in out and "line2" in out

    def test_an_address_with_a_newline_is_kept_to_one_line(self):
        out = render_comment(
            [_row(resource_list={"entries": [_entry("a.b\n+ injected.resource")], "omitted": 0})]
        )
        assert "\n+ injected.resource" not in out


def _list(n_entries, attrs=8, width=30):
    return {
        "entries": [
            _entry(
                f"aws_instance.srv_{e}",
                "update",
                [_attr(f"a{a}", "v" * width, "w" * width) for a in range(attrs)],
            )
            for e in range(n_entries)
        ],
        "omitted": 0,
    }


def _big_list():
    """The worst the recording caps allow: 25 resources x 8 attributes x 100-char
    values. One workspace's block is then ~43,000 characters."""
    return _list(25, attrs=8, width=100)


class TestOneCommentHasOneBudget:
    def test_many_workspaces_stay_under_the_comment_limit(self):
        rows = [_row(name=f"ws-{i}", resource_list=_big_list()) for i in range(40)]
        out = render_comment(rows)
        assert len(out) <= COMMENT_MAX_CHARS

    def test_the_table_survives_the_budget_even_when_the_lists_do_not(self):
        rows = [_row(name=f"ws-{i}", resource_list=_big_list()) for i in range(40)]
        out = render_comment(rows)
        for i in range(40):
            assert f"ws-{i}" in out, f"workspace {i} lost its table row"

    def test_a_list_that_does_not_fit_degrades_to_a_pointer_to_the_run(self):
        rows = [_row(name=f"ws-{i}", resource_list=_big_list()) for i in range(40)]
        out = render_comment(rows)
        assert "see the run" in out

    def test_a_fair_share_means_one_huge_plan_cannot_starve_the_rest(self):
        """The first workspace is enormous and the other nine are small. Spent
        first-come it would take the whole budget and leave the small ones with
        pointers; split fairly, the small ones keep their lists."""
        rows = [_row(name="ws-huge", resource_list=_big_list())] + [
            _row(name=f"ws-small-{i}", resource_list=_list(2, attrs=2)) for i in range(9)
        ]
        out = render_comment(rows)
        for i in range(9):
            section = out[out.index(f"ws-small-{i}</code>") :]
            assert "aws_instance.srv_0" in section.split("</details>")[0], (
                f"ws-small-{i} lost its list to the huge one"
            )

    def test_when_everything_is_big_the_comment_still_fits_and_every_row_stays(self):
        """Ten workspaces, each ~15,000 characters, cannot all fit. Some keep their
        list and the rest degrade to a pointer; which is the rule's business. What
        must hold is that the comment stays under the limit, that SOME degrade
        (otherwise the budget is not doing anything) and that SOME do not, and
        that every workspace still has its table row."""
        rows = [_row(name=f"ws-{i}", resource_list=_list(25, attrs=8)) for i in range(10)]
        out = render_comment(rows)
        assert len(out) <= COMMENT_MAX_CHARS
        degraded = out.count("see the run")
        assert 0 < degraded < 10, f"{degraded} of 10 degraded"
        for i in range(10):
            assert f"ws-{i}" in out

    def test_a_workspace_within_its_share_keeps_its_list_beside_ones_that_are_not(self):
        rows = [
            _row(name="ws-big", resource_list=_list(25, attrs=8)),
            _row(name="ws-fits", resource_list=_list(3, attrs=2)),
        ]
        out = render_comment(rows)
        fits = out[out.index("ws-fits</code>") :].split("</details>")[0]
        assert "aws_instance.srv_0" in fits and "see the run" not in fits

    def test_realistic_plans_are_not_degraded(self):
        """40 workspaces of 5 small resources, or 5 of 10, are ordinary PRs."""
        for n_ws, n_ent, attrs in [(40, 5, 2), (5, 10, 3), (1, 25, 8)]:
            rows = [_row(name=f"ws-{i}", resource_list=_list(n_ent, attrs)) for i in range(n_ws)]
            out = render_comment(rows)
            assert "see the run" not in out, (n_ws, n_ent, attrs)
            assert len(out) <= COMMENT_MAX_CHARS

    def test_a_small_list_is_never_degraded(self):
        out = render_comment([_row(resource_list={"entries": [_entry("a.b")], "omitted": 0})])
        assert "+ a.b" in out and "see the run" not in out

    def test_the_limit_is_under_the_smaller_of_the_two_providers(self):
        """GitHub caps a comment at 65,536 characters; GitLab's is far larger.
        The budget must hold for the smaller, with room for what the cap does not
        control (the table, the gate and AI blocks are rendered outside it)."""
        assert COMMENT_MAX_CHARS < 65_536
        assert COMMENT_MAX_CHARS >= 40_000, "so low that ordinary plans would degrade"


class TestTheListSurvivesTheApply:
    """No new trigger is needed for the apply: the dispatcher already refreshes
    the comment on every status transition, `applied` included. What has to hold
    is that the list, which is read from the plan-time record, is still there
    after the run has applied, beside an Apply column that now reports the
    outcome."""

    def test_the_list_is_still_rendered_once_the_run_has_applied(self):
        out = render_comment(
            [
                _row(
                    apply_summary="applied",
                    resource_list={"entries": [_entry("aws_instance.web")], "omitted": 0},
                )
            ]
        )
        assert "+ aws_instance.web" in out
        assert "| applied |" in out

    def test_an_errored_apply_does_not_lose_the_list(self):
        out = render_comment(
            [
                _row(
                    apply_summary="errored",
                    resource_list={"entries": [_entry("aws_instance.web")], "omitted": 0},
                )
            ]
        )
        assert "+ aws_instance.web" in out
        assert "| errored |" in out


class TestSmallThings:
    def test_a_single_resource_pointer_reads_in_the_singular(self):
        """The degraded line said `1 resources change`."""
        rows = [_row(name=f"ws-{i}", resource_list=_list(25, attrs=8)) for i in range(10)]
        rows.append(_row(name="ws-one", resource_list={"entries": [_entry("a.b")], "omitted": 0}))
        out = render_comment(rows)
        assert "1 resources" not in out

    def test_an_attribute_with_no_path_is_labelled_not_blank(self):
        out = render_comment(
            [
                _row(
                    resource_list={
                        "entries": [_entry("a.b", "update", [_attr("(value)", "x", "y")])],
                        "omitted": 0,
                    }
                )
            ]
        )
        assert "    (value): x → y" in out
