#!/usr/bin/env python3
"""Render `ee_matrix.py`'s output as GitHub Actions job outputs (#2154).

A separate file rather than a heredoc inside the workflow: a heredoc nested in a
YAML block scalar is an indentation hazard, and this is testable on its own.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matrix", type=Path)
    parser.add_argument("--summary", action="store_true", help="human line instead of outputs")
    arguments = parser.parse_args()

    matrix = json.loads(arguments.matrix.read_text())
    build = matrix["build"]
    lines = matrix["lines"]

    if arguments.summary:
        print(f"Planned {len(lines)} image(s) across {len(build)} build(s).")
        for entry in lines:
            print(f"  {entry['line']:5} ansible-core {entry['version']:9} on {entry['base']}")
        return 0

    # `include` so each entry is a complete, self-contained combination. The
    # cross product is built in ee_matrix.py, because a GitHub matrix `include`
    # EXTENDS combinations rather than crossing with another dimension.
    print("build=" + json.dumps({"include": build}))
    print("lines=" + json.dumps({"include": lines}))
    print("any=" + ("true" if lines else "false"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
