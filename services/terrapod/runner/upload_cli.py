"""Module-runner shim so bash can invoke uploads via
`python -m terrapod.runner.upload_cli SUBCOMMAND ARGS...`.

The actual implementation lives in terrapod.runner.phases.uploads;
this module exists only so the `python -m` invocation reads
naturally — the bash entrypoint shouldn't have to know about the
`phases` subpackage.
"""

from __future__ import annotations

import sys

from terrapod.runner.phases.uploads import _cli_main

if __name__ == "__main__":
    sys.exit(_cli_main())
