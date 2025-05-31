# AGENTS Instructions

This repository does not yet include automated tests. When modifying or adding code:

1. Ensure all Python files compile:
   `python -m py_compile $(git ls-files '*.py')`
2. If a `tests/` directory exists, run `pytest -q` and confirm it passes.
3. Update the corresponding file in `notes/` summarising any changes made.
4. Provide a concise summary of key changes in the pull request description.

