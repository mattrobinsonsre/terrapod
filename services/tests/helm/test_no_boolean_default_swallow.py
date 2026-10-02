"""`| default true` on a boolean silently discards an operator's `false`.

Go templates treat `false` as the empty value, so `x | default true` renders
`true` both when the key is unset AND when the operator deliberately set it to
`false`. The opt-out is accepted by `helm lint`, survives the values schema, and
then does nothing — in the one direction nobody checks, because the chart still
renders a valid ConfigMap and the pod still starts.

Two keys shipped this way and both were found while reviewing an unrelated
change: `database.pool_pre_ping` (stale connections kept being handed out) and
`notifications.smtp.use_tls` (an operator pointing Terrapod at a relay with no
TLS got a connection failure their own values file said was impossible). The
template already carried comments warning about the trap in two other places
while these two lines still did it, which is the shape worth guarding: the
knowledge was written down and the code did not follow it.

The ban is blanket rather than "where the code default is true", because
`| default true` is wrong for a boolean either way. If the code default is true
it swallows the opt-out; if it is false the chart is silently overriding the
code's default AND still swallowing the opt-out. `hasKey` is the house pattern:
render the key when the operator set it, and otherwise say nothing and let the
model's own default stand.

Source-level, not rendered: what matters is whether the pattern is *written*,
and the unit tier has no helm binary.
"""

from __future__ import annotations

import pathlib
import re

TEMPLATES = pathlib.Path(__file__).resolve().parents[3] / "helm" / "terrapod" / "templates"

#: `{{ … | default true }}`, ignoring the `{{/* … */}}` comments that warn about it.
_SWALLOW = re.compile(r"\{\{(?!/\*)[^}]*\|\s*default\s+true[^}]*\}\}")


def test_no_template_renders_a_boolean_with_default_true() -> None:
    offenders: list[str] = []
    for template in sorted(TEMPLATES.rglob("*.yaml")):
        for number, line in enumerate(template.read_text().splitlines(), start=1):
            if _SWALLOW.search(line):
                offenders.append(f"{template.name}:{number}  {line.strip()}")

    assert offenders == [], (
        "`| default true` discards an operator's explicit `false`, because Go "
        "templates treat false as empty. Use the house pattern instead:\n"
        '  {{- if hasKey .Values.path.to "key" }}\n'
        "  key: {{ .Values.path.to.key }}\n"
        "  {{- end }}\n"
        "so an unset key renders nothing and the code default stands.\n" + "\n".join(offenders)
    )
