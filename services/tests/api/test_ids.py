"""The shared id parser (#1699).

Every caller-supplied id in the API flows through `parse_id`, so its behaviour
is pinned here rather than inferred from the call sites that use it.

The property that matters most is the additive one: an id that worked before
must still work, spelled exactly as before. The tolerance only ever *adds* the
other spelling.
"""

import uuid

import pytest
from fastapi import HTTPException

from terrapod.api.ids import ID_PREFIXES, parse_id, parse_id_for, strip_id_prefix

SAMPLE = uuid.UUID("01a0aa66-8cd5-7ea3-b290-4004b03a8672")


class TestBothSpellings:
    """The point of the exercise: either spelling names the same row."""

    def test_accepts_the_bare_form(self):
        assert parse_id(str(SAMPLE), "ws-") == SAMPLE

    def test_accepts_the_prefixed_form(self):
        assert parse_id(f"ws-{SAMPLE}", "ws-") == SAMPLE

    def test_both_spellings_give_the_same_uuid(self):
        assert parse_id(str(SAMPLE), "ws-") == parse_id(f"ws-{SAMPLE}", "ws-")

    def test_accepts_any_of_several_prefixes(self):
        # A plan and an apply are both views of a run, so all three spellings
        # name the same row and all three must be accepted.
        for spelling in (f"run-{SAMPLE}", f"plan-{SAMPLE}", f"apply-{SAMPLE}", str(SAMPLE)):
            assert parse_id(spelling, "run-", "plan-", "apply-") == SAMPLE

    def test_a_bare_id_is_unchanged_when_no_prefix_is_offered(self):
        # Resources that emit bare ids still parse, and still get the error
        # handling, which is the other half of why they route through here.
        assert parse_id(str(SAMPLE)) == SAMPLE


class TestNoLongerAServerFault:
    """A bad id is a bad request. It was reported as a 500 in 46 places."""

    def test_garbage_raises_an_http_error_not_a_value_error(self):
        # The bug: uuid.UUID() raises ValueError, which reached the global
        # handler in app.py and became "Internal server error".
        with pytest.raises(HTTPException) as e:
            parse_id("notauuid", "ws-")
        assert e.value.status_code == 404

    def test_an_empty_id_is_rejected_cleanly(self):
        with pytest.raises(HTTPException):
            parse_id("", "ws-")

    def test_a_prefix_with_nothing_after_it_is_rejected_cleanly(self):
        with pytest.raises(HTTPException):
            parse_id("ws-", "ws-")

    def test_the_wrong_prefix_is_rejected_cleanly(self):
        # `apool-{uuid}` sent to a workspace endpoint: the prefix does not
        # match, so what is left is not a uuid. Still a 4xx, never a 500.
        with pytest.raises(HTTPException):
            parse_id(f"apool-{SAMPLE}", "ws-")

    @pytest.mark.parametrize("bad", [None, 12345, ["a"], {"a": 1}])
    def test_a_non_string_is_rejected_cleanly(self, bad):
        # A body field can arrive as any JSON type; none of them are a server
        # fault either.
        with pytest.raises(HTTPException):
            parse_id(bad, "ws-")


class TestTheCallerKeepsItsOwnStatus:
    """Additive: an endpoint that answers 400 or 422 today still does."""

    def test_the_default_is_404(self):
        with pytest.raises(HTTPException) as e:
            parse_id("nope", "ws-")
        assert e.value.status_code == 404

    @pytest.mark.parametrize("status", [400, 422])
    def test_an_existing_status_is_preserved(self, status):
        # roles.py answers 422 and registry_modules.py answers 400. Converging
        # them on 404 would change behaviour for a caller handling the current
        # answer, so the call site passes its own.
        with pytest.raises(HTTPException) as e:
            parse_id("nope", "ws-", status=status)
        assert e.value.status_code == status

    def test_the_detail_names_the_resource(self):
        with pytest.raises(HTTPException) as e:
            parse_id("nope", "run-", detail="Run not found")
        assert e.value.detail == "Run not found"


class TestStripping:
    def test_strips_only_once(self):
        # Stripping repeatedly would accept `ws-ws-{uuid}`.
        assert strip_id_prefix(f"ws-ws-{SAMPLE}", "ws-") == f"ws-{SAMPLE}"
        with pytest.raises(HTTPException):
            parse_id(f"ws-ws-{SAMPLE}", "ws-")

    def test_leaves_an_unprefixed_value_alone(self):
        assert strip_id_prefix(str(SAMPLE), "ws-") == str(SAMPLE)

    def test_only_the_first_matching_prefix_is_removed(self):
        assert strip_id_prefix(f"plan-{SAMPLE}", "run-", "plan-") == str(SAMPLE)

    def test_an_id_beginning_with_the_prefix_letters_is_not_corrupted(self):
        # A bare uuid can legitimately start with the letters of a prefix
        # without the hyphen; only the exact prefix is removed.
        assert strip_id_prefix("wsabc", "ws-") == "wsabc"


class TestByResourceType:
    def test_resolves_the_prefix_from_the_table(self):
        assert parse_id_for(f"ws-{SAMPLE}", "workspaces") == SAMPLE
        assert parse_id_for(f"apool-{SAMPLE}", "agent-pools") == SAMPLE

    def test_a_type_with_no_prefix_still_parses_and_still_guards(self):
        assert parse_id_for(str(SAMPLE), "catalog-items") == SAMPLE
        with pytest.raises(HTTPException):
            parse_id_for("nope", "catalog-items")

    def test_every_prefix_in_the_table_round_trips(self):
        # Guards the table itself: a typo'd entry (a missing hyphen, say)
        # would not strip, and the prefixed form would stop parsing.
        for resource_type in ID_PREFIXES:
            prefix = ID_PREFIXES[resource_type]
            assert parse_id_for(f"{prefix}{SAMPLE}", resource_type) == SAMPLE
