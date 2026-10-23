"""Reading a configure's log, and the format contract around it (#2144).

Before this there was no read path at all: `configure_log_key` had a write route
and no GET anywhere, `_serve_log` had exactly two callers, and the web fetcher
was typed `'plan' | 'apply'`. So a configure's output was written to storage and
unreachable by every consumer -- the API, the SDK, the MCP tool and the UI.

The format gate at the bottom is the one worth reading. The prefix is written by
a plugin that imports `ansible`, which this process does not have, so the two
sides cannot share a constant and the literals are restated here. Restating a
contract is exactly how two sides drift, so the gate reads the plugin's own
source and fails if either literal moves on either side.
"""

from __future__ import annotations

import pathlib
import re
import uuid
from unittest.mock import MagicMock, patch

import pytest

from terrapod.api.routers import runs as runs_router
from terrapod.auth import capability_urls as cu

_PLUGIN = (
    pathlib.Path(runs_router.__file__).resolve().parents[3]
    / "terrapod"
    / "runner"
    / "ansible_callback"
    / "terrapod_stdout.py"
)


def _run(status: str = "applied", *, is_configure: bool = True) -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = uuid.uuid4()
    run.status = status
    run.is_configure = is_configure
    return run


class TestTheConfigureSubResource:
    def test_it_hands_out_a_capability_rather_than_a_bare_run_id(self):
        run = _run()
        attrs = runs_router._configure_json(run)["data"]["attributes"]
        url = attrs["log-read-url"]
        assert str(run.id) not in url
        segment = url.split("/configures/")[1].split("/log")[0]
        assert cu.verify(segment, expect_kind=cu.KIND_CONFIGURE_LOG) == str(run.id)

    def test_it_is_native_and_never_on_the_compatibility_surface(self):
        """No CLI consumes a configure, so `/api/tfe/v2` would be #1905 in reverse.

        `go-tfe` has no concept of a configure phase, so a route for one on the
        surface it reads is surface nothing consumes -- and the engine-scoping
        lesson is that the compatibility surface is for what the CLI needs, not
        for whatever is convenient to mount twice.
        """
        url = runs_router._configure_json(_run())["data"]["attributes"]["log-read-url"]
        assert "/api/v1/configures/" in url
        assert "/api/tfe/v2" not in url and "/api/v2/" not in url

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("queued", "pending"),
            ("applying", "pending"),
            ("configuring", "running"),
            ("canceling", "running"),
            ("applied", "finished"),
            ("errored", "errored"),
        ],
    )
    def test_the_status_tracks_the_configure_and_not_the_apply(self, status, expected):
        assert runs_router._configure_status(_run(status)) == expected

    def test_a_run_that_is_not_a_configure_says_so_rather_than_404ing(self):
        """A client gates the surface on the run's own `is-configure`.

        Making absence the signal would be the recorded shape: a 404 is also
        what a wrong path, a deleted run and a permissions failure look like.
        """
        assert runs_router._configure_status(_run("applied", is_configure=False)) == "unreachable"


