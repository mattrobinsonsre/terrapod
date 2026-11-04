"""Provenance is assigned by the creator, not inferred from a source label."""

import ast
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.services.run_service import create_configuration_version


def test_every_configuration_archive_producer_classifies_its_bytes():
    """An omitted classification silently makes normal VCS/Catalog downloads unreadable."""
    package = Path(__file__).resolve().parents[2] / "terrapod"
    for path in package.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if name not in ("create_configuration_version", "ConfigurationVersion"):
                continue
            assert any(kw.arg == "archive_provenance" for kw in node.keywords), (
                f"{path}:{node.lineno} creates a CV without archive provenance"
            )


@pytest.mark.parametrize("source", ["catalog", "module-test", "module-publish", "vcs", "cli"])
async def test_unclassified_service_cv_is_unknown_even_for_familiar_source(source):
    db = AsyncMock()
    db.add = MagicMock()
    cv = await create_configuration_version(db, uuid.uuid4(), source=source)
    assert cv.archive_provenance == "unknown"
    assert cv.archive_module_id is None


async def test_protected_archive_records_module_id_at_creation():
    db = AsyncMock()
    db.add = MagicMock()
    module_id = uuid.uuid4()
    cv = await create_configuration_version(
        db,
        uuid.uuid4(),
        source="catalog",
        archive_provenance="protected",
        archive_module_id=module_id,
    )
    assert cv.archive_provenance == "protected"
    assert cv.archive_module_id == module_id
    db.add.assert_called_once_with(cv)
    db.flush.assert_awaited_once()


async def test_admin_only_archive_has_no_originating_module():
    db = AsyncMock()
    db.add = MagicMock()
    cv = await create_configuration_version(
        db, uuid.uuid4(), source="catalog", archive_provenance="admin-only"
    )
    assert cv.archive_provenance == "admin-only"
    assert cv.archive_module_id is None
    db.add.assert_called_once_with(cv)


@pytest.mark.parametrize(
    ("provenance", "module_id"),
    [
        ("protected", None),
        ("unprotected", uuid.uuid4()),
        ("unknown", uuid.uuid4()),
        ("invalid", None),
        ("protected", "not-a-uuid"),
        ("admin-only", uuid.uuid4()),
    ],
)
async def test_invalid_archive_classification_cannot_be_persisted(provenance, module_id):
    db = AsyncMock()
    with pytest.raises(ValueError):
        await create_configuration_version(
            db,
            uuid.uuid4(),
            archive_provenance=provenance,
            archive_module_id=module_id,
        )
    db.add.assert_not_called()
