"""Where the OWL keeps the files it writes.

config/, logs/, models/, downloads/, uploads/ and agent/widgets|sessions sit
in the checkout itself. OWL_HOME moves them somewhere else. The test suite
sets it to a temp directory (tests/conftest.py) so a test run can never
rewrite the config, models or logs of the unit it runs on.

Code and static files are never read through here, only files the OWL
changes at run time.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def owl_home():
    """The directory holding config/, logs/, models/ and the rest. Read on
    every call, so it follows the environment."""
    override = os.environ.get('OWL_HOME', '').strip()
    return Path(override) if override else REPO_ROOT


def home_dir(*parts):
    """owl_home() / parts, as a Path."""
    return owl_home().joinpath(*parts)
