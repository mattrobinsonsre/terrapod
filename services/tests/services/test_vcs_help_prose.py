"""Terrapod answers commands, not conversations (#1836).

The parser maps every unknown verb to `help`, and #1799 made `help` post a
twelve-line usage table. Together that meant a comment merely BEGINNING with
the word — "terrapod is working well now" — drew an unsolicited table onto
someone's pull request, with no way to switch it off.

This line has no `unrecognised` handling (#1799 is 1.8+), so it posts the
generic table for every unknown verb — which makes the prose case the whole
of the problem here.
"""

from __future__ import annotations

import pytest

from terrapod.services.vcs_command_dispatcher import _looks_like_a_command_attempt


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
