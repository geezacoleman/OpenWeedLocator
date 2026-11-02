"""tools/import_check.py: owl_update.sh's guard against code that imports a
repo module which never got committed (v3.26.0, utils/support_bundle.py)."""

import pytest

from tools import import_check


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / 'utils').mkdir()
    (tmp_path / 'utils' / 'helper.py').write_text('VALUE = 1\n')
    (tmp_path / 'controller' / 'standalone').mkdir(parents=True)
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'GENERAL_CONFIG.ini').write_text('')
    monkeypatch.setattr(import_check, 'REPO', tmp_path)
    return tmp_path


def entry(repo, source):
    path = repo / 'controller' / 'standalone' / 'standalone.py'
    path.write_text(source)
    return [str(path)]


@pytest.mark.unit
class TestImportCheck:
    def test_resolving_imports_pass(self, repo):
        (repo / 'controller' / 'standalone' / 'settings_routes.py').write_text(
            'from utils.helper import VALUE\n')
        assert import_check.check(entry(repo, (
            'import os\nimport flask\nfrom utils import helper\n'
            'from utils.helper import VALUE\nimport settings_routes\n'))) == []

    def test_uncommitted_utils_module_fails(self, repo):
        problems = import_check.check(entry(repo, 'from utils import support_bundle\n'))
        assert len(problems) == 1
        assert problems[0].endswith('standalone.py: missing utils.support_bundle')

    def test_missing_module_in_a_dotted_import_fails(self, repo):
        assert import_check.check(entry(repo, 'from utils.gone import thing\n'))

    def test_followed_into_local_modules(self, repo):
        (repo / 'controller' / 'standalone' / 'settings_routes.py').write_text(
            'from utils import support_bundle\n')
        problems = import_check.check(entry(repo, 'import settings_routes\n'))
        assert len(problems) == 1 and 'settings_routes.py' in problems[0]

    def test_name_a_module_never_defines_fails(self, repo):
        assert import_check.check(entry(repo, 'from utils.helper import NOT_THERE\n'))

    def test_data_directories_are_not_packages(self, repo):
        # config/ holds no .py: an import named config is someone else's
        assert import_check.check(entry(repo, 'import config\n')) == []

    def test_the_real_entry_points_resolve(self):
        root = import_check.REPO
        assert import_check.check([root / 'owl.py', root / 'controller/standalone/standalone.py',
                                   root / 'controller/networked/networked.py',
                                   root / 'controller/setup/setup_app.py']) == []
