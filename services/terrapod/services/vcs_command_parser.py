"""Parse `terrapod ...` commands from PR/MR comments (#282).

Strict prefix match: a comment line starting with `terrapod` (or the
configured mention prefix) is the only signal of intent. Mid-sentence
mentions never match. Unknown verbs match the parser but resolve to
`Command(verb="help", ...)` so the dispatcher can post help-back instead
of silently ignoring a deliberate-looking message.

Per the design in #282:
- Authorization is delegated to VCS repo permissions — the parser does
  not consult Terrapod RBAC.
- Code-fenced blocks (``` ... ```) MUST NOT match. Users discussing the
  bot in a code sample shouldn't get a reply.
- The command must start at the beginning of a non-blank line (after
  whitespace). Continuation text after the command on the same line is
  ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Verbs we recognise. Anything else becomes "help" so the dispatcher can
# respond with usage rather than silently ignoring a `terrapod ...`
# comment that looks deliberate.
_KNOWN_VERBS = frozenset({"plan", "apply", "unlock", "merge", "help"})


@dataclass(frozen=True)
class Command:
    """Parsed `terrapod ...` command from a PR/MR comment.

    `verb` is one of the known verbs (or "help" if the verb was missing
    or unrecognised). `workspace` is the value of the `-W` / `--workspace`
    flag, if any. `raw` is the matched line for diagnostics + audit.
    """

    verb: str
    workspace: str | None
    raw: str


# Match `terrapod <verb>` optionally with `-W <name>` / `--workspace=<name>`.
# Anchored to start-of-string (the caller passes one logical line at a
# time). Trailing content after the recognised tokens is allowed but
# ignored — we'd rather match a stray suffix than reject a valid command.
_PREFIX_VERB_RE = re.compile(r"^[ \t]*(?P<prefix>\S+)[ \t]+(?P<verb>\S+)(?P<rest>.*)$")
_WORKSPACE_RE = re.compile(
    r"(?:^|\s)(?:-W|--workspace)(?:[ \t]+|=)(?P<ws>[A-Za-z0-9][A-Za-z0-9_.-]*)"
)


def parse(body: str, *, mention_prefix: str = "terrapod") -> Command | None:
    """Parse a comment body looking for a `terrapod ...` command.

    Returns the first matching command (one per comment), or None if no
    line in the body starts with the prefix.

    `mention_prefix` is configurable per deployment (e.g. `@terrapod-bot`
    if the App has a distinct name in the repo).
    """
    target_prefix = mention_prefix.lower()
    in_code_block = False
    for raw_line in body.splitlines():
        # Track fenced code blocks (```) and skip everything inside them
        # so a discussion of the bot in a code sample never matches.
        stripped = raw_line.lstrip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        m = _PREFIX_VERB_RE.match(raw_line)
        if m is None:
            continue
        # Strip optional `@` so `@terrapod-bot` and `terrapod-bot` both match
        # when the deployment sets mention_prefix accordingly.
        observed = m.group("prefix").lstrip("@").lower()
        if observed != target_prefix:
            continue
        verb = m.group("verb").strip(".,!?:;").lower()
        rest = m.group("rest") or ""
        if verb not in _KNOWN_VERBS:
            return Command(verb="help", workspace=None, raw=raw_line.strip())
        ws_match = _WORKSPACE_RE.search(rest)
        workspace = ws_match.group("ws") if ws_match else None
        return Command(verb=verb, workspace=workspace, raw=raw_line.strip())
    return None


def is_command_comment(body: str, *, mention_prefix: str = "terrapod") -> bool:
    """Cheap pre-filter for the dispatcher: does this comment look like
    a command at all? Returns True iff parse() would return a Command.

    Used by the dispatcher to avoid loading PRSession / VCSConnection for
    comments that obviously aren't directed at the bot.
    """
    return parse(body, mention_prefix=mention_prefix) is not None
