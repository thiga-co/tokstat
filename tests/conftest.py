"""Shared test fixtures and path setup.

tokstat stays zero-dependency at runtime; pytest is a dev-only extra
(`pip install -e '.[test]'`). These tests import the package straight from
``src/`` so no install step is required.
"""
import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
