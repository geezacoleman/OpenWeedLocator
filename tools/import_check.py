"""Fail when code imports a repo module that is not on disk.

owl_update.sh runs this after checkout, before anything restarts. v3.26.0
imported utils/support_bundle.py, which was never committed, and the
dashboard crash-looped on every unit that took the update. Nothing is
imported or executed: it walks the AST of each entry file and of every
local module those files import. Third-party and stdlib imports are left to
pip and the service health check.

    python tools/import_check.py owl.py controller/standalone/standalone.py
"""

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _local_root(top, bases):
    """The base a top-level name resolves to inside the repo, or None (stdlib,
    pip). Data directories (config/, models/) hold no .py files."""
    for base in bases:
        if (base / f'{top}.py').is_file():
            return base
        folder = base / top
        if folder.is_dir() and any(folder.rglob('*.py')):
            return base
    return None


def _module_path(base, parts):
    """file.py, package/__init__.py, or a namespace directory; None if missing."""
    target = base.joinpath(*parts)
    if target.with_suffix('.py').is_file():
        return target.with_suffix('.py')
    if (target / '__init__.py').is_file():
        return target / '__init__.py'
    if target.is_dir():
        return target
    return None


def _imports(path):
    """(module parts, imported names, relative level) for every import."""
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split('.'), [], 0
        elif isinstance(node, ast.ImportFrom):
            parts = node.module.split('.') if node.module else []
            yield parts, [a.name for a in node.names], node.level


def check(entries):
    """List of 'file: missing module' problems; empty when everything resolves."""
    problems, seen = [], set()
    todo = [Path(e).resolve() for e in entries]
    while todo:
        path = todo.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        here = path.parent
        for parts, names, level in _imports(path):
            if level:
                base = here
                for _ in range(level - 1):
                    base = base.parent
            else:
                base = _local_root(parts[0], (REPO, here))
                if base is None:
                    continue
            found = _module_path(base, parts) if parts else base
            if found is None:
                problems.append(f"{path.relative_to(REPO)}: missing {'.'.join(parts)}")
                continue
            if found.is_file():
                todo.append(found)
            for name in names:
                if name == '*':
                    continue
                sub = _module_path(base, parts + [name])
                if sub is not None:
                    if sub.is_file():
                        todo.append(sub)
                elif found.is_dir() or name not in found.read_text(encoding='utf-8'):
                    # A namespace package defines nothing; a module must at
                    # least mention the name it is supposed to export.
                    problems.append(f"{path.relative_to(REPO)}: missing "
                                    f"{'.'.join(parts + [name])}")
    return problems


def main(argv):
    problems = check(argv or ['owl.py'])
    for line in problems:
        print(f"import_check: {line}")
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
