"""`terrapod.config` imports nothing from the rest of Terrapod (#1151).

This is a real invariant with two independent reasons, and violating it broke both
at once — the API would not boot, and the config-channel contract check could not
run.

**Startup.** `config.py` constructs `settings = Settings()` at module scope, so
every validator runs during the import. `terrapod.storage` imports `settings` back
at *its* module scope. So a validator that reaches into the services or storage
layer re-enters `terrapod.config` while it is still executing, before `settings`
exists — an `ImportError` on a name that is genuinely about to be defined, at the
one moment it isn't. The API pod crash-loops.

**The contract check.** `scripts/helm-config-contract.sh` renders every values
profile and parses it through the real models in a container with **pydantic and
pyyaml only** — no SQLAlchemy, no boto3. That minimal environment is what makes the
check cheap enough to run on every PR. Any transitive import of the services layer
turns it into `ModuleNotFoundError`.

So a config validator that needs a vocabulary declares it here (see
`BLOB_CLASS_NAMES`), and the module that owns the richer version imports *from*
config and asserts agreement. The dependency points one way.
"""

from __future__ import annotations

import ast
from pathlib import Path

_CONFIG = Path("/app/terrapod/config.py")
if not _CONFIG.exists():  # local checkout fallback (running outside the image)
    _CONFIG = Path(__file__).resolve().parents[2] / "terrapod" / "config.py"


def _imported_terrapod_modules() -> set[str]:
    """Every `terrapod.*` module `config.py` imports, at any scope.

    Deliberately includes function-local imports: deferring one does not make it
    safe here, because the validators run during the module's own import.
    """
    tree = ast.parse(_CONFIG.read_text())
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("terrapod"))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("terrapod"):
                found.add(node.module)

    return found


class TestConfigImportsNothingFromTerrapod:
    def test_no_terrapod_imports_at_all(self):
        imported = _imported_terrapod_modules()

        assert imported == set(), (
            "terrapod.config must import nothing from Terrapod itself. It found:\n  "
            + "\n  ".join(sorted(imported))
            + "\n\nA validator that needs a vocabulary declares it in config.py "
            "(see BLOB_CLASS_NAMES) and the richer module imports FROM config, "
            "asserting agreement at its own import. The reverse direction breaks "
            "startup: terrapod.storage imports `settings` back, so the import "
            "re-enters config.py before `settings` exists."
        )

    def test_the_blob_vocabulary_is_declared_here_rather_than_imported(self):
        """The concrete case this invariant was learned on."""
        from terrapod.config import BLOB_CLASS_NAMES, BLOB_MODES

        assert "state" in BLOB_CLASS_NAMES
        assert BLOB_MODES == ("off", "verify", "copy")

    def test_the_register_agrees_with_the_declared_vocabulary(self):
        """Written down twice, bound together. `blob_classes` raises at import if
        they diverge; this states the expectation from the other side so a failure
        names both."""
        from terrapod.config import BLOB_CLASS_NAMES
        from terrapod.services.blob_classes import CLASS_NAMES

        assert CLASS_NAMES == BLOB_CLASS_NAMES
