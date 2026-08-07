"""The one definition of what a workspace name may be.

A workspace name is not cosmetic. It is the key the `cloud {}` block matches
on, it appears in `/app/{org}/{name}` redirects, in the DR state-index YAML,
and in VCS status contexts. A name that does not meet the format contract
is not a tidiness problem — it is a workspace some of those surfaces cannot
address.

This lives here rather than in a router because more than one path creates a
workspace, and the review that prompted it (#1299) found the newest one —
undelete/restore — checking only "is a non-empty string" while every other
path ran the full check. A validator that only one caller uses is a validator
the next caller will forget.

Raises ValueError; callers translate to their own error shape (the routers to
HTTP 422).
"""

import re

#: Must start alphanumeric, then alphanumerics, hyphens and underscores.
_WORKSPACE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")

#: Matches the DB column width (String(90)). Enforced here rather than left to
#: the database so the caller gets a 422 explaining the rule, not a 500 from a
#: truncated insert.
MAX_WORKSPACE_NAME_LENGTH = 90


def validate_workspace_name(name: str) -> str:
    """Return the cleaned name, or raise ValueError explaining why not."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("Workspace name is required")
    if len(cleaned) > MAX_WORKSPACE_NAME_LENGTH:
        raise ValueError(f"Workspace name must be {MAX_WORKSPACE_NAME_LENGTH} characters or fewer")
    if not _WORKSPACE_NAME_RE.match(cleaned):
        raise ValueError(
            "Workspace name must start with a letter or number and contain only "
            "letters, numbers, hyphens, and underscores"
        )
    return cleaned
