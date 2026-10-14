"""Single source of truth for the argo-hold version at runtime.

`pyproject.toml` carries the same string for packaging. `tests/test_version.py` fails if
the two disagree, so a release bump has to change both.
"""
from __future__ import annotations

__version__ = "0.1.0"
