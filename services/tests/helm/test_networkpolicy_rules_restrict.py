"""Every bundled NetworkPolicy rule actually restricts something.

GHSA-vg6r-fvfr-27v5. `networkpolicy-web.yaml` carried an ingress rule with only
`ports:` and no `from:`. In NetworkPolicy semantics that admits **all** sources
on the port, so an operator who turned policies on got a web policy that read
as a restriction and enforced nothing — while the comment above it said
"Allow traffic from ingress controller".

An enabled policy that permits everything is worse than an absent one: nothing
prompts anyone to look at it. The sibling `networkpolicy-runner.yaml` shows both
correct forms — an explicit `ingress: []` and tightly scoped egress — so this
pins the rule rather than the one instance that was wrong.

Source-level, deliberately: these are Go templates, and rendering them needs a
helm binary the unit tier does not have. The check is whether a `from:`/`to:`
selector is *written*, which is exactly what source tells you.
"""

from __future__ import annotations

import re
from pathlib import Path

_HELM_ROOT = Path("/app/helm/terrapod")
if not _HELM_ROOT.exists():  # local checkout fallback
    _HELM_ROOT = Path(__file__).resolve().parents[3] / "helm" / "terrapod"

_TEMPLATES = _HELM_ROOT / "templates"


def _policy_templates() -> list[Path]:
    return sorted(p for p in _TEMPLATES.glob("networkpolicy-*.yaml"))


def _rules(text: str, section: str) -> list[tuple[str, list[str]]]:
    """Each top-level rule under an `ingress:` / `egress:` section.

    Indentation-aware on purpose. Splitting on any `- ` treats the nested
    `- podSelector:` entries INSIDE a `from:` block as separate rules, and each
    of those has no `from:` of its own — which made an earlier version of this
    test report `networkpolicy-api.yaml`, a template that is entirely correct.
    Only list items at the rule's own indentation are rules.
    """
    out: list[tuple[str, list[str]]] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != f"{section}:":
            continue
        section_indent = len(line) - len(line.lstrip())

        body = []
        for nxt in lines[i + 1 :]:
            if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= section_indent:
                break
            body.append(nxt)

        starts = [
            (j, len(ln) - len(ln.lstrip()))
            for j, ln in enumerate(body)
            if ln.lstrip().startswith("- ")
        ]
        if not starts:
            continue  # `ingress: []` or a template-only body — nothing to check
        rule_indent = min(ind for _, ind in starts)
        bounds = [j for j, ind in starts if ind == rule_indent]
        for k, start in enumerate(bounds):
            end = bounds[k + 1] if k + 1 < len(bounds) else len(body)
            out.append((section, body[start:end]))
    return out


def _is_empty_selector(match: re.Match[str], rule: str) -> bool:
    """`from: []` admits everything, exactly like no selector at all.

    An earlier version of this test only asked whether the key was WRITTEN, so
    "fixing" a permissive rule as `- from: []` would have kept it green while
    admitting every source — the precise defect it exists to catch.

    Callers apply this to ingress only; see the comment at the call site for
    why an empty egress selector is legitimate.
    """
    inline = match.group(1).strip()
    if inline in ("[]", "[ ]"):
        return True
    # `from:` with nothing indented under it and nothing inline is also empty.
    if inline == "":
        tail = rule[match.end() :]
        return not re.search(r"^\s*-\s", tail, flags=re.M)
    return False


class TestEveryRuleNamesItsPeers:
    def test_there_are_policies_to_check(self):
        # A sweep that found nothing would pass silently, which is the shape of
        # failure this whole file exists to prevent.
        assert len(_policy_templates()) >= 2

    def test_the_sweep_finds_real_rules(self):
        total = sum(
            len(_rules(p.read_text(), sec))
            for p in _policy_templates()
            for sec in ("ingress", "egress")
        )
        assert total >= 5, f"only {total} rules parsed — the parser has drifted"

    def test_no_rule_omits_its_selector(self):
        offenders = []
        for path in _policy_templates():
            text = path.read_text()
            for section, selector in (("ingress", "from"), ("egress", "to")):
                for _, rule in _rules(text, section):
                    joined = "\n".join(rule)
                    match = re.search(rf"(?:^|\s|-\s){selector}:(.*)$", joined, flags=re.M)
                    # Empty is a defect for INGRESS only. `to: []` with ports
                    # is a real restriction — egress anywhere but on those
                    # ports, which is how the bundled charts reach DNS and the
                    # cluster API. An empty `from:` restricts nothing at all.
                    if match is not None and not (
                        section == "ingress" and _is_empty_selector(match, joined)
                    ):
                        continue
                    offenders.append(
                        f"{path.name}: a {section} rule declares no usable `{selector}:`, "
                        f"so it matches ALL peers on those ports — "
                        f"{joined.strip()[:60]!r}"
                    )
        assert not offenders, "\n  " + "\n  ".join(offenders)
