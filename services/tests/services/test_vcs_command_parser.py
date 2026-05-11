"""Tests for the VCS comment command parser (#282 phase 4)."""

from terrapod.services.vcs_command_parser import Command, is_command_comment, parse


class TestBasicVocabulary:
    def test_plan_matches(self):
        c = parse("terrapod plan")
        assert c == Command(verb="plan", workspace=None, raw="terrapod plan")

    def test_apply_matches(self):
        c = parse("terrapod apply")
        assert c is not None and c.verb == "apply"

    def test_unlock_matches(self):
        c = parse("terrapod unlock")
        assert c is not None and c.verb == "unlock"

    def test_merge_matches(self):
        c = parse("terrapod merge")
        assert c is not None and c.verb == "merge"

    def test_help_matches(self):
        c = parse("terrapod help")
        assert c is not None and c.verb == "help"


class TestWorkspaceFlag:
    def test_short_flag(self):
        c = parse("terrapod apply -W accounts-alpha-net")
        assert c is not None
        assert c.verb == "apply"
        assert c.workspace == "accounts-alpha-net"

    def test_long_flag_with_equals(self):
        c = parse("terrapod plan --workspace=billing-prod")
        assert c is not None
        assert c.workspace == "billing-prod"

    def test_long_flag_with_space(self):
        c = parse("terrapod plan --workspace billing-prod")
        assert c is not None
        assert c.workspace == "billing-prod"

    def test_no_flag_returns_none(self):
        c = parse("terrapod plan")
        assert c is not None and c.workspace is None


class TestPrefixMatching:
    def test_mid_sentence_does_not_match(self):
        """Mentions of `terrapod` mid-text are not commands."""
        assert parse("Earlier I was using terrapod apply but...") is None
        assert parse("see terrapod docs") is None

    def test_must_start_line(self):
        """Indented start is OK; suffix after `terrapod foo` on the same line is ignored."""
        c = parse("  terrapod apply  # please")
        assert c is not None and c.verb == "apply"

    def test_at_prefix_matches(self):
        """`@terrapod-bot apply` matches when the prefix is configured."""
        c = parse("@terrapod-bot apply", mention_prefix="terrapod-bot")
        assert c is not None and c.verb == "apply"

    def test_at_prefix_does_not_match_default(self):
        """With the default `terrapod` prefix, `@terrapod-bot ...` is silently ignored."""
        # The observed prefix stripped of `@` is `terrapod-bot`, which
        # doesn't equal the default `terrapod`. The comment is ignored.
        assert parse("@terrapod-bot apply") is None

    def test_wrong_prefix_ignored(self):
        assert parse("atlantis apply") is None


class TestCodeFences:
    def test_inside_code_block_ignored(self):
        body = """Here's how it would work:

```
terrapod apply
```

Thoughts?"""
        assert parse(body) is None

    def test_after_code_block_matches(self):
        body = """Example:

```
some code
```

terrapod apply
"""
        c = parse(body)
        assert c is not None and c.verb == "apply"

    def test_inline_backticks_not_a_fence(self):
        """Single backticks should not flip the fence state."""
        c = parse("note that `terrapod` is the name\nterrapod apply")
        assert c is not None and c.verb == "apply"


class TestMultilineBodies:
    def test_first_command_wins(self):
        body = "terrapod plan\nterrapod apply"
        c = parse(body)
        assert c is not None and c.verb == "plan"

    def test_finds_command_after_prose(self):
        body = "Looks good!\n\nterrapod apply"
        c = parse(body)
        assert c is not None and c.verb == "apply"


class TestUnknownVerb:
    def test_typo_becomes_help(self):
        """A deliberate-looking `terrapod ploon` resolves to help so the
        dispatcher can reply with usage instead of silent-ignoring."""
        c = parse("terrapod ploon")
        assert c is not None and c.verb == "help"

    def test_bare_terrapod_is_not_a_command(self):
        """`terrapod` with no verb has no second token to match."""
        assert parse("terrapod") is None

    def test_trailing_punctuation_stripped(self):
        c = parse("terrapod apply!")
        assert c is not None and c.verb == "apply"


class TestIsCommandComment:
    def test_true_for_command(self):
        assert is_command_comment("terrapod plan") is True

    def test_false_for_prose(self):
        assert is_command_comment("looks good to me!") is False

    def test_false_for_code_block(self):
        body = "```\nterrapod apply\n```"
        assert is_command_comment(body) is False