class TestStrippingTheHostTag:
    """`format=plain` yields ansible's own words (user, 2026-10-23)."""

    HEADER = b"#terrapod-host-log v1"

    def test_it_removes_the_prefix_and_the_header(self):
        data = self.HEADER + b"\nweb1       | ok: [web1]\nweb1       |     done\n"
        assert runs_router._untag_host_log(data) == b"ok: [web1]\n    done\n"

    def test_an_untagged_log_is_returned_byte_for_byte(self):
        """An older runner, or a configure that ran without the plugin."""
        data = b"PLAY [x] ***\nok: [web1]\n"
        assert runs_router._untag_host_log(data) == data

    def test_bytes_before_the_header_are_left_alone(self):
        """The same artifact legitimately carries untagged bytes first.

        `ansible_collections.install` writes galaxy output to this file before
        ansible runs, and the orchestrator's own structlog lines are rolled in
        around it. A prefix matcher let loose on those would eat the first pipe
        out of an ordinary line of prose -- which is why the header is searched
        for rather than assumed to be at offset 0.
        """
        prose = b"Installing collection a.b | version 1.0\n"
        data = prose + self.HEADER + b"\nweb1       | ok: [web1]\n"
        assert runs_router._untag_host_log(data) == prose + b"ok: [web1]\n"

    def test_the_hostless_marker_is_stripped_like_any_other(self):
        data = self.HEADER + b"\n~          | TASK [x] ***\n"
        assert runs_router._untag_host_log(data) == b"TASK [x] ***\n"

    def test_an_escaped_host_is_stripped_too(self):
        """A host carrying the delimiter arrives escaped, so one pipe is the field."""
        data = self.HEADER + b"\nwe%7Cird   | ok: [we|ird]\n"
        assert runs_router._untag_host_log(data) == b"ok: [we|ird]\n"


class TestTheFormatContractHoldsAcrossTheProcessBoundary:
    def test_the_api_literals_match_the_plugins(self):
        """The two sides cannot share a constant, so a gate stands in for one.

        The plugin imports `ansible`; this process has none, so importing it
        here would fail. Restating the literals is therefore forced -- and this
        is what stops the restatement silently drifting from the thing it
        restates.
        """
        src = _PLUGIN.read_text()
        header = re.search(r'^HEADER = "(?P<v>[^"]+)"', src, re.M).group("v")
        delimiter = re.search(r'^DELIMITER = "(?P<v>[^"]+)"', src, re.M).group("v")

        assert runs_router._HOST_LOG_HEADER == header.encode(), (
            "the API's restated header no longer matches the plugin's — a tagged "
            "log would be served with its prefixes intact to a caller asking for "
            "plain text"
        )
        assert runs_router._HOST_LOG_DELIMITER == delimiter.encode(), (
            "the API's restated delimiter no longer matches the plugin's"
        )

    def test_the_matcher_handles_the_plugins_own_padding_width(self):
        """Derived from the plugin's `_PAD`, not from a number copied here."""
        src = _PLUGIN.read_text()
        pad = int(re.search(r"^_PAD = (?P<v>\d+)", src, re.M).group("v"))
        host = "web1"
        line = host.ljust(pad) + " | " + "ok: [web1]"
        data = runs_router._HOST_LOG_HEADER + b"\n" + line.encode() + b"\n"
        assert runs_router._untag_host_log(data) == b"ok: [web1]\n"


class TestTheCapabilityIsItsOwnKind:
    def test_a_plan_capability_cannot_read_a_configure_log(self):
        run_id = str(uuid.uuid4())
        cap = cu.mint(cu.KIND_PLAN_LOG, run_id)
        assert cu.verify(cap, expect_kind=cu.KIND_CONFIGURE_LOG) is None

    def test_a_configure_capability_cannot_read_a_plan_log(self):
        run_id = str(uuid.uuid4())
        cap = cu.mint(cu.KIND_CONFIGURE_LOG, run_id)
        assert cu.verify(cap, expect_kind=cu.KIND_PLAN_LOG) is None
        assert cu.verify(cap, expect_kind=cu.KIND_APPLY_LOG) is None

    def test_all_three_phase_kinds_are_distinct(self):
        """Derived, so a fourth phase cannot be added without separation."""
        run_id = str(uuid.uuid4())
        kinds = [cu.KIND_PLAN_LOG, cu.KIND_APPLY_LOG, cu.KIND_CONFIGURE_LOG]
        assert len(set(kinds)) == len(kinds)
        minted = {k: cu.mint(k, run_id) for k in kinds}
        for kind, cap in minted.items():
            for other in kinds:
                if other == kind:
                    assert cu.verify(cap, expect_kind=other) == run_id
                else:
                    assert cu.verify(cap, expect_kind=other) is None


