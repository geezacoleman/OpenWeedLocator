"""tools/owl_factory.py is the tracked entry point to the factory provisioning
tools. The tools themselves live in tools/factory/, which is git-ignored, so
on a public checkout (and on CI) the folder is not there.

The launcher is copied into a scratch folder for each test, so a developer
machine that does have tools/factory/ never runs the real tools from here.
"""

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAUNCHER = REPO / 'tools' / 'owl_factory.py'


def run_copy(tmp_path, *args):
    tools = tmp_path / 'tools'
    tools.mkdir(exist_ok=True)
    shutil.copy(LAUNCHER, tools / 'owl_factory.py')
    return subprocess.run([sys.executable, str(tools / 'owl_factory.py'), *args],
                          capture_output=True, text=True, timeout=30)


def test_missing_tools_warn_and_point_to_the_normal_install(tmp_path):
    result = run_copy(tmp_path, 'provision', '192.0.2.1')
    assert result.returncode == 2
    assert '[WARN]' in result.stderr
    assert 'not part of the public' in result.stderr
    assert 'bash owl_setup.sh' in result.stderr
    assert result.stdout == ''


def test_missing_claim_label_tool_warns_too(tmp_path):
    result = run_copy(tmp_path, 'claim-labels', '--csv', 'codes.csv')
    assert result.returncode == 2
    assert 'label_generator.py' in result.stderr


def test_arguments_are_passed_through_when_the_tools_exist(tmp_path):
    factory = tmp_path / 'tools' / 'factory'
    factory.mkdir(parents=True)
    (factory / 'owl_provision.py').write_text(
        'import sys\nprint("|".join(sys.argv[1:]))\nsys.exit(7)\n', encoding='utf-8')
    result = run_copy(tmp_path, '--config', 'x.toml', 'check', '192.0.2.1')
    assert result.returncode == 7
    assert result.stdout.strip() == '--config|x.toml|check|192.0.2.1'


def test_the_factory_folder_is_git_ignored():
    ignore = (REPO / '.gitignore').read_text(encoding='utf-8')
    assert '/tools/factory/' in ignore.splitlines()
