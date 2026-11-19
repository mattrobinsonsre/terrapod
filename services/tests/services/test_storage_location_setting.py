"""The `storage-location` rule: a configured name, or absent. `default` is refused."""

import pytest

from terrapod.config import S3Config, StorageBackend, StorageLocationConfig, settings
from terrapod.services.workspace_settings import validate_storage_location


@pytest.fixture(autouse=True)
def _locations(monkeypatch):
    eu = StorageLocationConfig(backend=StorageBackend.S3, s3=S3Config(bucket="eu-bucket"))
    monkeypatch.setattr(settings.storage, "locations", {"eu-west": eu})


def test_absent_means_the_default_location():
    assert validate_storage_location(None) is None


def test_the_literal_default_is_refused_rather_than_collapsed():
    """It used to return None, so a client sending `default` read `null` back.

    The Terraform provider writes the server's answer into state, so that
    round-trip is "Provider produced inconsistent result after apply" on every
    apply of `storage_location = "default"` -- unfixable from the config. Refuse
    at the write boundary instead, and say what to do.
    """
    with pytest.raises(ValueError, match="omit the attribute or send null"):
        validate_storage_location("default")


def test_a_configured_name_is_kept():
    assert validate_storage_location("eu-west") == "eu-west"


@pytest.mark.parametrize("raw", ["nowhere", "", "EU-WEST", " eu-west", 7, True, ["eu-west"]])
def test_anything_else_is_refused(raw):
    with pytest.raises(ValueError, match="storage-location"):
        validate_storage_location(raw)


def test_every_name_is_refused_when_none_are_configured(monkeypatch):
    monkeypatch.setattr(settings.storage, "locations", {})
    # `default` is refused whether or not any location is configured -- it is
    # the absence of one, not a name that happens to be unconfigured.
    with pytest.raises(ValueError, match="storage-location"):
        validate_storage_location("default")
    with pytest.raises(ValueError, match="storage-location"):
        validate_storage_location("eu-west")


class TestTheReadSideTolerance:
    """`default` is refused at the boundary and folded on the way out (#2621).

    Both halves matter and they fail in opposite directions. Without the
    refusal, a client's `default` reads back as `null` and the provider aborts
    every apply. Without the tolerance, a row written before the refusal breaks
    materialisation, the rule preview and a restore -- and the only way to fix
    that workspace is direct SQL, which is the #316 uneditable-row shape.
    """

    def test_a_stored_default_folds_to_none(self):
        from terrapod.services.workspace_settings import stored_storage_location

        assert stored_storage_location("default") is None

    def test_a_stored_configured_name_is_kept(self):
        from terrapod.services.workspace_settings import stored_storage_location

        assert stored_storage_location("eu-west") == "eu-west"

    def test_a_stored_unconfigured_name_is_still_refused(self):
        """Tolerance is for `default` alone. A row naming a location nobody
        configured is a real problem and must not be folded away silently."""
        from terrapod.services.workspace_settings import stored_storage_location

        with pytest.raises(ValueError, match="storage-location"):
            stored_storage_location("removed")

    def test_the_write_boundary_still_refuses_what_the_read_side_tolerates(self):
        with pytest.raises(ValueError, match="omit the attribute or send null"):
            validate_storage_location("default")


def test_every_read_path_uses_the_tolerant_reader():
    """Derived, so a fourth read path cannot quietly re-validate a stored value.

    A read path calling `validate_storage_location` on a row raises on a legacy
    `default` and leaves the workspace unfixable -- which is the whole reason
    the two functions exist separately.
    """
    import ast
    import pathlib

    from terrapod.services import workspace_settings

    root = pathlib.Path(workspace_settings.__file__).parent.parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name in ("workspace_settings.py",):
            continue
        src = path.read_text()
        if "validate_storage_location" not in src:
            continue
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name != "validate_storage_location":
                continue
            # A row's own attribute is the tell: `rule.storage_location`,
            # `settings["storage_location"]`. A request body is a dict `.get`
            # off attrs, which is a write and must keep refusing.
            for arg in node.args:
                if isinstance(arg, ast.Attribute) and arg.attr == "storage_location":
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "these validate a STORED storage_location, so a row holding the legacy "
        f"literal `default` raises and the workspace cannot be fixed: {offenders}. "
        "Use stored_storage_location() on a read path."
    )
