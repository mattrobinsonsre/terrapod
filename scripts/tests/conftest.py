"""Import the re-scan scripts by path.

`scripts/` is not a package and is deliberately not on `sys.path` — the scripts
are invoked as files by the workflow, which is what keeps them dependency-free.
Loading them here by location tests exactly what runs, rather than a copy.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


normalise = _load("rescan_normalise")
report = _load("rescan_report")
advisory = _load("rescan_advisory")
register_review = _load("register_review")
open_alerts = _load("rescan_open_alerts")
