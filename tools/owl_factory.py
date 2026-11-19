#!/usr/bin/env python3
"""Entry point for the factory provisioning tools.

    python tools/owl_factory.py check 192.168.1.60
    python tools/owl_factory.py provision 192.168.1.60
    python tools/owl_factory.py claim-labels --csv codes.csv

The tools themselves live in tools/factory/, which is git-ignored and not
part of the public OpenWeedLocator repository. They are used to build OWLs
in batches: flashing on the programming jig, registration, traceability
records and labels. This file only finds them and passes your arguments on.

Nothing here is needed to set up an OWL. For that, run: bash owl_setup.sh
"""

import subprocess
import sys
from pathlib import Path

FACTORY_DIR = Path(__file__).resolve().parent / "factory"
MAIN_TOOL = "owl_provision.py"
OTHER_TOOLS = {"claim-labels": "label_generator.py"}

MISSING = """\
[WARN] Factory provisioning tools were not found.
       Looked for: {path}

       They are used to build OWLs in batches and are not part of the public
       OpenWeedLocator repository, so this is expected on a normal checkout.

       You do not need them to set up an OWL. Run:  bash owl_setup.sh
"""


def main(argv: list) -> int:
    script = FACTORY_DIR / MAIN_TOOL
    if argv and argv[0] in OTHER_TOOLS:
        script = FACTORY_DIR / OTHER_TOOLS[argv[0]]
        argv = argv[1:]
    if not script.is_file():
        sys.stderr.write(MISSING.format(path=script))
        return 2
    return subprocess.call([sys.executable, str(script), *argv])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