class TestTheLogRouteIsWiredToTheRightKey:
    async def test_it_serves_the_configure_key_and_phase(self):
        """Not the apply key, and not relabelled as `plan`.

        The relabelling is not hypothetical: the live-stream endpoint used to
        coerce an unknown phase to `plan` and write over the plan's own Redis
        key, so a configure's output appeared in the plan tab with nothing
        reporting an error.
        """
        run = _run("applied")
        captured: dict = {}

        async def fake_serve_log(**kwargs):
            captured.update(kwargs)
            from fastapi import Response

            return Response(content=b"")

        with (
            patch.object(runs_router, "_serve_log", side_effect=fake_serve_log),
            patch.object(runs_router.run_service, "get_run", return_value=run),
            patch.object(
                runs_router,
                "resolve_capability_or_authenticate",
                return_value=(f"configure-{run.id}", None),
            ),
        ):
            await runs_router.configure_log(
                request=MagicMock(), configure_id="cap-x", offset=0, limit=0, format="raw", db=None
            )

        assert captured["phase"] == "configure"
        assert f"/configures/{run.id}.log" in captured["log_key"]
        assert captured["untag"] is False, "a raw read keeps the prefix the UI parses"

    async def test_plain_asks_for_untagging(self):
        run = _run("applied")
        captured: dict = {}

        async def fake_serve_log(**kwargs):
            captured.update(kwargs)
            from fastapi import Response

            return Response(content=b"")

        with (
            patch.object(runs_router, "_serve_log", side_effect=fake_serve_log),
            patch.object(runs_router.run_service, "get_run", return_value=run),
            patch.object(
                runs_router,
                "resolve_capability_or_authenticate",
                return_value=(f"configure-{run.id}", None),
            ),
        ):
            await runs_router.configure_log(
                request=MagicMock(),
                configure_id="cap-x",
                offset=0,
                limit=0,
                format="plain",
                db=None,
            )

        assert captured["untag"] is True
        assert captured["strip_ansi"] is True


class TestAConfigureIdNamesItsRun:
    """`configure-{uuid}` is the third spelling of a run, beside plan and apply.

    `_configure_json` emits `configure-{run.id}` as the sub-resource's id and
    `/api/v1/configures/configure-{run.id}` as its `self` link, so a caller
    legitimately holds that spelling and sends it back -- which is why
    `ID_PREFIXES` carries it and the run resolver strips it.

    Gated here rather than left to `test_id_prefix_tolerance`, which found the
    missing table entry and could NOT find this half: removing `configure-`
    from the resolver leaves all 58 of its tests passing, because it drives a
    run route with the `run-` spelling. A table entry that nothing strips is
    the recorded flag-nothing-reads shape, and so is its mirror -- a stripper
    with no test.
    """

    async def test_the_run_resolver_accepts_a_configure_id(self):
        run = _run()
        with patch.object(runs_router.run_service, "get_run", return_value=run) as get:
            got = await runs_router._get_run(f"configure-{run.id}", None)
        assert got is run
        # The prefix was stripped, not passed through: the resolver must have
        # asked for the bare uuid.
        assert get.await_args.args[1] == run.id

    async def test_every_spelling_of_a_run_reaches_the_same_row(self):
        """Derived from the resolver's own prefix list, so a fourth view cannot
        be added without being accepted here too."""
        run = _run()
        for prefix in ("", "run-", "plan-", "apply-", "configure-"):
            with patch.object(runs_router.run_service, "get_run", return_value=run) as get:
                got = await runs_router._get_run(f"{prefix}{run.id}", None)
            assert got is run, prefix
            assert get.await_args.args[1] == run.id, prefix

    def test_the_prefix_is_in_the_shared_table(self):
        """Or no endpoint accepts the spelling `_configure_json` hands out."""
        from terrapod.api.ids import ID_PREFIXES

        assert ID_PREFIXES.get("configures") == "configure-"
