"""A temp OWL home for anything that runs production code as a test: the
pytest suite (tests/conftest.py) and the browser scripts in tests/e2e/.

use() sets OWL_HOME (utils/paths.py) to a fresh temp directory holding the
shipped config/ and models/ files, so nothing a test run writes (config,
logs, models, downloads, agent files) lands in the checkout it runs from.
Call it before importing production code: some paths are fixed at import.
"""

import os
import shutil
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Shipped files only: a developer's own CONTROLLER.ini, active-config pointer
# or downloaded models must not decide what a test sees
SHIPPED = ('GENERAL_CONFIG.ini', 'CONTROLLER_TEMPLATE.ini', 'GEOMETRY_TEMPLATE.ini',
           'bytetrack_owl.yaml', 'README.md', 'labels.txt')


def use():
    """Create the temp home, point OWL_HOME at it, return its Path."""
    home = Path(tempfile.mkdtemp(prefix='owl_test_home_'))
    for folder in ('config', 'models'):
        (home / folder).mkdir()
        for item in (REPO / folder).iterdir():
            if item.is_file() and item.name in SHIPPED:
                shutil.copy(item, home / folder / item.name)
    os.environ['OWL_HOME'] = str(home)
    return home
