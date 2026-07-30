"""`values.schema.json` and the blob-class register must name the same classes (#1151).

`ha.blobs.classes` is validated twice, on purpose and at different moments:

- **`helm lint` / `helm template`** rejects an unknown class name before anything is
  installed, which is where an operator wants to find a typo;
- **startup** rejects it again in `HABlobsConfig`, which catches the paths the chart
  is not involved in — an env override, a hand-written `config.yaml`, a values file
  the chart never linted.

Two enforcement points means two lists, and two lists drift. This is the gate that
stops them: the schema's pattern has to enumerate exactly the register's class
names. Without it, adding a class would silently produce one the chart refuses and
the code accepts — the operator sees a `helm lint` failure for a class that is
genuinely supported, which is a worse experience than either check alone.
"""

from __future__ import annotations

import json
from pathlib import Path

from terrapod.services.blob_classes import CLASS_NAMES, MODES

_HELM_ROOT = Path("/app/helm/terrapod")
if not _HELM_ROOT.exists():  # local checkout fallback (running outside the image)
    _HELM_ROOT = Path(__file__).resolve().parents[3] / "helm" / "terrapod"

_SCHEMA = _HELM_ROOT / "values.schema.json"


def _blobs_schema() -> dict:
    """The `api.config.ha.blobs` node.

    Reached through `$defs.apiConfig` because that is where the chart factors the
    API config block — resolved by lookup rather than assumed, so a future
    reshuffle of the schema fails this loudly instead of silently checking nothing.
    """
    schema = json.loads(_SCHEMA.read_text())
    return schema["$defs"]["apiConfig"]["properties"]["ha"]["properties"]["blobs"]


def _pattern_names() -> list[str]:
    """The class names the schema's single pattern property enumerates."""
    (pattern,) = _blobs_schema()["properties"]["classes"]["patternProperties"]
    return pattern.removeprefix("^(").removesuffix(")$").split("|")


class TestTheSchemaAndTheRegisterAgree:
    def test_the_schema_enumerates_exactly_the_registered_classes(self):
        schema_names = _pattern_names()
        registered = list(CLASS_NAMES)

        assert sorted(schema_names) == sorted(registered), (
            "values.schema.json and services/blob_classes.py disagree about which "
            "classes exist. A class in the register but not the schema is one "
            "`helm lint` rejects while the code supports it; the reverse is one the "
            "chart accepts and startup refuses."
        )

    def test_the_order_matches_the_register(self):
        """Kept in register order — irreplaceable first — so the pattern reads as
        the same list a human sees in a readiness report, not an arbitrary one."""
        assert _pattern_names() == list(CLASS_NAMES)

    def test_the_schema_enumerates_exactly_the_modes(self):
        blobs = _blobs_schema()

        assert blobs["properties"]["mode"]["enum"] == list(MODES)
        assert blobs["properties"]["classes"]["patternProperties"][f"^({'|'.join(CLASS_NAMES)})$"][
            "enum"
        ] == list(MODES)

    def test_an_unlisted_class_key_is_refused_by_the_schema(self):
        """`additionalProperties: false` alongside the pattern is what makes the
        enumeration a rejection rather than a suggestion."""
        assert _blobs_schema()["properties"]["classes"]["additionalProperties"] is False
