"""Terrapod answers commands, not conversations (#1836).

The parser maps every unknown verb to `help`, and #1799 made `help` post a
twelve-line usage table. Together that meant a comment merely BEGINNING with
the word — "terrapod is working well now" — drew an unsolicited table onto
someone's pull request, with no way to switch it off.

The same conflation lost the typo hint in the case that needed it most:
`Command.unrecognised` is set only when there is NO trailing text, so
`terrapod aply -W web` — the most command-shaped typo there is — arrived with
it empty and got the generic table rather than its own name back.
"""

from __future__ import annotations

import pytest

from terrapod.services.vcs_command_dispatcher import (
    _looks_like_a_command_attempt,
    _unrecognised_verb,
)


class TestProseIsNotACommand:
    """Biased toward silence on purpose. A missed typo hint costs the author
    one puzzled moment; an unsolicited table on every passing mention is noise
    every reviewer on that PR scrolls past, and there is no opt-out."""

    @pytest.mark.parametrize(
        "line",
        [
            "terrapod is working well now",
            "terrapod has been rock solid since the upgrade",
            "terrapod did not pick up my change, any ideas?",
            "terrapod, can you take a look at this?",
        ],
    )
    def test_a_passing_mention_draws_no_reply(self, line):
        assert _looks_like_a_command_attempt(line) is False

    @pytest.mark.parametrize(
        "line",
        [
            "terrapod aply",
            "terrapod aply -W web",
            "terrapod paln -W my-workspace",
            "terrapod appply --all",
        ],
    )
    def test_a_typo_still_gets_answered(self, line):
        assert _looks_like_a_command_attempt(line) is True


class TestTheVerbIsNamedBack:
    """Read off `raw`, because `Command.unrecognised` is empty whenever there
    is trailing text -- which is exactly when the line looks most like a
    command the author meant."""

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("terrapod aply", "aply"),
            ("terrapod aply -W web", "aply"),
            ("terrapod PALN -W web", "paln"),
            ("terrapod aply.", "aply"),
        ],
    )
    def test_it_names_what_was_typed(self, line, expected):
        assert _unrecognised_verb(line) == expected

    def test_a_real_help_request_is_not_an_unrecognised_verb(self):
        """`terrapod help` must still get the table, not a "did you mean"."""
        assert _unrecognised_verb("terrapod help") is None

    def test_a_bare_mention_has_no_verb(self):
        assert _unrecognised_verb("terrapod") is None
