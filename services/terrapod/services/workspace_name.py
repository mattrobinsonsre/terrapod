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


#: The separator joining a Pulumi project and stack into one workspace name.
#: Pulumi identifies a stack as `{org}/{project}/{stack}` while a workspace has
#: one flat name, so the two halves are joined with a sequence neither a Pulumi
#: project nor a stack admits. Kept beside the rule that has to accept it —
#: `pulumi_service` composes names in exactly this shape, and a validator that
#: rejected them would make every Pulumi workspace uncreatable (#1535).
PULUMI_NAME_SEPARATOR = "::"


def validate_workspace_name(name: str, engine: str = "terraform") -> str:
    """Return the cleaned name, or raise ValueError explaining why not.

    Pulumi workspaces are named `project::stack`, so each half is validated by
    the ordinary rule and the separator is allowed between them. Every other
    engine takes a single plain name.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("Workspace name is required")
    if len(cleaned) > MAX_WORKSPACE_NAME_LENGTH:
        raise ValueError(f"Workspace name must be {MAX_WORKSPACE_NAME_LENGTH} characters or fewer")

    if engine == "pulumi":
        parts = cleaned.split(PULUMI_NAME_SEPARATOR)
        if len(parts) != 2 or not all(_WORKSPACE_NAME_RE.match(p) for p in parts):
            raise ValueError(
                "A Pulumi workspace is named 'project::stack' — two parts separated "
                "by '::', each starting with a letter or number and containing only "
                "letters, numbers, hyphens, and underscores. This is the name "
                "`pulumi stack select default/{project}/{stack}` resolves to."
            )
        return cleaned

    if not _WORKSPACE_NAME_RE.match(cleaned):
        raise ValueError(
            "Workspace name must start with a letter or number and contain only "
            "letters, numbers, hyphens, and underscores"
        )
    return cleaned
